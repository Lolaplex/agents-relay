"""stdlib HTTP server for /v1/turn and /v1/inject."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .config import RelayConfig
from .loop_client import LoopTurnResult, run_loop_turn

log = logging.getLogger("agents_relay.http")


def _check_secret(handler: BaseHTTPRequestHandler, expected: str) -> bool:
    if not expected:
        return True
    got = (
        handler.headers.get("X-Relay-Secret")
        or handler.headers.get("Relay-Secret")
        or handler.headers.get("X-Gateway-Secret")
        or handler.headers.get("Gateway-Secret")
        or ""
    )
    return got.strip() == expected


class _TurnHook:
    """Wrap a turn callable so BaseHTTPRequestHandler does not bind it as a method."""

    __slots__ = ("fn",)

    def __init__(self, fn: Callable[..., LoopTurnResult]) -> None:
        self.fn = fn

    def __call__(self, **kwargs) -> LoopTurnResult:
        return self.fn(**kwargs)


class TurnHandler(BaseHTTPRequestHandler):
    relay_secret: str = ""
    turn_hook: _TurnHook | None = None
    config: RelayConfig | None = None

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self.send_error(400, "invalid json")
            return None

    def _write_json(self, payload: dict, status: int) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_inject(self) -> None:
        body = self._read_json()
        if body is None:
            return
        raw_id = str(body.get("chat_id") or body.get("user") or "").strip()
        text = str(body.get("text") or body.get("message") or "")
        if not raw_id:
            self.send_error(400, "chat_id is required")
            return
        try:
            chat_id = int(raw_id)
        except ValueError:
            self.send_error(400, "chat_id must be numeric")
            return
        runner = (self.turn_hook.fn if self.turn_hook else None)
        from .telegram_adapter import inbound_payload, inject_text

        try:
            result = inject_text(
                chat_id=chat_id,
                text=text,
                config=self.config or RelayConfig.from_env(),
                on_turn=runner,
            )
        except PermissionError as exc:
            self.send_error(403, str(exc))
            return
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        except Exception as exc:
            log.exception("inject failed")
            self.send_error(500, str(exc))
            return
        payload = inbound_payload(result)
        self._write_json(payload, 200 if result.returncode == 0 else 502)

    def do_POST(self) -> None:
        path = self.path.rstrip("/")
        if path == "/v1/inject":
            if not _check_secret(self, self.relay_secret):
                self.send_error(401, "unauthorized")
                return
            self._handle_inject()
            return
        if path not in ("/v1/turn", "/v1/alert", "/webhook/alert"):
            self.send_error(404)
            return
        if not _check_secret(self, self.relay_secret):
            self.send_error(401, "unauthorized")
            return
        body = self._read_json()
        if body is None:
            return
        channel = str(body.get("channel") or ("webhook" if path != "/v1/turn" else "http"))
        user = str(body.get("user") or ("alert" if path != "/v1/turn" else "anonymous"))
        text = str(body.get("text") or body.get("message") or "")
        session = str(body.get("session") or "")
        user_id = str(body.get("user_id") or "")
        new_session = bool(body.get("new_session"))
        notify = bool(path in ("/v1/alert", "/webhook/alert") or body.get("notify") or body.get("broadcast"))
        raw_attachments = body.get("attachments") or []

        runner = (self.turn_hook.fn if self.turn_hook else None) or run_loop_turn
        turn_kwargs = {
            "channel": channel,
            "user": user,
            "message": text,
            "session": session,
            "user_id": user_id,
            "new_session": new_session,
        }
        if raw_attachments:
            from .attachments import materialize_attachments
            from .state import inbox_dir

            turn_kwargs["attachments"] = materialize_attachments(
                raw_attachments,
                inbox_dir(self.config),
            )
        try:
            try:
                result = runner(**turn_kwargs)
            except TypeError:
                if "attachments" not in turn_kwargs:
                    raise
                turn_kwargs.pop("attachments", None)
                result = runner(**turn_kwargs)
        except Exception as exc:
            log.exception("turn failed")
            self.send_error(500, str(exc))
            return

        notified = False
        if notify and self.config and self.config.telegram_bot_token and self.config.telegram_allowed_chat_ids:
            from .telegram_adapter import deliver_html
            from .telegram_format import extract_traces, format_telegram_html
            try:
                primary_chat = self.config.telegram_allowed_chat_ids[0]
                _, err_traces = extract_traces(result.stderr or "")
                html = format_telegram_html(result.reply, err_traces)
                deliver_html(self.config.telegram_bot_token, primary_chat, html)
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
        self._write_json(payload, 200 if result.returncode == 0 else 502)

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


def serve_http(config: RelayConfig, *, on_turn: Callable[..., LoopTurnResult] | None = None) -> ThreadingHTTPServer:
    attrs: dict = {"relay_secret": config.relay_secret, "config": config}
    if on_turn is not None:
        attrs["turn_hook"] = _TurnHook(on_turn)
    handler = type("ConfiguredTurnHandler", (TurnHandler,), attrs)
    server = ThreadingHTTPServer((config.relay_host, config.relay_port), handler)
    log.info("HTTP listening on %s:%s", config.relay_host, config.relay_port)
    return server
