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

_ACTIVE: dict[str, threading.Event] = {}
_ACTIVE_LOCK = threading.Lock()


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


def _secure_for(handler: BaseHTTPRequestHandler, config: RelayConfig) -> bool:
    if not config.web_secure:
        return False
    host = (handler.headers.get("Host") or "").split(":")[0].strip().lower()
    return host not in ("localhost", "127.0.0.1", "::1", "")


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
    handler._write_json(  # type: ignore[attr-defined]
        {"ok": True, "authenticated": bool(user), "user": user or "", "enabled": web_enabled(config)},
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
    if not text:
        handler._write_json({"ok": False, "error": "text is required"}, 400)  # type: ignore[attr-defined]
        return

    run = str(body.get("run") or "").strip() or uuid.uuid4().hex
    cancel = threading.Event()
    with _ACTIVE_LOCK:
        _ACTIVE[run] = cancel

    ws_ref = str(body.get("workspace") or "").strip()
    persona_ref = str(body.get("persona") or "").strip()
    if ws_ref:
        from .turn import resolve_workspace_id

        try:
            resolve_workspace_id(ws_ref)
        except ValueError as exc:
            handler._write_json({"ok": False, "error": str(exc)}, 400)  # type: ignore[attr-defined]
            return
    if persona_ref:
        from .turn import valid_id

        if not valid_id(persona_ref):
            handler._write_json({"ok": False, "error": f"invalid persona id: {persona_ref!r}"}, 400)  # type: ignore[attr-defined]
            return

    params = TurnParams(
        channel="web",
        user=config.web_user,
        message=text,
        session=str(body.get("session") or "").strip(),
        new_session=bool(body.get("new_session")),
        provider=str(body.get("provider") or "").strip(),
        persona=persona_ref,
        project=str(body.get("project") or "").strip(),
        workspace=ws_ref,
        workdir=str(body.get("workdir") or "").strip(),
    )

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "close")
    handler.end_headers()

    try:
        handler.wfile.write(_sse({"type": "start", "run": run}))
        handler.wfile.flush()
        for event in iter_loop_stream_live(params, config, cancel=cancel):
            if event.type == "status":
                line = _status_text(event.text)
                if not line:
                    continue
                payload: dict[str, Any] = {"type": "status", "text": line}
            elif event.type == "delta":
                payload = {"type": "delta", "text": event.text}
            elif event.type == "trailer":
                payload = {
                    "type": "trailer",
                    "session": event.session,
                    "user_id": event.user_id,
                    "alias": event.alias,
                    "returncode": event.returncode,
                }
            else:
                payload = {"type": "error", "text": event.text or "turn failed"}
            handler.wfile.write(_sse(payload))
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        cancel.set()
        log.info("web stream client disconnected (run %s)", run)
    except Exception as exc:
        log.exception("web stream failed")
        try:
            handler.wfile.write(_sse({"type": "error", "text": str(exc)}))
            handler.wfile.flush()
        except Exception:
            pass
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(run, None)


def handle_chat_stop(handler: BaseHTTPRequestHandler, body: dict) -> None:
    run = str(body.get("run") or "").strip()
    stopped: list[str] = []
    with _ACTIVE_LOCK:
        for key, event in list(_ACTIVE.items()):
            if run and key != run:
                continue
            event.set()
            stopped.append(key)
    handler._write_json({"ok": True, "stopped": stopped}, 200)  # type: ignore[attr-defined]


def active_runs() -> list[str]:
    with _ACTIVE_LOCK:
        return sorted(_ACTIVE)
