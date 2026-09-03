"""stdlib HTTP server for /v1/turn."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .config import GatewayConfig
from .loop_client import LoopTurnResult, run_loop_turn

log = logging.getLogger("agents_gateway.http")


def _check_secret(handler: BaseHTTPRequestHandler, expected: str) -> bool:
    if not expected:
        return True
    got = handler.headers.get("X-Gateway-Secret") or handler.headers.get("Gateway-Secret") or ""
    return got.strip() == expected


class _TurnHook:
    """Wrap a turn callable so BaseHTTPRequestHandler does not bind it as a method."""

    __slots__ = ("fn",)

    def __init__(self, fn: Callable[..., LoopTurnResult]) -> None:
        self.fn = fn

    def __call__(self, **kwargs) -> LoopTurnResult:
        return self.fn(**kwargs)


class TurnHandler(BaseHTTPRequestHandler):
    gateway_secret: str = ""
    turn_hook: _TurnHook | None = None
    config: GatewayConfig | None = None

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_POST(self) -> None:
        path = self.path.rstrip("/")
        if path not in ("/v1/turn", "/v1/alert", "/webhook/alert"):
            self.send_error(404)
            return
        if not _check_secret(self, self.gateway_secret):
            self.send_error(401, "unauthorized")
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self.send_error(400, "invalid json")
            return
        channel = str(body.get("channel") or ("webhook" if path != "/v1/turn" else "http"))
        user = str(body.get("user") or ("alert" if path != "/v1/turn" else "anonymous"))
        text = str(body.get("text") or body.get("message") or "")
        session = str(body.get("session") or "")
        user_id = str(body.get("user_id") or "")
        new_session = bool(body.get("new_session"))
        notify = bool(path in ("/v1/alert", "/webhook/alert") or body.get("notify") or body.get("broadcast"))

        runner = (self.turn_hook.fn if self.turn_hook else None) or run_loop_turn
        try:
            result = runner(
                channel=channel,
                user=user,
                message=text,
                session=session,
                user_id=user_id,
                new_session=new_session,
            )
        except Exception as exc:
            log.exception("turn failed")
            self.send_error(500, str(exc))
            return

        notified = False
        if notify and self.config and self.config.telegram_bot_token and self.config.telegram_allowed_chat_ids:
            from .telegram_adapter import send_message
            from .telegram_format import format_telegram_html
            try:
                primary_chat = self.config.telegram_allowed_chat_ids[0]
                html = format_telegram_html(result.reply, ())
                send_message(self.config.telegram_bot_token, primary_chat, html, parse_mode="HTML")
                notified = True
            except Exception as exc:
                log.warning("failed to broadcast notification to telegram: %s", exc)

        payload = {
            "reply": result.reply,
            "session": result.session,
            "user_id": result.user_id,
            "alias": result.alias,
            "returncode": result.returncode,
            "notified": notified,
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200 if result.returncode == 0 else 502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/health", "/"):
            data = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)


def serve_http(config: GatewayConfig, *, on_turn: Callable[..., LoopTurnResult] | None = None) -> ThreadingHTTPServer:
    attrs: dict = {"gateway_secret": config.gateway_secret, "config": config}
    if on_turn is not None:
        attrs["turn_hook"] = _TurnHook(on_turn)
    handler = type("ConfiguredTurnHandler", (TurnHandler,), attrs)
    server = ThreadingHTTPServer((config.gateway_host, config.gateway_port), handler)
    log.info("HTTP listening on %s:%s", config.gateway_host, config.gateway_port)
    return server
