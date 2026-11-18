"""Relay-held remote registry and authenticated proxy.

Remotes live in ``<state>/remotes.json`` as ``{id: {base_url, secret,
label}}``. The remote secret never leaves the relay: browser/PWA clients
mount a remote via ``/v1/remote/<id>/<path>`` and the relay injects the
Bearer. Clients therefore only ever talk to their own origin — remote
relays without browser CORS work through the mount, and no secret is held
in browser storage.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

from .config import RelayConfig
from .state import read_json, resolve_state_dir, write_json_atomic
from .turn import valid_id

log = logging.getLogger(__name__)

# Query keys never forwarded upstream — they carry OUR relay's secret.
_STRIP_QS = {"secret", "token"}
_STREAM_PATH = "/v1/chat/stream"


def _path(config: RelayConfig):
    return resolve_state_dir(config) / "remotes.json"


def load_remotes(config: RelayConfig) -> dict:
    data = read_json(_path(config)) or {}
    remotes = data.get("remotes")
    return remotes if isinstance(remotes, dict) else {}


def _store(config: RelayConfig, remotes: dict) -> None:
    write_json_atomic(_path(config), {"remotes": remotes})


def list_public(config: RelayConfig) -> list[dict]:
    rows = []
    for rid, r in sorted(load_remotes(config).items()):
        rows.append({
            "id": rid,
            "base_url": r.get("base_url") or "",
            "label": r.get("label") or "",
            "has_secret": bool(r.get("secret")),
        })
    return rows


def upsert_remote(config: RelayConfig, body: dict) -> tuple[dict | None, str]:
    rid = str(body.get("id") or "").strip()
    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    if not rid or not valid_id(rid):
        return None, f"invalid remote id: {rid!r}"
    if not base_url.startswith(("http://", "https://")):
        return None, "base_url must be an http(s) origin"
    remotes = load_remotes(config)
    prev = remotes.get(rid) or {}
    remotes[rid] = {
        "base_url": base_url,
        "label": str(body.get("label") or "").strip(),
        # Empty secret keeps the stored one — clients never echo it back.
        "secret": str(body.get("secret") or "").strip() or prev.get("secret") or "",
    }
    _store(config, remotes)
    return {"id": rid, "base_url": base_url, "label": remotes[rid]["label"],
            "has_secret": bool(remotes[rid]["secret"])}, ""


def delete_remote(config: RelayConfig, rid: str) -> bool:
    remotes = load_remotes(config)
    if rid not in remotes:
        return False
    del remotes[rid]
    _store(config, remotes)
    return True


def _forward_query(raw_path: str) -> str:
    qs = urllib.parse.urlsplit(raw_path).query
    pairs = urllib.parse.parse_qsl(qs, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k.lower() not in _STRIP_QS]
    return urllib.parse.urlencode(kept)


def handle_proxy(handler: BaseHTTPRequestHandler, rid: str, rest: str) -> None:
    """Forward ``<method> <rest>`` to the remote with its stored Bearer.

    Response status and body stream through verbatim — SSE included.
    """
    cfg = handler._cfg()  # type: ignore[attr-defined]
    remote = load_remotes(cfg).get(rid)
    if remote is None:
        handler._write_json({"ok": False, "error": f"unknown remote: {rid}"}, 404)  # type: ignore[attr-defined]
        return
    base_url = str(remote.get("base_url") or "").rstrip("/")
    if not rest.startswith("/"):
        rest = "/" + rest
    qs = _forward_query(handler.path or "")
    url = f"{base_url}{rest}{'?' + qs if qs else ''}"

    length = int(handler.headers.get("Content-Length") or "0")
    data = handler.rfile.read(length) if length else None
    req = urllib.request.Request(url, data=data, method=handler.command)
    secret = str(remote.get("secret") or "").strip()
    if secret:
        req.add_header("Authorization", f"Bearer {secret}")
    ctype = handler.headers.get("Content-Type")
    if ctype and data is not None:
        req.add_header("Content-Type", ctype)

    timeout = 600 if rest.startswith(_STREAM_PATH) else 30
    try:
        upstream = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        upstream = exc  # HTTP errors still carry a real response to relay
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        handler._write_json({"ok": False, "error": f"remote unreachable: {exc}"}, 502)  # type: ignore[attr-defined]
        return

    handler.send_response(upstream.status)
    ctype = upstream.headers.get("Content-Type") or "application/octet-stream"
    handler.send_header("Content-Type", ctype)
    if ctype.startswith("text/event-stream"):
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
        handler.send_header("Connection", "close")
    handler.end_headers()
    try:
        while True:
            chunk = upstream.read(8192)
            if not chunk:
                break
            handler.wfile.write(chunk)
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        log.info("remote proxy client disconnected (%s %s)", rid, rest)
    finally:
        upstream.close()
