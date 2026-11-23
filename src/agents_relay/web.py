"""Cookie auth, static assets, and streaming chat for a browser client.

The relay stays a dumb gateway: it serves whatever directory ``AGENTS_RELAY_WEB_DIR``
points at and never ships a UI of its own. The dashboard is built elsewhere and
mounted here, so the browser talks to the same origin as the API and needs no
CORS.

``/app/`` serves the bundled herd PWA from the packaged ``web/`` directory (or
``AGENTS_RELAY_WEB_DIR``). Static files stay public; ``/v1/*`` keeps the secret
check.

Auth is one shared token exchanged for an HttpOnly cookie. A cookie keeps the
token out of JS storage and works for ``EventSource``, which cannot set headers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from .config import RelayConfig
from .turn import TurnParams, iter_loop_stream_live

log = logging.getLogger("agents_relay.web")

COOKIE_NAME = "relay_web"
COOKIE_MAX_AGE = 30 * 24 * 3600

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}

_NO_CACHE = frozenset({".html", ".js", ".css", ".webmanifest"})

_ACTIVE: dict[str, dict[str, Any]] = {}
_ACTIVE_LOCK = threading.Lock()

# Event buffer cap per run: deltas are small but long turns are not bounded;
# an attach that starts mid-run replays only the tail. Trailer/start/meta are
# always kept (replayed first, marked replayed).
_RUN_EVENT_CAP = 2000


def _new_run_entry() -> dict[str, Any]:
    return {
        "cancel": threading.Event(),
        "cond": threading.Condition(),
        "meta": {},
        "events": [],
        "done": False,
    }


def _publish(entry: dict[str, Any], payload: dict[str, Any]) -> None:
    cond = entry["cond"]
    with cond:
        events = entry["events"]
        events.append(payload)
        if len(events) > _RUN_EVENT_CAP:
            keep = [e for e in events[:8] if e.get("type") in ("start", "meta")]
            events[:] = keep + events[-(_RUN_EVENT_CAP - len(keep)) :]
        cond.notify_all()


def _find_run(run: str = "", session: str = "", workspace: str = "", persona: str = "") -> tuple[str, dict[str, Any] | None]:
    with _ACTIVE_LOCK:
        if run:
            return run, _ACTIVE.get(run)
        for rid, entry in _ACTIVE.items():
            meta = entry.get("meta") or {}
            if session and meta.get("session") != session:
                continue
            if workspace and meta.get("workspace") != workspace:
                continue
            if persona and meta.get("persona") != persona:
                continue
            return rid, entry
    return "", None


def handle_runs_attach(handler: BaseHTTPRequestHandler, query: dict[str, Any]) -> None:
    """SSE attach to a running turn: replay buffered events, then live fan-out."""
    run_id, entry = _find_run(
        run=str(query.get("run") or "").strip(),
        session=str(query.get("session") or "").strip(),
        workspace=str(query.get("workspace") or "").strip(),
        persona=str(query.get("persona") or "").strip(),
    )
    if entry is None:
        handler._write_json({"ok": False, "error": "no such running turn"}, 404)  # type: ignore[attr-defined]
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache, no-transform")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()

    index = 0
    cond = entry["cond"]
    try:
        handler.wfile.write(_sse({"type": "attach", "run": run_id, "meta": entry.get("meta") or {}}))
        handler.wfile.flush()
        while True:
            ping = False
            with cond:
                pending = entry["events"][index:]
                index = len(entry["events"])
                if not pending and not entry["done"]:
                    cond.wait(timeout=15.0)
                    pending = entry["events"][index:]
                    index = len(entry["events"])
                    ping = not pending and not entry["done"]
                finished = entry["done"] and index >= len(entry["events"])
            if ping:
                handler.wfile.write(b": ping\n\n")
                handler.wfile.flush()
                continue
            for payload in pending:
                handler.wfile.write(_sse(payload))
                handler.wfile.flush()
            if finished:
                break
    except (BrokenPipeError, ConnectionResetError):
        log.info("attached client disconnected (run %s)", run_id)


def web_enabled(config: RelayConfig) -> bool:
    return bool((config.web_token or "").strip())


def web_root() -> Path:
    """Bundled PWA root. ``AGENTS_RELAY_WEB_DIR`` overrides it."""
    override = os.environ.get("AGENTS_RELAY_WEB_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent / "web"


def static_root(config: RelayConfig) -> Path | None:
    raw = (config.static_dir or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    root = web_root()
    return root if root.is_dir() else None


def guess_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime
    if path.suffix == ".webmanifest":
        return "application/manifest+json"
    return "application/octet-stream"


# --- auth -----------------------------------------------------------------


def _secret(config: RelayConfig) -> bytes:
    return ((config.relay_secret or config.web_token) or "relay-web").encode("utf-8")


def _sign(config: RelayConfig, exp: int) -> str:
    msg = f"{config.web_user}:{exp}".encode("utf-8")
    return hmac.new(_secret(config), msg, hashlib.sha256).hexdigest()


def issue_cookie(config: RelayConfig) -> str:
    exp = int(time.time()) + COOKIE_MAX_AGE
    return f"{exp}.{_sign(config, exp)}"


def verify_cookie(config: RelayConfig, raw: str) -> str | None:
    """Return the user for a valid, unexpired cookie value, else None."""
    if not raw or "." not in raw:
        return None
    exp_part, _, sig = raw.partition(".")
    try:
        exp = int(exp_part)
    except ValueError:
        return None
    if exp < time.time():
        return None
    if not hmac.compare_digest(sig, _sign(config, exp)):
        return None
    return config.web_user


def cookie_from_header(header: str, name: str = COOKIE_NAME) -> str:
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value.strip()
    return ""


_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "")


def _forwarded_proto(handler: BaseHTTPRequestHandler) -> str:
    raw = handler.headers.get("X-Forwarded-Proto") or ""
    return raw.split(",")[0].strip().lower()


def _secure_for(handler: BaseHTTPRequestHandler, config: RelayConfig) -> bool:
    if not config.web_secure:
        return False
    xfp = _forwarded_proto(handler)
    if xfp:
        return xfp == "https"
    host = (handler.headers.get("Host") or "").split(":")[0].strip().lower()
    return host not in _LOOPBACK_HOSTS


def _cookie_header(config: RelayConfig, value: str, *, secure: bool) -> str:
    parts = [
        f"{COOKIE_NAME}={value}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={COOKIE_MAX_AGE}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def handle_login(handler: BaseHTTPRequestHandler, body: dict) -> None:
    config = handler._cfg()  # type: ignore[attr-defined]
    if not web_enabled(config):
        handler._write_json({"ok": False, "error": "web surface disabled"}, 404)  # type: ignore[attr-defined]
        return
    token = str(body.get("token") or "").strip()
    if not token or not hmac.compare_digest(token, config.web_token):
        handler._write_json({"ok": False, "error": "invalid token"}, 401)  # type: ignore[attr-defined]
        return
    value = issue_cookie(config)
    payload = json.dumps({"ok": True, "user": config.web_user}).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header(
        "Set-Cookie",
        _cookie_header(config, value, secure=_secure_for(handler, config)),
    )
    handler.end_headers()
    handler.wfile.write(payload)


def handle_token_query(handler: BaseHTTPRequestHandler, token: str) -> bool:
    """``/?token=<web_token>`` — mint the cookie and redirect to ``/``.

    ``klanker serve`` prints this URL when it auto-generates the token, so a
    pasted link has to be the whole login flow: one GET, one Set-Cookie, one
    redirect that strips the token from the address bar.
    """
    config = handler._cfg()  # type: ignore[attr-defined]
    if not web_enabled(config) or not token:
        return False
    if not hmac.compare_digest(token, config.web_token):
        return False
    value = issue_cookie(config)
    handler.send_response(302)
    handler.send_header("Location", "/")
    handler.send_header(
        "Set-Cookie",
        _cookie_header(config, value, secure=_secure_for(handler, config)),
    )
    handler.end_headers()
    return True


def handle_logout(handler: BaseHTTPRequestHandler) -> None:
    payload = json.dumps({"ok": True}).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header(
        "Set-Cookie",
        f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
    )
    handler.end_headers()
    handler.wfile.write(payload)


def handle_status(handler: BaseHTTPRequestHandler) -> None:
    config = handler._cfg()  # type: ignore[attr-defined]
    raw = cookie_from_header(handler.headers.get("Cookie") or "")
    user = verify_cookie(config, raw) if web_enabled(config) else None
    is_secure = _secure_for(handler, config)
    host = (handler.headers.get("Host") or "").split(":")[0].strip().lower()
    plain_http_non_loopback = (
        _forwarded_proto(handler) != "https" and host not in _LOOPBACK_HOSTS
    )
    cookie_warning = ""
    if is_secure and plain_http_non_loopback:
        cookie_warning = (
            "Relay issues Secure cookies but this request reached it over "
            "plain HTTP on a non-loopback host; browsers drop the cookie "
            "and login loops. Expose the relay over HTTPS (for example "
            "`tailscale serve`) or restart with RELAY_WEB_SECURE=0."
        )
    handler._write_json(  # type: ignore[attr-defined]
        {
            "ok": True,
            "authenticated": bool(user),
            "user": user or "",
            "enabled": web_enabled(config),
            "secure_cookie": is_secure,
            "cookie_warning": cookie_warning,
        },
        200,
    )


# --- static ---------------------------------------------------------------


def _mime(path: Path) -> str:
    return _MIME.get(path.suffix.lower(), "application/octet-stream")


def serve_static(handler: BaseHTTPRequestHandler, path: str) -> bool:
    """Serve one file from the static root. False when the path is not a file."""
    config = handler._cfg()  # type: ignore[attr-defined]
    root = static_root(config)
    if root is None or not root.is_dir():
        return False
    rel = path.strip("/") or "index.html"
    if rel.endswith("/"):
        rel += "index.html"
    target = (root / rel).resolve()
    if target != root and not target.is_relative_to(root):
        return False
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        return False
    try:
        data = target.read_bytes()
    except OSError:
        return False
    cache = "no-cache" if target.suffix.lower() in _NO_CACHE else "public, max-age=86400"
    handler.send_response(200)
    handler.send_header("Content-Type", _mime(target))
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", cache)
    handler.end_headers()
    handler.wfile.write(data)
    return True


# --- chat -----------------------------------------------------------------


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _status_text(raw: str) -> str:
    """Humanize one stderr line, keeping the job name when the label is generic."""
    from .telegram_format import humanize_status

    line = humanize_status(raw)
    if not line:
        return ""
    if line.startswith("Step"):
        cleaned = (raw or "").strip()
        for prefix in ("[*] Running ", "[*] ", "[+] ", "[-] "):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()
                break
        return cleaned or line
    return line


def handle_chat_stream(handler: BaseHTTPRequestHandler, body: dict) -> None:
    config = handler._cfg()  # type: ignore[attr-defined]
    text = str(body.get("text") or "").strip()
    raw_attachments = body.get("attachments")
    if not text and not raw_attachments:
        handler._write_json({"ok": False, "error": "text is required"}, 400)  # type: ignore[attr-defined]
        return

    run = str(body.get("run") or "").strip() or uuid.uuid4().hex
    with _ACTIVE_LOCK:
        existing = _ACTIVE.get(run)
    if existing is not None:
        # Same run id re-posted (client retry after a dropped stream):
        # never start a second turn -- follow the one already running.
        _send_sse_headers(handler)
        try:
            _follow_entry(handler, existing)
        except (BrokenPipeError, ConnectionResetError, OSError):
            log.info("web stream client disconnected (run %s)", run)
        return
    entry = _new_run_entry()

    ws_ref = str(body.get("workspace") or "").strip()
    persona_ref = str(body.get("persona") or "").strip()
    ws = None
    if ws_ref:
        from .turn import resolve_workspace_id

        try:
            ws = resolve_workspace_id(ws_ref)
        except ValueError as exc:
            handler._write_json({"ok": False, "error": str(exc)}, 400)  # type: ignore[attr-defined]
            return
    if persona_ref:
        from .turn import valid_id

        if not valid_id(persona_ref):
            handler._write_json({"ok": False, "error": f"invalid persona id: {persona_ref!r}"}, 400)  # type: ignore[attr-defined]
            return

    attach_paths: tuple[str, ...] = ()
    attachment_notes: list[str] = []
    if raw_attachments:
        from .attachments import AttachmentRejected, apply_notes, materialize_for_turn

        try:
            attach_paths = tuple(materialize_for_turn(
                raw_attachments,
                config=config,
                workspace_id=ws.id if ws is not None else "",
                notes=attachment_notes,
            ))
        except AttachmentRejected:
            handler._write_json({"ok": False, "error": "attachment path outside allowed directory"}, 400)  # type: ignore[attr-defined]
            return
        if attachment_notes:
            text = apply_notes(text, attachment_notes)
        if not attach_paths and not str(body.get("text") or "").strip():
            handler._write_json(  # type: ignore[attr-defined]
                {"ok": False, "error": "; ".join(attachment_notes) or "attachment rejected"},
                400,
            )
            return

    if attachment_notes:
        entry["pending_notes"] = [
            {"type": "delta", "text": "\n\n".join(attachment_notes) + "\n\n"}
        ]
    entry["meta"] = {
        "session": str(body.get("session") or "").strip(),
        "workspace": ws_ref,
        "persona": persona_ref,
        "user": config.web_user,
    }
    params = TurnParams(
        channel="web",
        user=config.web_user,
        message=text or ("(attachment)" if attach_paths else ""),
        session=str(body.get("session") or "").strip(),
        new_session=bool(body.get("new_session")),
        provider=str(body.get("provider") or "").strip(),
        model=str(body.get("model") or "").strip(),
        persona=persona_ref,
        project=str(body.get("project") or "").strip(),
        workspace=ws_ref,
        workdir=str(body.get("workdir") or "").strip(),
        attachments=attach_paths,
    )

    # The turn runs in its own thread and publishes into the run entry;
    # this request only follows the entry (like /v1/runs/stream). A
    # dropped client socket (mobile background, network switch) detaches
    # the reader only -- the turn keeps running, stays listed in _ACTIVE
    # until its process ends, and can be re-attached. Only
    # POST /v1/chat/stop cancels.
    with _ACTIVE_LOCK:
        _ACTIVE[run] = entry  # registered only once the turn really starts
    _start_run_driver(run, entry, params, config)
    _send_sse_headers(handler)

    try:
        _follow_entry(handler, entry)
    except (BrokenPipeError, ConnectionResetError, OSError):
        log.info("web stream client disconnected (run %s); turn continues", run)


def _send_sse_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache, no-transform")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "close")
    handler.end_headers()


def _event_payload(entry: dict[str, Any], event: Any) -> dict[str, Any] | None:
    """Map one loop StreamEvent to the SSE payload (None = drop)."""
    if event.type == "keepalive":
        return None
    if event.type == "status":
        line = _status_text(event.text)
        return {"type": "status", "text": line} if line else None
    if event.type == "delta":
        return {"type": "delta", "text": event.text}
    if event.type == "trailer":
        if event.session:
            entry["meta"]["session"] = event.session
        return {
            "type": "trailer",
            "session": event.session,
            "user_id": event.user_id,
            "alias": event.alias,
            "returncode": event.returncode,
        }
    return {"type": "error", "text": event.text or "turn failed"}


def _drive_run(run: str, entry: dict[str, Any], params: Any, config: Any) -> None:
    """Run one turn to completion, publishing every event into ``entry``.

    Independent of any HTTP reader: the loop generator is consumed here,
    so a client disconnect can never close it (closing it would kill the
    turn's process tree in its ``finally``).
    """
    try:
        _publish(entry, {"type": "start", "run": run})
        for note in entry.pop("pending_notes", []) or []:
            _publish(entry, note)
        for event in iter_loop_stream_live(params, config, cancel=entry["cancel"]):
            payload = _event_payload(entry, event)
            if payload is not None:
                _publish(entry, payload)
    except Exception as exc:  # noqa: BLE001
        log.exception("web turn failed (run %s)", run)
        _publish(entry, {"type": "error", "text": str(exc) or exc.__class__.__name__})
    finally:
        with entry["cond"]:
            entry["done"] = True
            entry["done_at"] = time.monotonic()
            entry["cond"].notify_all()
        _schedule_reap(run, entry)


def _start_run_driver(run: str, entry: dict[str, Any], params: Any, config: Any) -> threading.Thread:
    t = threading.Thread(
        target=_drive_run, args=(run, entry, params, config),
        name=f"web-turn-{run[:8]}", daemon=True,
    )
    entry["thread"] = t
    t.start()
    return t


# A finished run stays attachable this long so a client that lost its
# stream right before the end can still fetch the tail + trailer.
_DONE_GRACE_SEC = 120.0


def _schedule_reap(run: str, entry: dict[str, Any]) -> None:
    def _reap() -> None:
        with _ACTIVE_LOCK:
            if _ACTIVE.get(run) is entry:
                _ACTIVE.pop(run, None)

    if _DONE_GRACE_SEC <= 0:
        _reap()
        return
    timer = threading.Timer(_DONE_GRACE_SEC, _reap)
    timer.daemon = True
    timer.start()


def _follow_entry(handler: BaseHTTPRequestHandler, entry: dict[str, Any], index: int = 0) -> None:
    """Write every published payload of ``entry`` to the client until done."""
    cond = entry["cond"]
    while True:
        ping = False
        with cond:
            pending = entry["events"][index:]
            index = len(entry["events"])
            if not pending and not entry["done"]:
                cond.wait(timeout=_KEEPALIVE_WAIT_SEC)
                pending = entry["events"][index:]
                index = len(entry["events"])
                ping = not pending and not entry["done"]
            finished = entry["done"] and index >= len(entry["events"])
        if ping:
            handler.wfile.write(b": ping\n\n")
            handler.wfile.flush()
            continue
        for payload in pending:
            handler.wfile.write(_sse(payload))
            handler.wfile.flush()
        if finished:
            break


_KEEPALIVE_WAIT_SEC = 12.0


def handle_chat_stop(handler: BaseHTTPRequestHandler, body: dict) -> None:
    run = str(body.get("run") or "").strip()
    stopped: list[str] = []
    with _ACTIVE_LOCK:
        for key, entry in list(_ACTIVE.items()):
            if run and key != run:
                continue
            entry["cancel"].set()
            stopped.append(key)
    handler._write_json({"ok": True, "stopped": stopped}, 200)  # type: ignore[attr-defined]


def active_runs() -> list[str]:
    """Runs whose turn is still executing (finished runs linger only for
    re-attach and are not listed)."""
    with _ACTIVE_LOCK:
        return sorted(k for k, e in _ACTIVE.items() if not e.get("done"))
