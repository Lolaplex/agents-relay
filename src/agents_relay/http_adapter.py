"""stdlib HTTP server for /v1/turn and /v1/inject."""

from __future__ import annotations

import hmac
import json
import logging
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .config import RelayConfig
from .loop_client import LoopTurnResult, run_loop_turn

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
})


def _parse_query(raw_path: str) -> tuple[str, dict[str, str]]:
    parsed = urllib.parse.urlsplit(raw_path)
    path = parsed.path.rstrip("/")
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
    params = {k: v[-1] for k, v in qs.items() if v}
    return path, params


def _check_secret(
    handler: BaseHTTPRequestHandler,
    expected: str,
    query_params: dict[str, str] | None = None,
) -> bool:
    if not expected:
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
    if not got:
        return False
    return hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))


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

    def _handle_inject(self, query_params: dict[str, str] | None = None) -> None:
        body = self._read_json()
        if body is None:
            return
        qp = query_params or {}
        cfg = self.config or RelayConfig.from_env()
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
        if path == "/v1/inject":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_inject(query_params)
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
            from .attachments import AttachmentRejected, allowed_attach_root, materialize_attachments
            from .state import inbox_dir

            try:
                turn_kwargs["attachments"] = materialize_attachments(
                    raw_attachments,
                    inbox_dir(self.config),
                    allowed_root=allowed_attach_root(self.config),
                )
            except AttachmentRejected:
                self.send_error(400, "attachment path outside allowed directory")
                return
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
        path, _ = _parse_query(self.path)
        if path in ("/health", "/"):
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
