"""Relay-held remote registry and authenticated proxy.

Remote metadata lives in ``<host>/clients/remotes.json`` as
``{id: {base_url, label}}``. Bearer tokens live exclusively in the Host Secret
Vault (``REMOTE_<ID>_TOKEN``, ``scope="host"``) and never leave the relay:
browser/PWA clients mount a remote via ``/v1/remote/<id>/<path>`` and the
relay injects the Bearer. A 1-release fallback reads legacy
``<ws>/relay/remotes.json`` when ``<host>/clients/remotes.json`` is absent.
"""

from __future__ import annotations

import logging
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from agents_workspace import Host, delete_secret, resolve_secret, write_secret

from .config import RelayConfig
from .state import legacy_relay_dirs, read_json, write_json_atomic
from .turn import valid_id

log = logging.getLogger(__name__)

# Query keys never forwarded upstream — they carry OUR relay's secret.
_STRIP_QS = {"secret", "token"}
_STREAM_PATHS = ("/v1/chat/stream", "/v1/runs/stream", "/v1/stream")


def validate_remote_base_url(url: str) -> str:
    """Validate a remote ``base_url``: http(s) scheme, host, no creds.

    Returns the normalized URL (trailing slash stripped); raises
    ``ValueError`` on anything else — SSRF/path-escape guard shared by
    ``upsert_remote`` and ``handle_proxy``.
    """
    raw = (url or "").strip().rstrip("/")
    if not raw:
        raise ValueError("base_url must be an http(s) origin")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("base_url must be an http(s) origin")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not embed credentials")
    return raw


def _normalize_proxy_rest(rest: str) -> str:
    """Normalize the upstream path; only ``/v1/*`` and ``/health`` pass.

    Uses ``posixpath.normpath`` (never ``os.path.normpath`` — backslashes
    would sneak through on Windows) and rejects escapes BEFORE the
    allowlist check.
    """
    raw = rest if rest.startswith("/") else "/" + rest
    if "\x00" in raw or "\\" in raw:
        raise ValueError("invalid proxy path")
    norm = posixpath.normpath(raw)
    if norm != "/health" and not norm.startswith("/v1/"):
        raise ValueError(f"proxy path not allowed: {norm}")
    return norm


def _path() -> Path:
    return Host.from_env().clients / "remotes.json"


def _secret_key(rid: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", rid.strip()).upper()
    return f"REMOTE_{clean}_TOKEN"


def _secret_key_candidates(rid: str) -> tuple[str, ...]:
    primary = _secret_key(rid)
    raw = f"REMOTE_{rid.strip()}_TOKEN"
    return (primary,) if raw == primary else (primary, raw)


def _read_secret(rid: str) -> str:
    for key in _secret_key_candidates(rid):
        val = resolve_secret(key, scope="host")
        if val:
            return val
    return ""


def _write_secret(rid: str, value: str) -> None:
    write_secret(_secret_key(rid), value, scope="host")


def _delete_secret(rid: str) -> None:
    for key in _secret_key_candidates(rid):
        delete_secret(key, scope="host")


def _load_legacy_remotes(config: RelayConfig) -> dict:
    for legacy_dir in legacy_relay_dirs(config):
        legacy_path = legacy_dir / "remotes.json"
        if not legacy_path.is_file():
            continue
        data = read_json(legacy_path) or {}
        remotes = data.get("remotes")
        if isinstance(remotes, dict):
            warnings.warn(
                f"legacy remotes file at {legacy_path} is deprecated; relocate to {_path()}",
                DeprecationWarning,
                stacklevel=2,
            )
            return remotes
    return {}


def load_remotes(config: RelayConfig) -> dict:
    path = _path()
    if path.is_file():
        data = read_json(path) or {}
        raw = data.get("remotes")
        if not isinstance(raw, dict):
            return {}
        out: dict = {}
        for rid, meta in raw.items():
            if not isinstance(meta, dict):
                continue
            out[rid] = {
                "base_url": str(meta.get("base_url") or ""),
                "label": str(meta.get("label") or ""),
                "secret": _read_secret(str(rid)),
            }
        return out

    legacy = _load_legacy_remotes(config)
    out = {}
    for rid, meta in legacy.items():
        if not isinstance(meta, dict):
            continue
        secret = str(meta.get("secret") or "").strip() or _read_secret(str(rid))
        out[rid] = {
            "base_url": str(meta.get("base_url") or ""),
            "label": str(meta.get("label") or ""),
            "secret": secret,
        }
    return out


def _store(remotes: dict) -> None:
    meta_only = {
        rid: {
            "base_url": str(info.get("base_url") or ""),
            "label": str(info.get("label") or ""),
        }
        for rid, info in remotes.items()
        if isinstance(info, dict)
    }
    write_json_atomic(_path(), {"remotes": meta_only})


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
    base_url = str(body.get("base_url") or "")
    if not rid or not valid_id(rid):
        return None, f"invalid remote id: {rid!r}"
    try:
        base_url = validate_remote_base_url(base_url)
    except ValueError as exc:
        return None, str(exc)
    migrating = not _path().is_file()
    remotes = load_remotes(config)
    if migrating:
        for other_id, info in remotes.items():
            if other_id != rid and isinstance(info, dict):
                other_secret = str(info.get("secret") or "").strip()
                if other_secret and not _read_secret(str(other_id)):
                    _write_secret(str(other_id), other_secret)
    prev = remotes.get(rid) or {}
    new_secret = str(body.get("secret") or "").strip()
    prev_secret = str(prev.get("secret") or "").strip()
    secret = new_secret or prev_secret
    if secret and (new_secret or not _read_secret(rid)):
        _write_secret(rid, secret)
    label = str(body.get("label") or "").strip()
    remotes[rid] = {
        "base_url": base_url,
        "label": label,
    }
    _store(remotes)
    return {
        "id": rid,
        "base_url": base_url,
        "label": label,
        "has_secret": bool(secret),
    }, ""


def delete_remote(config: RelayConfig, rid: str) -> bool:
    migrating = not _path().is_file()
    remotes = load_remotes(config)
    if rid not in remotes:
        return False
    if migrating:
        for other_id, info in remotes.items():
            if other_id != rid and isinstance(info, dict):
                other_secret = str(info.get("secret") or "").strip()
                if other_secret and not _read_secret(str(other_id)):
                    _write_secret(str(other_id), other_secret)
    del remotes[rid]
    _store(remotes)
    _delete_secret(rid)
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
    try:
        base_url = validate_remote_base_url(str(remote.get("base_url") or ""))
        rest = _normalize_proxy_rest(rest)
    except ValueError as exc:
        handler._write_json({"ok": False, "error": str(exc)}, 400)  # type: ignore[attr-defined]
        return
    qs = _forward_query(handler.path or "")
    url = f"{base_url}{rest}{'?' + qs if qs else ''}"

    length = int(handler.headers.get("Content-Length") or "0")
    data = handler.rfile.read(length) if length else None
    from . import __version__

    req = urllib.request.Request(
        url,
        data=data,
        method=handler.command,
        headers={"User-Agent": f"agents-relay/{__version__}"},
    )
    secret = str(remote.get("secret") or "").strip()
    if secret:
        req.add_header("Authorization", f"Bearer {secret}")
    ctype = handler.headers.get("Content-Type")
    if ctype and data is not None:
        req.add_header("Content-Type", ctype)
    accept = (handler.headers.get("Accept") or "").strip()
    if accept:
        req.add_header("Accept", accept)

    timeout = (
        600
        if any(rest.startswith(p) for p in _STREAM_PATHS)
        else 30
    )
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
