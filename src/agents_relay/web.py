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
from .turn import TurnParams, iter_loop_stream_live, turn_directive_error

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

# fn(run_id, entry) fires once the driver thread finishes the turn —
# the release point for session leases held over an async run
# (klanker#148). entry["meta"] carries session/workspace.
run_done_hook = None

# fn(run_id, entry, text): optional host-app mirror for steer text (for
# example workspace inbox/traces). Failures are logged and never break the
# running turn or the SSE steer signal.
steer_record_hook = None

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
        "steer_queue": [],
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
            entry = _ACTIVE.get(run)
            # An explicit workspace filter scopes a direct run lookup
            # too — run ids are unguessable but a ws-bound caller must
            # never attach across its boundary (agents-relay#44).
            if entry is not None and workspace and \
                    (entry.get("meta") or {}).get("workspace") != workspace:
                return run, None
            return run, entry
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


def _write_steer_pipe(entry: dict[str, Any], text: str) -> None:
    """Best-effort stdin steer for runtimes that expose a writable pipe.

    Current runner-loop children do not expose one. A future embedder may
    attach ``entry["stdin"]``; closed or unsupported pipes degrade to a log
    line, never a failed turn or failed inject request.
    """
    pipe = entry.get("stdin")
    if pipe is None:
        return
    try:
        writable = getattr(pipe, "writable", None)
        if callable(writable) and not writable():
            return
        try:
            pipe.write(text + "\n")
        except TypeError:
            pipe.write((text + "\n").encode("utf-8"))
        flush = getattr(pipe, "flush", None)
        if callable(flush):
            flush()
    except (BrokenPipeError, ConnectionResetError, OSError, ValueError, TypeError):
        log.info("steer stdin unavailable for active run")


def steer_active(session: str, text: str, *, workspace: str = "",
                 persona: str = "") -> tuple[str, dict[str, Any] | None]:
    """Append steer text to the active run and fan it out as typed SSE.

    Returns ``(run_id, entry)``; ``entry`` is None when no matching run is
    currently executing. Finished runs lingering for re-attach are not
    steerable.
    """
    clean_session = (session or "").strip()
    clean_text = (text or "").strip()
    if not clean_session or not clean_text:
        return "", None
    run_id, entry = _find_run(
        session=clean_session, workspace=workspace, persona=persona)
    if entry is None:
        return run_id, None
    cond = entry["cond"]
    with cond:
        if entry.get("done"):
            return run_id, None
        entry.setdefault("steer_queue", []).append({
            "text": clean_text,
            "session": clean_session,
            "workspace": workspace,
            "persona": persona,
            "ts": time.time(),
        })
    payload = {"type": "steer", "text": clean_text, "session": clean_session}
    _publish(entry, payload)
    _write_steer_pipe(entry, clean_text)
    if steer_record_hook is not None:
        try:
            steer_record_hook(run_id, entry, clean_text)
        except Exception as exc:  # noqa: BLE001 - recording is best-effort
            log.warning("steer record hook failed for run %s: %s", run_id, exc)
    return run_id, entry


# Revocation teardown (agents-relay#44): device-tagged stream attaches
# die with the credential — DELETE /v1/hub/devices/<id> fires
# disconnect_stream_tag alongside channel disconnect + remote delete.
_STREAM_TAGS_LOCK = threading.Lock()
_STREAM_TAGS: dict[str, list[tuple[threading.Event, threading.Condition]]] = {}


def register_stream_tag(tag: str, cancel: threading.Event,
                        cond: threading.Condition) -> None:
    t = (tag or "").strip()
    if not t:
        return
    with _STREAM_TAGS_LOCK:
        _STREAM_TAGS.setdefault(t, []).append((cancel, cond))


def unregister_stream_tag(tag: str, cancel: threading.Event) -> None:
    t = (tag or "").strip()
    if not t:
        return
    with _STREAM_TAGS_LOCK:
        kept = [row for row in _STREAM_TAGS.get(t, []) if row[0] is not cancel]
        if kept:
            _STREAM_TAGS[t] = kept
        else:
            _STREAM_TAGS.pop(t, None)


def disconnect_stream_tag(tag: str) -> int:
    """Kill every stream attach bound to ``tag``; returns how many died."""
    with _STREAM_TAGS_LOCK:
        rows = _STREAM_TAGS.pop((tag or "").strip(), [])
    for cancel, cond in rows:
        cancel.set()
        with cond:
            cond.notify_all()
    return len(rows)


def handle_runs_attach(handler: BaseHTTPRequestHandler, query: dict[str, Any],
                       *, tag: str = "") -> None:
    """SSE attach to a running turn: replay buffered events, then live fan-out.

    ``tag`` binds the attach to a caller identity (device id) so
    revocation can tear it down (agents-relay#44)."""
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
    cancel = threading.Event()
    register_stream_tag(tag, cancel, cond)
    try:
        handler.wfile.write(_sse({"type": "attach", "run": run_id, "meta": entry.get("meta") or {}}))
        handler.wfile.flush()
        while not cancel.is_set():
            ping = False
            with cond:
                pending = entry["events"][index:]
                index = len(entry["events"])
                if not pending and not entry["done"] and not cancel.is_set():
                    cond.wait(timeout=15.0)
                    pending = entry["events"][index:]
                    index = len(entry["events"])
                    ping = not pending and not entry["done"]
                finished = entry["done"] and index >= len(entry["events"])
            if cancel.is_set():
                break
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
    finally:
        unregister_stream_tag(tag, cancel)


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
    """X-Forwarded-Proto only counts behind a trusted proxy —
    RELAY_TRUST_PROXY=1 opts in; otherwise the client can spoof its
    own scheme (agents-relay#42)."""
    import os

    if os.environ.get("RELAY_TRUST_PROXY", "").strip().lower() not in (
            "1", "true", "yes", "on"):
        return ""
    raw = handler.headers.get("X-Forwarded-Proto") or ""
    return raw.split(",")[0].strip().lower()


def _host_only(host_header: str) -> str:
    """Bare host token from a Host header — ``[::1]:8787`` must parse to
    ``::1``, not ``[`` (agents-relay#44)."""
    h = (host_header or "").strip()
    if h.startswith("["):
        end = h.find("]")
        return (h[1:end] if end >= 0 else h[1:]).strip().lower()
    if h.count(":") == 1:
        h = h.rsplit(":", 1)[0]
    return h.strip().lower()


def _secure_for(handler: BaseHTTPRequestHandler, config: RelayConfig) -> bool:
    if not config.web_secure:
        return False
    xfp = _forwarded_proto(handler)
    if xfp:
        return xfp == "https"
    return _host_only(handler.headers.get("Host") or "") not in _LOOPBACK_HOSTS


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


_LOGIN_WINDOW_SEC = 60.0
_LOGIN_MAX_FAILS = 5
_login_lock = threading.Lock()
_login_fails: dict[str, list[float]] = {}


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    try:
        return str(handler.client_address[0] or "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def _prune_login_fails(now: float) -> None:
    """Drop stale rows for ALL ips — the dict must not grow with every
    source ip that ever probed the token oracle (agents-relay#44)."""
    cutoff = now - _LOGIN_WINDOW_SEC
    for ip in list(_login_fails):
        kept = [t for t in _login_fails[ip] if t > cutoff]
        if kept:
            _login_fails[ip] = kept
        else:
            _login_fails.pop(ip, None)


def _login_allowed(ip: str) -> bool:
    """Sliding-window login throttle — a relay reachable over the
    network must not be a token oracle (agents-relay#42)."""
    now = time.monotonic()
    with _login_lock:
        _prune_login_fails(now)
        return len(_login_fails.get(ip, ())) < _LOGIN_MAX_FAILS


def _login_failed(ip: str) -> None:
    now = time.monotonic()
    with _login_lock:
        _prune_login_fails(now)
        _login_fails.setdefault(ip, []).append(now)


def reset_login_limits() -> None:
    with _login_lock:
        _login_fails.clear()


def handle_login(handler: BaseHTTPRequestHandler, body: dict) -> None:
    config = handler._cfg()  # type: ignore[attr-defined]
    if not web_enabled(config):
        handler._write_json({"ok": False, "error": "web surface disabled"}, 404)  # type: ignore[attr-defined]
        return
    ip = _client_ip(handler)
    if not _login_allowed(ip):
        handler._write_json({"ok": False, "error": "rate limited"}, 429)  # type: ignore[attr-defined]
        return
    token = str(body.get("token") or "").strip()
    if not token or not hmac.compare_digest(token, config.web_token):
        _login_failed(ip)
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
    # The query-token path was an unthrottled oracle — it counts the same
    # login budget as POST /v1/web/login (agents-relay#44).
    ip = _client_ip(handler)
    if not _login_allowed(ip):
        handler._write_json({"ok": False, "error": "rate limited"}, 429)  # type: ignore[attr-defined]
        return True
    if not hmac.compare_digest(token, config.web_token):
        _login_failed(ip)
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
    host = _host_only(handler.headers.get("Host") or "")
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


def handle_chat_stream(handler: BaseHTTPRequestHandler, body: dict) -> bool:
    """Start-or-follow the run and stream SSE; returns True when a turn
    is (or stays) running — False on a validation 400 so lease holders
    can release (agents-relay#44)."""
    config = handler._cfg()  # type: ignore[attr-defined]
    directive_error = turn_directive_error(body)
    if directive_error:
        handler._write_json({"ok": False, "error": directive_error}, 400)  # type: ignore[attr-defined]
        return False
    text = str(body.get("text") or "").strip()
    raw_attachments = body.get("attachments")
    if not text and not raw_attachments:
        handler._write_json({"ok": False, "error": "text is required"}, 400)  # type: ignore[attr-defined]
        return False

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
        return True
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
            return False
    if persona_ref:
        from .turn import valid_id

        if not valid_id(persona_ref):
            handler._write_json({"ok": False, "error": f"invalid persona id: {persona_ref!r}"}, 400)  # type: ignore[attr-defined]
            return False

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
            return False
        if attachment_notes:
            text = apply_notes(text, attachment_notes)
        if not attach_paths and not str(body.get("text") or "").strip():
            handler._write_json(  # type: ignore[attr-defined]
                {"ok": False, "error": "; ".join(attachment_notes) or "attachment rejected"},
                400,
            )
            return False

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
        turn_mode=str(body.get("turn_mode") or "").strip(),
        reasoning=str(body.get("reasoning") or "").strip(),
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
    return True


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
    if event.type == "thought":
        d = getattr(event, "data", None) or {}
        txt = event.text or str(d.get("text") or "")
        return {"type": "thought", "text": txt} if txt else None
    if event.type == "tool_start":
        d = getattr(event, "data", None) or {}
        return {
            "type": "tool_start",
            "id": str(d.get("id") or ""),
            "tool": str(d.get("tool") or ""),
            "args_preview": str(d.get("args_preview") or ""),
        }
    if event.type == "tool_end":
        d = getattr(event, "data", None) or {}
        return {
            "type": "tool_end",
            "id": str(d.get("id") or ""),
            "tool": str(d.get("tool") or ""),
            "ok": bool(d.get("ok", True)),
            "duration_ms": d.get("duration_ms"),
            "result_preview": str(d.get("result_preview") or ""),
            "error": d.get("error"),
        }
    if event.type == "tier":
        d = getattr(event, "data", None) or {}
        tier = str(d.get("tier") or event.text or "")
        if not tier:
            return None
        return {
            "type": "tier",
            "tier": tier,
            "mode": str(d.get("mode") or ""),
        }
    if event.type == "usage":
        d = getattr(event, "data", None) or {}
        return {
            "type": "usage",
            "tokens_in": d.get("tokens_in"),
            "tokens_out": d.get("tokens_out"),
            "est_cost_usd": d.get("est_cost_usd"),
            "model": str(d.get("model") or ""),
        }
    if event.type == "delta":
        return {"type": "delta", "text": event.text}
    if event.type == "error":
        text = event.text or "turn failed"
        return {"type": "error", "text": text, "error": text}
    if event.type == "trailer":
        if event.session:
            entry["meta"]["session"] = event.session
        payload = {
            "type": "trailer",
            "session": event.session,
            "user_id": event.user_id,
            "alias": event.alias,
            "returncode": event.returncode,
        }
        error = str((getattr(event, "data", None) or {}).get("error") or "")
        if error:
            payload["error"] = error
        return payload
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
        if run_done_hook is not None:
            try:
                run_done_hook(run, entry)
            except Exception:  # noqa: BLE001 - lease release never kills reap
                pass
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
    # A workspace filter scopes the sweep — a ws-bound caller cancels
    # only its own runs, never the whole host (agents-relay#44).
    ws_filter = str(body.get("workspace") or "").strip()
    stopped: list[str] = []
    with _ACTIVE_LOCK:
        for key, entry in list(_ACTIVE.items()):
            if run and key != run:
                continue
            if ws_filter and \
                    (entry.get("meta") or {}).get("workspace") != ws_filter:
                continue
            entry["cancel"].set()
            stopped.append(key)
    handler._write_json({"ok": True, "stopped": stopped}, 200)  # type: ignore[attr-defined]


def active_runs() -> list[str]:
    """Runs whose turn is still executing (finished runs linger only for
    re-attach and are not listed)."""
    with _ACTIVE_LOCK:
        return sorted(k for k, e in _ACTIVE.items() if not e.get("done"))
