"""stdlib HTTP server: /v1/turn, /v1/stream, /v1/inject, /v1/alert,
/webhook/alert, /v1/webhook/*, /v1/a2a/*, /v1/traces, /v1/approvals."""

from __future__ import annotations

import hmac
import json
import logging
import urllib.parse
from collections import deque
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .a2a import handle_a2a_mail, list_peers
from .config import RelayConfig
from .loop_client import LoopTurnResult, run_loop_turn
from .turn import iter_loop_stream, resolve_workspace_id, turn_params_from_body, valid_id
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
    "model",
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
_OPTIONAL_TURN_KWARGS = ("attachments", "provider", "model", "persona", "project", "workdir")


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

    def _cors_origin(self) -> str:
        """Origin to echo on /v1/* when RELAY_CORS_ORIGINS allows it (off by default)."""
        origins = self._cfg().cors_origins
        if not origins:
            return ""
        path, _ = _parse_query(self.path or "/")
        if not path.startswith("/v1/"):
            return ""
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return ""
        if "*" in origins:
            return "*"
        return origin if origin in origins else ""

    def end_headers(self) -> None:
        allowed = self._cors_origin()
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        if not self._cors_origin():
            self.send_error(404)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        req_headers = (self.headers.get("Access-Control-Request-Headers") or "").strip()
        self.send_header(
            "Access-Control-Allow-Headers",
            req_headers or "Content-Type, Authorization, X-Relay-Secret",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

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
            ws_id = str(body.get("id") or "").strip()
            if ws_id and not valid_id(ws_id):
                self.send_error(400, f"invalid workspace id: {ws_id!r}")
                return
            if not ws_id and not label:
                self.send_error(400, "id or label is required")
                return
            from .workspaces import create_workspace

            try:
                ws = create_workspace(label, ws_id)
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            self._write_json({"ok": True, "workspace": ws.to_dict()}, 201)
            return

        parts = path.split("/")
        # /v1/workspaces/<ws>/personas[/<p>[/sessions[/<s>]]]
        if (len(parts) >= 5 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "personas"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_persona_post(parts, query_params)
            return

        # POST /v1/approvals/<id> {allow: true|false}
        if len(parts) == 4 and parts[1] == "v1" and parts[2] == "approvals":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_approval_post(parts[3], query_params)
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

        # Workspace/persona are ids only — a path or label is a client bug.
        ws = None
        ws_ref = str(body.get("workspace") or "").strip()
        if ws_ref:
            try:
                ws = resolve_workspace_id(ws_ref)
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
        persona_ref = str(body.get("persona") or "").strip()
        if persona_ref and not valid_id(persona_ref):
            self.send_error(400, f"invalid persona id: {persona_ref!r}")
            return

        turn_kwargs = {
            "channel": channel,
            "user": user,
            "message": text,
            "session": session,
            "user_id": user_id,
            "new_session": new_session,
        }
        for key in ("provider", "model", "persona", "project", "workspace", "workdir"):
            val = str(body.get(key) or "").strip()
            if val:
                turn_kwargs[key] = val
        if raw_attachments:
            from .attachments import AttachmentRejected, allowed_attach_root, materialize_attachments
            from .state import inbox_dir

            dest = (ws.relay_state / "inbox") if ws is not None else inbox_dir(self._cfg())
            allowed_root = (
                ws.relay_state if ws is not None
                else allowed_attach_root(self._cfg())
            )
            try:
                turn_kwargs["attachments"] = materialize_attachments(
                    raw_attachments,
                    dest,
                    allowed_root=allowed_root,
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

    def _ws_and_persona(self, parts: list[str]):
        """(Workspace, persona id) from route segments; ids only (C22 rules)."""
        from .workspaces import get_workspace

        ws_id, persona = parts[3], parts[5] if len(parts) > 5 else ""
        for label, ref in (("workspace", ws_id), ("persona", persona)):
            if ref and not valid_id(ref):
                raise ValueError(f"invalid {label} id: {ref!r}")
        ws = get_workspace(ws_id)
        return ws, persona

    def _handle_persona_post(self, parts: list[str], query_params: dict) -> None:
        body = self._read_json()
        if body is None:
            return
        try:
            ws, persona = self._ws_and_persona(parts)
        except KeyError:
            self.send_error(404, "unknown workspace")
            return
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        from .workspaces import create_persona, create_session, list_sessions

        if len(parts) == 5:
            # POST /v1/workspaces/<ws>/personas {id, ...manifest fields}
            pid = str(body.get("id") or "").strip()
            if not pid or not valid_id(pid):
                self.send_error(400, "persona id is required (ids only)")
                return
            row = create_persona(ws, pid, body)
            self._write_json({"ok": True, "persona": row}, 201)
            return
        if len(parts) == 7 and parts[6] == "sessions":
            # POST .../personas/<p>/sessions {user, channel, workdir}
            try:
                rec = create_session(
                    ws, persona,
                    user=str(body.get("user") or body.get("user_id") or ""),
                    channel=str(body.get("channel") or ""),
                    workdir=str(body.get("workdir") or ""),
                )
            except RuntimeError as exc:
                self.send_error(501, str(exc))
                return
            self._write_json({"ok": True, "session": rec}, 201)
            return
        if len(parts) == 7 and parts[6] == "inbox":
            # POST .../personas/<p>/inbox {title, note, user}
            title = str(body.get("title") or "").strip()
            if not title:
                self.send_error(400, "title is required")
                return
            from agents_workspace.inbox import create_task
            try:
                task = create_task(
                    ws, persona, title,
                    note=str(body.get("note") or ""),
                    user=str(body.get("user") or ""),
                    task_id=str(body.get("id") or ""),
                )
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            self._write_json({"ok": True, "task": task}, 201)
            return
        if (len(parts) == 9 and parts[6] == "inbox"
                and parts[8] == "handoff"):
            # POST .../inbox/<task>/handoff {to, note}
            to = str(body.get("to") or "").strip()
            if not to or not valid_id(to):
                self.send_error(400, "handoff 'to' persona id is required")
                return
            from agents_workspace.inbox import handoff
            try:
                task = handoff(
                    ws, parts[7], to,
                    from_persona=persona,
                    note=str(body.get("note") or ""),
                )
            except ValueError as exc:
                msg = str(exc)
                self.send_error(404 if "unknown task" in msg else 400, msg)
                return
            self._write_json({"ok": True, "task": task}, 200)
            return
        self.send_error(404)

    def _handle_inbox_get(self, parts: list[str]) -> None:
        try:
            ws, persona = self._ws_and_persona(parts)
        except KeyError:
            self.send_error(404, "unknown workspace")
            return
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        from agents_workspace.inbox import list_inbox

        self._write_json(
            {"ok": True, "workspace": ws.id, "persona": persona,
             "inbox": list_inbox(ws, persona)}, 200)

    def _handle_session_get(self, parts: list[str], query_params: dict) -> None:
        try:
            ws, persona = self._ws_and_persona(parts)
        except KeyError:
            self.send_error(404, "unknown workspace")
            return
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        from .workspaces import get_session, list_sessions

        if len(parts) == 7:
            user = str(query_params.get("user") or "").strip()
            try:
                rows = list_sessions(ws, persona=persona, user=user)
            except RuntimeError as exc:
                self.send_error(501, str(exc))
                return
            self._write_json(
                {"ok": True, "workspace": ws.id, "persona": persona,
                 "sessions": rows}, 200)
            return
        sid = parts[7]
        if not valid_id(sid) and not sid.startswith(("ses_", "proc-", "telegram-", "repl-")):
            self.send_error(400, f"invalid session id: {sid!r}")
            return
        try:
            rec = get_session(ws, persona, sid)
        except RuntimeError as exc:
            self.send_error(501, str(exc))
            return
        if rec is None:
            self.send_error(404, "unknown session")
            return
        payload = {"ok": True, "session": rec}
        # history = session record + its trace events (lazy traces dep)
        try:
            from agents_traces.store import TraceStore
            events = [
                e.to_dict() if hasattr(e, "to_dict") else dict(e)
                for e in TraceStore(ws.traces).iter_events(session_id=sid)
            ]
            payload["events"] = events
        except ImportError:
            pass
        except Exception as exc:
            log.warning("session events unavailable: %s", exc)
        self._write_json(payload, 200)

    def _handle_traces(self, query_params: dict) -> None:
        """Tail the workspace's daily agents-traces JSONL files for one session.

        agents-traces appends to ``<ws>/traces/YYYY-MM-DD.jsonl`` (there is no
        per-session file), so ``session`` filters each line's ``session`` field.
        ``lines`` are the last ``limit`` matching raw JSONL lines, oldest first.
        Without ``session`` the on-disk session ids are listed instead.
        ``persona`` is a narrowing hint: events stamped with a *different*
        persona are skipped; unstamped events still match.
        """
        ws_ref = str(query_params.get("workspace") or "").strip()
        persona = str(query_params.get("persona") or "").strip()
        sid = str(query_params.get("session") or "").strip()
        for label, ref in (("workspace", ws_ref), ("persona", persona)):
            if ref and not valid_id(ref):
                self.send_error(400, f"invalid {label} id: {ref!r}")
                return
        if sid and not valid_id(sid) and not sid.startswith(
                ("ses_", "proc-", "telegram-", "repl-")):
            self.send_error(400, f"invalid session id: {sid!r}")
            return
        from .workspaces import get_workspace, select_workspace

        try:
            ws = get_workspace(ws_ref) if ws_ref else select_workspace()
        except (KeyError, ValueError):
            self.send_error(400, f"unknown workspace: {ws_ref!r}")
            return
        try:
            limit = max(1, min(int(str(query_params.get("limit") or "50")), 500))
        except ValueError:
            self.send_error(400, "limit must be numeric")
            return

        if not sid:
            root = Path(ws.personas)
            dirs = [root / persona] if persona else (
                sorted(p for p in root.iterdir() if p.is_dir())
                if root.is_dir() else []
            )
            sessions: list[str] = []
            for pdir in dirs:
                sdir = pdir / "sessions"
                if sdir.is_dir():
                    sessions.extend(f.stem for f in sdir.glob("*.json"))
            self._write_json(
                {
                    "ok": True,
                    "lines": [],
                    "sessions": sorted(sessions),
                    "note": "no session given; pass session=<id> to tail trace events",
                },
                200,
            )
            return

        tail: deque[str] = deque(maxlen=limit)
        troot = Path(ws.traces)
        if troot.is_dir():
            for path in sorted(troot.glob("*.jsonl")):
                try:
                    fh = open(path, encoding="utf-8", errors="replace")
                except OSError:
                    continue
                with fh:
                    for line in fh:
                        if sid not in line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict) or event.get("session") != sid:
                            continue
                        meta = event.get("metadata")
                        stamp = meta.get("persona") if isinstance(meta, dict) else None
                        if persona and stamp and stamp != persona:
                            continue
                        tail.append(line.rstrip("\r\n"))
        self._write_json({"ok": True, "lines": list(tail), "note": ""}, 200)

    def _approvals_dir(self, query_params: dict) -> Path:
        """``<ws>/relay/approvals`` for ?workspace=<id>, else the serve state dir."""
        from .state import approval_dir
        from .workspaces import get_workspace

        ws_ref = str(query_params.get("workspace") or "").strip()
        if not ws_ref:
            return approval_dir(self._cfg())
        if not valid_id(ws_ref):
            raise ValueError(f"invalid workspace id: {ws_ref!r}")
        return get_workspace(ws_ref).relay_state / "approvals"

    def _handle_approvals_get(self, query_params: dict) -> None:
        from .state import read_json

        try:
            adir = self._approvals_dir(query_params)
        except KeyError:
            self.send_error(404, "unknown workspace")
            return
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        pending: list[tuple[float, dict]] = []
        if adir.is_dir():
            for path in adir.glob("*.json"):
                rec = read_json(path)
                if not rec or rec.get("status") != "pending":
                    continue
                req = rec.get("request")
                if not isinstance(req, dict):
                    req = {}
                try:
                    created = float(rec.get("created_at") or 0)
                except (TypeError, ValueError):
                    created = 0.0
                pending.append((
                    created,
                    {
                        "id": str(rec.get("id") or path.stem),
                        "tool": str(req.get("tool") or ""),
                        "summary": str(req.get("summary") or ""),
                    },
                ))
        rows = [row for _, row in sorted(pending, key=lambda item: item[0])]
        self._write_json({"ok": True, "approvals": rows}, 200)

    def _handle_approval_post(self, ident: str, query_params: dict) -> None:
        """Resolve a pending approval through the same file /v1/turn waits on."""
        body = self._read_json()
        if body is None:
            return
        allow = body.get("allow")
        if not isinstance(allow, bool):
            self.send_error(400, "allow must be true or false")
            return
        cfg = self._cfg()
        ws_ref = str(query_params.get("workspace") or "").strip()
        if ws_ref:
            try:
                cfg = replace(cfg, state_dir=str(self._approvals_dir(query_params).parent))
            except KeyError:
                self.send_error(404, "unknown workspace")
                return
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
        from .approvals import decide_approval

        try:
            code, note = decide_approval(cfg, ident, allow=allow)
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        except Exception as exc:
            log.exception("approval decide failed")
            self.send_error(500, str(exc))
            return
        if code != 0:
            self.send_error(400 if note == "invalid approval id" else 404, note)
            return
        self._write_json({"ok": True, "id": ident, "status": note}, 200)

    def _handle_stream(self, body: dict) -> None:
        params = turn_params_from_body(body)
        if params.workspace:
            try:
                resolve_workspace_id(params.workspace)
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
        if params.persona and not valid_id(params.persona):
            self.send_error(400, f"invalid persona id: {params.persona!r}")
            return
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
        if path == "/v1/personas":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            from .workspaces import get_workspace, list_personas, select_workspace

            ws_ref = str(query_params.get("workspace") or "").strip()
            try:
                ws = get_workspace(ws_ref) if ws_ref else select_workspace()
            except (KeyError, ValueError) as exc:
                self.send_error(400, f"unknown workspace: {ws_ref!r}")
                return
            self._write_json(
                {"ok": True, "workspace": ws.id,
                 "personas": list_personas(ws)}, 200)
            return
        if path == "/v1/traces":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_traces(query_params)
            return
        if path == "/v1/approvals":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_approvals_get(query_params)
            return
        if path == "/v1/providers":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            try:
                from runner.providers import list_providers
                rows = [
                    {"name": r.get("name"), "kind": r.get("kind"),
                     "model": r.get("model"), "when": r.get("when")}
                    for r in list_providers()
                ]
            except Exception as exc:
                self.send_error(500, f"providers unavailable: {exc}")
                return
            self._write_json({"ok": True, "providers": rows}, 200)
            return
        parts = path.split("/")
        # GET /v1/workspaces/<ws>/personas/<p>/inbox
        if (len(parts) == 7 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "personas"
                and parts[6] == "inbox"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_inbox_get(parts)
            return
        # GET /v1/workspaces/<ws>/personas/<p>/sessions[/<s>]
        if (len(parts) in (7, 8) and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "personas"
                and parts[6] == "sessions"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_session_get(parts, query_params)
            return
        if path in ("/health", "/"):
            if path == "/" and web_enabled(cfg) and serve_static(self, "index.html"):
                return
            self._write_json({"ok": True}, 200)
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

    def do_DELETE(self) -> None:
        path, query_params = _parse_query(self.path)
        parts = path.split("/")
        # DELETE /v1/workspaces/<id>[?hard=1]
        if (len(parts) == 4 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[3]):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            ws_id = parts[3]
            if not valid_id(ws_id):
                self.send_error(400, f"invalid workspace id: {ws_id!r}")
                return
            from agents_workspace import Host

            try:
                out = Host.from_env().delete(
                    ws_id, hard=str(query_params.get("hard") or "") == "1")
            except ValueError as exc:
                msg = str(exc)
                code = 409 if "selected workspace" in msg else 400
                self.send_error(code, msg)
                return
            self._write_json({"ok": True, "workspace": ws_id,
                              "archived": str(out)}, 200)
            return
        # DELETE /v1/workspaces/<ws>/personas/<p>[?hard=1]
        if (len(parts) == 6 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "personas"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            try:
                ws, _persona = self._ws_and_persona(parts)
            except KeyError:
                self.send_error(404, "unknown workspace")
                return
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            pid = parts[5]
            if not valid_id(pid):
                self.send_error(400, f"invalid persona id: {pid!r}")
                return
            from .workspaces import delete_persona

            out = delete_persona(
                ws, pid, hard=str(query_params.get("hard") or "") == "1")
            if out is None:
                self.send_error(404, "unknown persona")
                return
            self._write_json({"ok": True, "persona": pid,
                              "archived": str(out)}, 200)
            return
        # DELETE /v1/workspaces/<ws>/personas/<p>/sessions/<s>
        if (len(parts) == 8 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "personas"
                and parts[6] == "sessions"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            try:
                ws, persona = self._ws_and_persona(parts)
            except KeyError:
                self.send_error(404, "unknown workspace")
                return
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            sid = parts[7]
            if not valid_id(sid) and not sid.startswith(("ses_", "proc-", "telegram-", "repl-")):
                self.send_error(400, f"invalid session id: {sid!r}")
                return
            from .workspaces import delete_session

            try:
                removed = delete_session(ws, persona, sid)
            except RuntimeError as exc:
                self.send_error(501, str(exc))
                return
            if not removed:
                self.send_error(404, "unknown session")
                return
            self._write_json({"ok": True, "session": sid}, 200)
            return
        self.send_error(404)


def serve_http(config: RelayConfig, *, on_turn: Callable[..., LoopTurnResult] | None = None) -> ThreadingHTTPServer:
    attrs: dict = {
        "relay_secret": config.relay_secret,
        "config": config,
    }
    if on_turn is not None:
        attrs["turn_hook"] = _TurnHook(on_turn)
    try:
        from agents_workspace.runtime import sweep

        removed = sweep()
        if removed:
            log.info("swept %d stale klanker-ws containers", len(removed))
    except Exception:
        pass
    handler = type("ConfiguredRelayHandler", (TurnHandler,), attrs)
    server = ThreadingHTTPServer((config.relay_host, config.relay_port), handler)
    log.info("HTTP listening on %s:%s", config.relay_host, config.relay_port)
    return server
