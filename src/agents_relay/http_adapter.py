"""stdlib HTTP server: /v1/turn, /v1/stream, /v1/webhook/*, /v1/a2a/*."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlparse

from .a2a import handle_a2a_mail, list_peers
from .config import RelayConfig
from .loop_client import LoopTurnResult, iter_loop_stream, run_loop_turn, turn_params_from_body
from .webhooks import handle_webhook

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


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _read_json_body(handler: BaseHTTPRequestHandler) -> tuple[dict, bytes]:
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length)
    try:
        body = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except json.JSONDecodeError as exc:
        raise ValueError("invalid json") from exc
    if not isinstance(body, dict):
        raise ValueError("json body must be an object")
    return body, raw


def _sse_event(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


class TurnHandler(BaseHTTPRequestHandler):
    relay_secret: str = ""
    relay_config: RelayConfig | None = None
    turn_hook: _TurnHook | None = None
    config: RelayConfig | None = None

    @property
    def config(self) -> RelayConfig:
        return self.gateway_config or RelayConfig.from_env()

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/health", "/"):
            _json_response(self, 200, {"ok": True})
            return
        if path == "/v1/a2a/peers":
            if not _check_secret(self, self.gateway_secret):
                self.send_error(401, "unauthorized")
                return
            from urllib.parse import parse_qs, urlparse as _urlparse

            qs = parse_qs(_urlparse(self.path).query)
            proj = (qs.get("project") or [""])[0]
            result = list_peers(project=proj, config=self.config)
            _json_response(self, 200 if result.get("ok", True) else 502, result)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if not _check_secret(self, self.gateway_secret):
            self.send_error(401, "unauthorized")
            return
        try:
            if path == "/v1/turn":
                self._handle_turn()
            elif path == "/v1/stream":
                self._handle_stream()
            elif path.startswith("/v1/webhook/"):
                self._handle_webhook(path.removeprefix("/v1/webhook/"))
            elif path == "/v1/a2a/mail":
                self._handle_a2a_mail()
            else:
                self.send_error(404)
        except ValueError:
            self.send_error(400, "invalid json")
        except Exception as exc:
            log.exception("request failed")
            self.send_error(500, str(exc))

    def _handle_turn(self) -> None:
        body, _raw = _read_json_body(self)
        params = turn_params_from_body(body)
        runner = (self.turn_hook.fn if self.turn_hook else None) or run_loop_turn
        result = runner(
            channel=params.channel,
            user=params.user,
            message=params.message,
            config=self.config,
            session=params.session,
            user_id=params.user_id,
            new_session=params.new_session,
            provider=params.provider,
            persona=params.persona,
            project=params.project,
        )
        payload = {
            "reply": result.reply,
            "session": result.session,
            "user_id": result.user_id,
            "alias": result.alias,
            "returncode": result.returncode,
            "notified": notified,
        }
        _json_response(self, 200 if result.returncode == 0 else 502, payload)

    def _handle_stream(self) -> None:
        body, _raw = _read_json_body(self)
        params = turn_params_from_body(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for event in iter_loop_stream(params, config=self.config):
                payload = {"type": event.type}
                if event.type == "delta":
                    payload["text"] = event.text
                else:
                    payload.update(
                        {
                            "session": event.session,
                            "user_id": event.user_id,
                            "alias": event.alias,
                            "returncode": event.returncode,
                        }
                    )
                self.wfile.write(_sse_event(payload))
                self.wfile.flush()
        except BrokenPipeError:
            log.info("stream client disconnected")

    def _handle_webhook(self, source: str) -> None:
        body, raw = _read_json_body(self)
        secret_header = self.headers.get("X-Webhook-Secret") or self.headers.get("Webhook-Secret") or ""
        notify = str(body.get("notify_turn", "true")).lower() not in ("0", "false", "no")
        result = handle_webhook(
            source,
            body,
            raw_body=raw,
            secret_header=secret_header,
            config=self.config,
            notify_turn=notify,
        )
        status = 200 if result.get("ok") else 401
        _json_response(self, status, result)

    def _handle_a2a_mail(self) -> None:
        body, _raw = _read_json_body(self)
        result = handle_a2a_mail(body, config=self.config)
        _json_response(self, 200 if result.get("ok", True) else 502, result)


def serve_http(config: RelayConfig, *, on_turn: Callable[..., LoopTurnResult] | None = None) -> ThreadingHTTPServer:
    attrs: dict = {
        "relay_secret": config.relay_secret,
        "relay_config": config,
    }
    if on_turn is not None:
        attrs["turn_hook"] = _TurnHook(on_turn)
    handler = type("ConfiguredRelayHandler", (TurnHandler,), attrs)
    server = ThreadingHTTPServer((config.relay_host, config.relay_port), handler)
    log.info("HTTP listening on %s:%s", config.relay_host, config.relay_port)
    return server
