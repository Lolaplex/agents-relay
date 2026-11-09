"""stdlib HTTP server: /v1/turn, /v1/stream, /v1/inject, /v1/alert,
/webhook/alert, /v1/webhook/*, /v1/a2a/*."""

from __future__ import annotations

import hmac
import json
import logging
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlparse

from .a2a import handle_a2a_mail, list_peers
from .config import RelayConfig
from .loop_client import LoopTurnResult, run_loop_turn
from .turn import iter_loop_stream, turn_params_from_body
from .web import (
    cookie_from_header,
    handle_chat_stop,
    handle_chat_stream,
    handle_login,
    handle_logout,
    handle_status,
    serve_static,
    verify_cookie,
    web_enabled,
)
from .webhooks import handle_webhook

log = logging.getLogger("agents_relay.http")

_ENVELOPE_KEYS = frozenset({
    "channel",
    "user",
    "chat_id",
    "text",
    "message",
    "session",
    "user_id",
    "new_session",
    "notify",
    "broadcast",
    "attachments",
    "async",
    "prefix",
    "prompt",
    "secret",
    "token",
    "provider",
    "persona",
    "project",
})


def _parse_query(raw_path: str) -> tuple[str, dict[str, str]]:
    parsed = urllib.parse.urlsplit(raw_path)
    path = parsed.path.rstrip("/") or "/"
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
    params = {k: v[-1] for k, v in qs.items() if v}
    return path, params


def _check_secret(
    handler: BaseHTTPRequestHandler,
    expected: str,
    query_params: dict[str, str] | None = None,
) -> bool:
    cfg = getattr(handler, "config", None)
    if not expected:
        # A web surface without a relay secret would expose every endpoint to
        # anyone who can reach the port. The cookie is then the only gate.
        if isinstance(cfg, RelayConfig) and web_enabled(cfg):
            raw = cookie_from_header(handler.headers.get("Cookie") or "")
            return bool(raw and verify_cookie(cfg, raw))
        return True
    auth_header = (handler.headers.get("Authorization") or "").strip()
    bearer = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    qp = query_params or {}
    got = (
        handler.headers.get("X-Relay-Secret")
        or handler.headers.get("Relay-Secret")
        or handler.headers.get("X-Gateway-Secret")
        or handler.headers.get("Gateway-Secret")
        or bearer
        or qp.get("secret")
        or qp.get("token")
        or ""
    ).strip()
    if got and hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8")):
        return True
    # A browser client authenticates with an HttpOnly cookie: the token stays
    # out of JS storage and EventSource, which cannot set headers, still works.
    if isinstance(cfg, RelayConfig) and web_enabled(cfg):
        raw = cookie_from_header(handler.headers.get("Cookie") or "")
        if raw and verify_cookie(cfg, raw):
            return True
    return False


def _extract_payload_text(
    body: dict,
    query_params: dict[str, str] | None = None,
    *,
    include_extra: bool = False,
) -> str:
    """Extract turn text from a relay request or generic structured webhook JSON."""
    qp = query_params or {}
    raw_primary = str(body.get("text") or body.get("message") or "")
    if not include_extra:
        return raw_primary

    primary = raw_primary.strip()
    prefix = str(
        body.get("prefix")
        or body.get("prompt")
        or qp.get("prefix")
        or qp.get("prompt")
        or ""
    ).strip()

    extra_lines: list[str] = []
    for key, val in body.items():
        if key in _ENVELOPE_KEYS or val is None or val == "":
            continue
        if isinstance(val, (dict, list)):
            rendered = json.dumps(val, ensure_ascii=False)
        else:
            rendered = str(val).strip()
        if rendered:
            extra_lines.append(f"{key}: {rendered}")

    if not extra_lines and not prefix:
        return raw_primary

    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    if primary:
        parts.append(primary)
    if extra_lines:
        parts.append("\n".join(extra_lines))
    return "\n\n".join(parts)


class _TurnHook:
    """Wrap a turn callable so BaseHTTPRequestHandler does not bind it as a method."""

    __slots__ = ("fn",)

    def __init__(self, fn: Callable[..., LoopTurnResult]) -> None:
        self.fn = fn

    def __call__(self, **kwargs) -> LoopTurnResult:
        return self.fn(**kwargs)


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


# Kwargs accepted by loop_client.run_loop_turn beyond the core turn fields.
# turn_hook callables (e.g. telegram inbound) may not accept them.
_OPTIONAL_TURN_KWARGS = ("attachments", "provider", "persona", "project")


class TurnHandler(BaseHTTPRequestHandler):
    relay_secret: str = ""
    config: RelayConfig | None = None
    turn_hook: _TurnHook | None = None

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

    def _cfg(self) -> RelayConfig:
        return self.config or RelayConfig.from_env()

    def _run_turn(self, turn_kwargs: dict) -> LoopTurnResult:
        runner = (self.turn_hook.fn if self.turn_hook else None) or run_loop_turn
        kwargs = dict(turn_kwargs)
        if runner is run_loop_turn:
            kwargs["config"] = self._cfg()
        try:
            return runner(**kwargs)
        except TypeError:
            optional = {k: kwargs.pop(k) for k in _OPTIONAL_TURN_KWARGS if k in kwargs}
            if not optional:
                raise
            return runner(**kwargs)

    def _handle_inject(self, query_params: dict[str, str] | None = None) -> None:
        body = self._read_json()
        if body is None:
            return
        qp = query_params or {}
        cfg = self._cfg()
        raw_id = str(
            body.get("chat_id")
            or body.get("user")
            or qp.get("chat_id")
            or qp.get("user")
            or ""
        ).strip()
        if not raw_id and cfg.telegram_allowed_chat_ids:
            chat_id = int(cfg.telegram_allowed_chat_ids[0])
        elif not raw_id:
            self.send_error(400, "chat_id is required")
            return
        else:
            try:
                chat_id = int(raw_id)
            except ValueError:
                self.send_error(400, "chat_id must be numeric")
                return

        text = _extract_payload_text(body, qp, include_extra=True)
        runner = self.turn_hook.fn if self.turn_hook else None
        is_async = str(qp.get("async") or body.get("async") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )

        if is_async:
            from .jobs import Job, get_registry
            from .telegram_adapter import chat_allowed, handle_inbound_text

            if not chat_allowed(chat_id, cfg):
                self.send_error(403, "chat_id not in TELEGRAM_ALLOWED_CHAT_IDS")
                return
            stripped = text.strip()
            if not stripped:
                self.send_error(400, "text is required")
                return
            reg = get_registry(cfg)

            def _run(job: Job) -> None:
                handle_inbound_text(
                    chat_id=chat_id,
                    text=stripped,
                    config=cfg,
                    on_turn=runner,
                    user=str(chat_id),
                    on_pid=job.bind_pid,
                    cancelled=job.cancel.is_set,
                )

            job = reg.submit(chat_id, stripped[:80], _run)
            if job is None:
                self.send_error(429, "job queue full")
                return
            self._write_json(
                {"ok": True, "queued": True, "job_id": job.id, "chat_id": chat_id},
                202,
            )
            return

        from .telegram_adapter import inbound_payload, inject_text

        try:
            result = inject_text(
                chat_id=chat_id,
                text=text,
                config=cfg,
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
        path, query_params = _parse_query(self.path)

        if path == "/v1/auth/login":
            body = self._read_json()
            if body is None:
                return
            handle_login(self, body)
            return

        if path == "/v1/auth/logout":
            handle_logout(self)
            return

        if path == "/v1/workspaces":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            label = str(body.get("label") or "").strip()
            if not label:
                self.send_error(400, "label is required")
                return
            from .workspaces import create_workspace

            ws = create_workspace(label)
            self._write_json({"ok": True, "workspace": ws.to_dict()}, 201)
            return

        if path == "/v1/chat/stream":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            handle_chat_stream(self, body)
            return

        if path == "/v1/chat/stop":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            handle_chat_stop(self, body)
            return

        if path == "/v1/inject":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_inject(query_params)
            return

        if path == "/v1/a2a/mail":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            try:
                body, _raw = _read_json_body(self)
            except ValueError:
                self.send_error(400, "invalid json")
                return
            result = handle_a2a_mail(body, config=self._cfg())
            self._write_json(result, 200 if result.get("ok", True) else 502)
            return

        if path == "/v1/stream":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            try:
                body, _raw = _read_json_body(self)
            except ValueError:
                self.send_error(400, "invalid json")
                return
            self._handle_stream(body)
            return

        if path.startswith("/v1/webhook/"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            try:
                body, raw = _read_json_body(self)
            except ValueError:
                self.send_error(400, "invalid json")
                return
            self._handle_webhook(path.removeprefix("/v1/webhook/"), body, raw)
            return

        if path not in ("/v1/turn", "/v1/alert", "/webhook/alert"):
            self.send_error(404)
            return
        if not _check_secret(self, self.relay_secret, query_params):
            self.send_error(401, "unauthorized")
            return
        body = self._read_json()
        if body is None:
            return
        is_alert = path in ("/v1/alert", "/webhook/alert")
        channel = str(body.get("channel") or ("webhook" if is_alert else "http"))
        user = str(body.get("user") or ("alert" if is_alert else "anonymous"))
        text = _extract_payload_text(body, query_params, include_extra=is_alert)
        session = str(body.get("session") or "")
        user_id = str(body.get("user_id") or "")
        new_session = bool(body.get("new_session"))
        notify = bool(is_alert or body.get("notify") or body.get("broadcast"))
        raw_attachments = body.get("attachments") or []

        turn_kwargs = {
            "channel": channel,
            "user": user,
            "message": text,
            "session": session,
            "user_id": user_id,
            "new_session": new_session,
        }
        for key in ("provider", "persona", "project", "workspace"):
            val = str(body.get(key) or "").strip()
            if val:
                turn_kwargs[key] = val
        if raw_attachments:
            from .attachments import AttachmentRejected, allowed_attach_root, materialize_attachments
            from .state import inbox_dir

            try:
                turn_kwargs["attachments"] = materialize_attachments(
                    raw_attachments,
                    inbox_dir(self._cfg()),
                    allowed_root=allowed_attach_root(self._cfg()),
                )
            except AttachmentRejected:
                self.send_error(400, "attachment path outside allowed directory")
                return
        try:
            result = self._run_turn(turn_kwargs)
        except Exception as exc:
            log.exception("turn failed")
            self.send_error(500, str(exc))
            return

        notified = False
        cfg = self._cfg()
        if notify and cfg.telegram_bot_token and cfg.telegram_allowed_chat_ids:
            from .telegram_adapter import deliver_html
            from .telegram_format import extract_traces, format_telegram_html
            try:
                primary_chat = cfg.telegram_allowed_chat_ids[0]
                _, err_traces = extract_traces(result.stderr or "")
                html = format_telegram_html(result.reply, err_traces)
                deliver_html(cfg.telegram_bot_token, primary_chat, html)
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

    def _handle_stream(self, body: dict) -> None:
        params = turn_params_from_body(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # keep-alive makes BaseHTTPRequestHandler wait for another request, so the body never ends.
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for event in iter_loop_stream(params, config=self._cfg()):
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

    def _handle_webhook(self, source: str, body: dict, raw: bytes) -> None:
        secret_header = self.headers.get("X-Webhook-Secret") or self.headers.get("Webhook-Secret") or ""
        notify = str(body.get("notify_turn", "true")).lower() not in ("0", "false", "no")
        result = handle_webhook(
            source,
            body,
            raw_body=raw,
            secret_header=secret_header,
            config=self._cfg(),
            notify_turn=notify,
        )
        status = 200 if result.get("ok") else 401
        self._write_json(result, status)

    def do_GET(self) -> None:
        path, query_params = _parse_query(self.path)
        cfg = self._cfg()
        if path == "/v1/auth/status":
            handle_status(self)
            return
        if path == "/v1/workspaces":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            from .workspaces import list_workspaces

            rows = [ws.to_dict() for ws in list_workspaces()]
            self._write_json({"ok": True, "workspaces": rows}, 200)
            return
        if path in ("/health", "/"):
            if path == "/" and web_enabled(cfg) and serve_static(self, "index.html"):
                return
            self._write_json({"ok": True}, 200)
            return
        if path == "/app" or path.startswith("/app/"):
            from .web import guess_type, resolve_app_path

            # _parse_query strips trailing slashes; the raw path tells them apart.
            # Relative asset URLs in the PWA need the /app/ base.
            raw_path = urllib.parse.urlsplit(self.path).path
            if raw_path == "/app":
                self.send_response(302)
                self.send_header("Location", "/app/")
                self.end_headers()
                return
            target = resolve_app_path(raw_path)
            if target is None:
                self.send_error(404)
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", guess_type(target))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache" if target.name == "index.html" else "max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return
        if web_enabled(cfg) and serve_static(self, path):
            return
        if path == "/v1/a2a/peers":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            proj = query_params.get("project", "")
            result = list_peers(project=proj, config=self._cfg())
            self._write_json(result, 200 if result.get("ok", True) else 502)
            return
        self.send_error(404)


def serve_http(config: RelayConfig, *, on_turn: Callable[..., LoopTurnResult] | None = None) -> ThreadingHTTPServer:
    attrs: dict = {
        "relay_secret": config.relay_secret,
        "config": config,
    }
    if on_turn is not None:
        attrs["turn_hook"] = _TurnHook(on_turn)
    handler = type("ConfiguredRelayHandler", (TurnHandler,), attrs)
    server = ThreadingHTTPServer((config.relay_host, config.relay_port), handler)
    log.info("HTTP listening on %s:%s", config.relay_host, config.relay_port)
    return server
