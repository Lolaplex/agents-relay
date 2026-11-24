"""Per-workspace agents-store HTTP handlers and optional Radicale CalDAV config."""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .events import emit_event

_SAFE_KINDS = frozenset({"event", "task", "note", "decision"})
_SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DID_CACHE: dict[tuple[str, str], tuple[tuple[int, int] | None, str]] = {}
_DEFAULT_MIME = {
    "markdown": "text/markdown; charset=utf-8",
    "ical": "text/calendar; charset=utf-8",
    "jsoncal": "application/json; charset=utf-8",
    "html": "text/html; charset=utf-8",
}


def _load_store_modules() -> tuple[Any, Any, type] | None:
    try:
        from agents_store import db, surface
        from agents_store.contracts import WriteRefusal
    except ImportError:
        return None
    return db, surface, WriteRefusal


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _key_signature(ws: Any, slug: str) -> tuple[bool, tuple[int, int] | None]:
    """Return ``(has_keys_dir, (mtime_ns, size) | None)`` for cache validation."""
    keys_dir = getattr(ws.host, "keys", None)
    if keys_dir is None:
        return False, None
    key_file = Path(keys_dir) / f"{slug}.ed25519"
    try:
        st = key_file.stat()
    except OSError:
        return True, None
    return True, (st.st_mtime_ns, st.st_size)


def resolve_store_actor(ws: Any, *, persona: str = "") -> str:
    """Resolve trusted server-side actor DID for store mutations.

    Reads ``AGENTS_ACTOR_DID`` first, then ``agents-keys did <slug>`` for the
    trusted caller ``persona`` and ``"host"`` (never reading ``.ed25519`` key
    material, only statting ``<host>/keys/<slug>.ed25519`` for presence and
    rotation invalidation), falling back to ``host:<host_id>``.
    """
    explicit = os.environ.get("AGENTS_ACTOR_DID", "").strip()
    if explicit:
        return explicit

    host_dir = str(ws.host.dir)
    if importlib.util.find_spec("agents_keys") is not None:
        env = {**os.environ, "AGENTS_HOST_DIR": host_dir}
        candidates: list[str] = []
        p_clean = (persona or "").strip()
        if p_clean and _SAFE_SLUG.fullmatch(p_clean):
            candidates.append(p_clean)
        candidates.append("host")
        for slug in candidates:
            cache_key = (host_dir, slug)
            has_keys_dir, sig = _key_signature(ws, slug)
            if has_keys_dir and sig is None:
                _DID_CACHE.pop(cache_key, None)
                continue
            cached = _DID_CACHE.get(cache_key)
            if cached is not None and cached[0] == sig:
                if cached[1]:
                    return cached[1]
                continue
            did_val = ""
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "agents_keys", "did", slug],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    env=env,
                )
                if proc.returncode == 0:
                    first = proc.stdout.strip().splitlines()[0].strip() if proc.stdout.strip() else ""
                    if first.startswith("did:key:"):
                        did_val = first
            except Exception:
                did_val = ""
            _DID_CACHE[cache_key] = (sig, did_val)
            if did_val:
                return did_val

    return f"host:{ws.host.host_id()}"


def _open_store(db_mod: Any, ws: Any) -> Any:
    db_path = Path(ws.store_db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _refusal_dict(refusal: Any) -> dict[str, Any]:
    return {
        "wrapper": str(getattr(refusal, "wrapper", "")),
        "reason": str(getattr(refusal, "reason", "")),
        "e": getattr(refusal, "e", None),
        "a": getattr(refusal, "a", None),
        "payload": str(getattr(refusal, "payload", "")),
    }


def _not_installed() -> tuple[int, dict[str, Any]]:
    return 501, {"ok": False, "error": "agents-store not installed"}


def handle_store_items(ws: Any, query_params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    mods = _load_store_modules()
    if mods is None:
        return _not_installed()
    db_mod, surface, _ = mods

    kind = str(query_params.get("kind") or "").strip().lower()
    if kind and kind not in _SAFE_KINDS:
        return 400, {"ok": False, "error": f"invalid kind: {kind!r}"}
    status = str(query_params.get("status") or "").strip().lower()
    try:
        limit = int(query_params.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))

    conn = _open_store(db_mod, ws)
    try:
        items = surface.query_entities(
            conn,
            kind=kind or None,
            status=status or None,
            limit=limit,
        )
    finally:
        conn.close()
    return 200, {"ok": True, "workspace": ws.id, "items": items}


def handle_store_pane(
    ws: Any,
    entity_id: str,
    query_params: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    mods = _load_store_modules()
    if mods is None:
        return _not_installed()
    db_mod, surface, WriteRefusal = mods

    eid = (entity_id or "").strip()
    if not eid or "/" in eid or "\\" in eid or ".." in eid:
        return 400, {"ok": False, "error": f"invalid entity id: {entity_id!r}"}
    target = str(query_params.get("target") or "markdown").strip().lower()

    conn = _open_store(db_mod, ws)
    try:
        reg = surface.default_registry()
        res = surface.render_pane(conn, reg, eid, target=target)
    finally:
        conn.close()

    if isinstance(res, WriteRefusal):
        return 422, {"ok": False, "refusal": _refusal_dict(res)}
    out_target = str(res.get("target") or target)
    return 200, {
        "ok": True,
        "workspace": ws.id,
        "entity": eid,
        "target": out_target,
        "content": res.get("content", ""),
        "mime": str(res.get("mime") or _DEFAULT_MIME.get(target, "text/plain; charset=utf-8")),
    }


def handle_store_append(
    ws: Any,
    body: Any,
    *,
    persona: str = "",
    device_id: str = "",
) -> tuple[int, dict[str, Any]]:
    mods = _load_store_modules()
    if mods is None:
        return _not_installed()
    db_mod, surface, WriteRefusal = mods

    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "request body must be an object"}
    domain = str(body.get("domain") or "records").strip()
    if not domain:
        return 400, {"ok": False, "error": "domain is required"}
    raw_payload = body.get("payload")
    if not isinstance(raw_payload, dict):
        return 400, {"ok": False, "error": "payload must be an object"}

    clean_payload = {k: v for k, v in raw_payload.items() if k != "actor"}
    # A paired device caller signs with its own identity — the host DID
    # must never sign a device-initiated write (agents-relay#44).
    actor = (f"device:{device_id.strip()}" if (device_id or "").strip()
             else resolve_store_actor(ws, persona=persona))

    conn = _open_store(db_mod, ws)
    try:
        reg = surface.default_registry()
        res = surface.append(domain, clean_payload, actor, conn=conn, reg=reg)
    finally:
        conn.close()

    if isinstance(res, WriteRefusal):
        return 422, {"ok": False, "refusal": _refusal_dict(res)}
    entity_id = str(res)
    emit_event(
        "store.changed",
        {"action": "append", "domain": domain, "entity": entity_id},
        workspace=ws.id,
    )
    return 200, {
        "ok": True,
        "workspace": ws.id,
        "id": entity_id,
        "actor": actor,
    }


def caldav_config_for_host(host: Any | None = None) -> dict[str, Any] | None:
    """Return optional Radicale bridge configuration pointing at workspace stores."""
    if not _truthy_env("AGENTS_CALDAV_ENABLE"):
        return None
    if importlib.util.find_spec("radicale_agents_store") is None:
        return None
    from agents_workspace import Host

    h = host if host is not None else Host.from_env()
    collections = [
        {"workspace": ws.id, "store_db": str(ws.store_db)}
        for ws in h.list_workspaces()
    ]
    return {
        "enabled": True,
        "storage_type": "radicale_agents_store",
        "collections": collections,
    }


def relay_capabilities() -> dict[str, bool]:
    """Optional suite capabilities exposed by this relay instance."""
    return {
        "store": importlib.util.find_spec("agents_store") is not None,
        "voice": importlib.util.find_spec("agents_voice") is not None,
        "caldav": caldav_config_for_host() is not None,
    }
