"""Workspace + persona + session registry for the relay HTTP surface."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from agents_workspace import Host, Workspace
from agents_workspace.workspace import _slug, _valid_id

from .read_cache import relay_read_cache


WS_PERSONA_FIELDS = frozenset(
    {
        "id",
        "name",
        "system_append",
        "notes",
        "role",
        "accepts_messages",
        "allow_peers",
        "memory",
    }
)


def invalidate_workspace_read_cache() -> None:
    relay_read_cache.invalidate("workspaces")


def list_workspaces() -> list[Workspace]:
    host = Host.from_env()
    items = host.list_workspaces()
    if not items:
        host.create("default")
        items = host.list_workspaces()
    return items


def create_workspace(label: str = "", ws_id: str = "") -> Workspace:
    """Create by explicit id (preferred) or by label slug."""
    host = Host.from_env()
    ws = host.create(ws_id or _slug(label), label=label)
    invalidate_workspace_read_cache()
    return ws


def get_workspace(ws_id: str) -> Workspace:
    host = Host.from_env()
    if not host.workspace_exists(ws_id):
        raise KeyError(ws_id)
    return host.workspace(ws_id)


def select_workspace() -> Workspace:
    """AGENTS_WORKSPACE (serve-pinned) > single existing workspace."""
    import os

    return Host.from_env().select_workspace(env=dict(os.environ))


def workspace_row(ws: Workspace) -> dict:
    """Serialized workspace: base fields + per-workspace separation config."""
    from agents_workspace import read_workspace_config, workspace_config

    row = ws.to_dict()
    meta = workspace_config(ws)
    cfg = read_workspace_config(ws)
    approval = cfg.get("approval") if isinstance(cfg.get("approval"), dict) else {}
    configured_workdir = str(meta.get("workdir") or "").strip()
    if configured_workdir:
        row["workdir"] = configured_workdir
    row["workdir_lock"] = bool(meta.get("workdir_lock"))
    row["browser_profile"] = str(meta.get("browser_profile") or "")
    row["approval_mode"] = str(approval.get("mode") or "")
    return row


_PATCH_FIELDS = frozenset(
    {"label", "workdir", "workdir_lock", "browser_profile",
     "approval_mode"}
)

# Ownership/share fields (agents-relay#43) — strict write validation
# lives in Host.update_workspace; ws_member callers are refused one
# layer up (http_adapter) before this function ever runs.
_POLICY_FIELDS = frozenset(
    {"sync_policy", "sync_hosts", "default_host", "repos", "execution"}
)


def patch_workspace(ws_id: str, payload: dict) -> dict:
    """Apply a validated per-workspace config patch; returns the new row.

    Raises ``KeyError`` for unknown workspaces and ``ValueError`` for
    unknown fields, invalid workdirs, or invalid approval modes.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    unknown = sorted(str(k) for k in payload
                     if k not in _PATCH_FIELDS | _POLICY_FIELDS)
    if unknown:
        raise ValueError(f"unknown workspace fields: {', '.join(unknown)}")
    from agents_workspace import update_workspace_config

    host = Host.from_env()
    if not host.workspace_exists(ws_id):
        raise KeyError(ws_id)
    kwargs: dict = {}
    if "label" in payload:
        kwargs["label"] = str(payload["label"] or "")
    if "workdir" in payload:
        kwargs["workdir"] = payload["workdir"]
    if "workdir_lock" in payload:
        kwargs["workdir_lock"] = bool(payload["workdir_lock"])
    if "browser_profile" in payload:
        kwargs["browser_profile"] = payload["browser_profile"]
    # ownership fields pass through verbatim — update_workspace runs
    # the strict validators and raises ValueError on anything off.
    for f in _POLICY_FIELDS:
        if f in payload:
            kwargs[f] = payload[f]
    ws = host.update_workspace(ws_id, **kwargs)
    if "approval_mode" in payload:
        mode = payload["approval_mode"]
        update_workspace_config(
            ws, {"approval": {"mode": mode if mode not in ("", None) else None}}
        )
    row = workspace_row(host.workspace(ws_id))
    # Single canonical workspace.changed emit per patch — the route
    # must not re-emit with a different key shape (agents-relay#45).
    try:
        from .events import emit_event

        emit_event("workspace.changed",
                   {"action": "patch", "workspace": ws_id,
                    "fields": sorted(set(payload)
                                     & (_PATCH_FIELDS | _POLICY_FIELDS))},
                   workspace=ws_id)
    except Exception:  # noqa: BLE001
        pass
    if set(payload) & _POLICY_FIELDS:
        fn = globals().get("ws_policy_changed_fn")
        if fn is not None:
            try:
                fn(ws_id)
            except Exception:  # noqa: BLE001
                pass
    invalidate_workspace_read_cache()
    return row


# klanker serve wires share-reconcile here: fn(ws_id) runs announce/
# revoke delivery after a sync_policy/sync_hosts mutation (agents-relay#43).
ws_policy_changed_fn = None


def list_personas(ws: Workspace) -> list[dict]:
    """Persona manifests under ``<ws>/personas/<id>/persona.json``."""
    root = Path(ws.personas)
    if not root.is_dir():
        return []
    rows = []
    for entry in sorted(root.iterdir()):
        manifest = entry / "persona.json"
        if not (entry.is_dir() and manifest.is_file()):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            data.setdefault("id", entry.name)
            rows.append(data)
    return rows


def get_persona(ws: Workspace, persona_id: str) -> dict | None:
    _valid_id(persona_id)
    manifest = Path(ws.personas) / persona_id / "persona.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("id", persona_id)
    return data


def create_persona(ws: Workspace, persona_id: str, body: dict | None = None) -> dict:
    """Mint or update an equal persona: <ws>/personas/<id>/persona.json."""
    _valid_id(persona_id)
    pdir = Path(ws.personas) / persona_id
    manifest = pdir / "persona.json"
    raw = body if isinstance(body, dict) else {}
    updates = {k: v for k, v in raw.items() if k in WS_PERSONA_FIELDS and k != "id"}
    if manifest.is_file():
        try:
            existing = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing["id"] = persona_id
        if not updates:
            return existing
        existing.update(updates)
        manifest.write_text(
            json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        invalidate_workspace_read_cache()
        return existing
    data = dict(updates)
    data["id"] = persona_id
    pdir.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    invalidate_workspace_read_cache()
    return data


def delete_persona(ws: Workspace, persona_id: str, *, hard: bool = False) -> Path | None:
    """Archive ``<ws>/personas/<id>`` to ``personas/.trash/<id>-<utc>`` (default)
    or remove it entirely (``hard=True``). Returns the archive path or the
    removed dir; None when the persona does not exist."""
    _valid_id(persona_id)
    pdir = Path(ws.personas) / persona_id
    if not pdir.is_dir():
        return None
    if hard:
        shutil.rmtree(pdir)
        invalidate_workspace_read_cache()
        return pdir
    trash = Path(ws.personas) / ".trash"
    trash.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dst = trash / f"{persona_id}-{stamp}"
    shutil.move(str(pdir), str(dst))
    invalidate_workspace_read_cache()
    return dst


def _session_store(ws: Workspace):
    """agents-traces is an optional relay dep: lazy import, 501 handled up."""
    try:
        from agents_traces.identity import SessionStore
    except ImportError as exc:
        raise RuntimeError("agents-traces required for session routes") from exc
    return SessionStore(ws)


def _synced_row(ws: Workspace, sid: str, meta: dict, persona: str) -> dict:
    """Session-record-shaped dict for a synced-only session."""
    effective = meta.get("persona") or getattr(ws, "default_persona", "") \
        or "default"
    row = {
        "id": sid,
        "user": str(meta.get("user") or ""),
        "persona": effective,
        "workspace": getattr(ws, "id", ""),
        "synced": True,
    }
    for key in ("title", "last_active", "host", "branch", "repo"):
        if meta.get(key):
            row[key] = meta[key]
    if meta.get("wip_unpushed"):
        row["wip_unpushed"] = True
    if meta.get("last_active"):
        row["start_date"] = str(meta["last_active"])[:10]
    return row


def list_sessions(ws: Workspace, persona: str = "", user: str = "",
                  include_empty: bool = False,
                  empty_grace_sec: int = 900) -> list[dict]:
    try:
        store = _session_store(ws)
    except RuntimeError:
        # agents-traces absent: still serve the synced projection —
        # a hard 501 would blind ws-share peers (agents-relay#45).
        store = None
    rows = ([r.to_dict() for r in store.list(persona=persona, user=user)]
            if store is not None else [])
    try:
        from .storeproj import synced_sessions

        synced = synced_sessions(ws)
    except Exception:
        synced = {"live": {}, "dead": set()}
    # Synced tombstones: the projection says every msg is gone, so the
    # local record must not resurrect the session.
    dead = synced.get("dead") or set()
    if dead:
        rows = [r for r in rows if str(r.get("id") or "") not in dead]
    # Delegate-channel worker records dropped by the grace filter must
    # not resurrect via the synced extras union either (agents-relay#45).
    delegate_ids = {str(r.get("id") or "") for r in rows
                    if str(r.get("channel") or "") == "delegate"}
    want = {str(r.get("id") or "") for r in rows} - {""}
    # One pass over the trace store: first user message per session becomes
    # the sidebar title, so clients need no per-session history fetch.
    # Scan oldest file first so a multi-day session's title is the first
    # day's prompt, not the newest. Skipped entirely when want is empty —
    # a synced-only ws must not walk the trace dir (agents-relay#45).
    titles: dict[str, str] = {}
    scan_ok = False
    if want:
        try:
            from agents_traces.store import TraceStore
            store_t = TraceStore(ws.traces)
            files = list(reversed(store_t.list_trace_files()))
            for e in store_t.iter_events(files=files):
                sid = e.session
                if sid not in want or sid in titles or e.type != "message":
                    continue
                md = e.metadata or {}
                if md.get("role") != "user":
                    continue
                text = " ".join(str(md.get("content") or "").split())
                if text:
                    titles[sid] = text[:160]
                if len(titles) >= len(want):
                    break
            for r in rows:
                title = titles.get(str(r.get("id") or ""))
                if title:
                    r["title"] = title
            scan_ok = True
        except Exception:
            pass
    # Enrich listed rows with synced state (title/last_active/binding).
    live_synced = synced.get("live") or {}
    for r in rows:
        meta = live_synced.get(str(r.get("id") or ""))
        if not meta:
            continue
        if not r.get("title") and meta.get("title"):
            r["title"] = meta["title"]
        for key in ("last_active", "host", "branch", "repo"):
            if meta.get(key) and not r.get(key):
                r[key] = meta[key]
            elif key == "last_active" and meta.get(key) \
                    and str(meta[key]) > str(r.get(key) or ""):
                r[key] = meta[key]
        if meta.get("wip_unpushed") and not r.get("wip_unpushed"):
            r["wip_unpushed"] = True
    if scan_ok and not include_empty:
        # resolve() mints a record per probe/delegate/test turn; a stale
        # record with no user message, and any delegate-channel worker
        # turn, is sidebar noise. Fresh records stay visible so a session
        # created via the API shows up before its first message — and a
        # row still live in the synced projection stays too (it carries
        # real activity the local traces lack, agents-relay#45).
        now = datetime.now(timezone.utc)
        kept = []
        for r in rows:
            rid = str(r.get("id") or "")
            # delegate rows stay dropped even when the synced
            # projection still holds their msgs (agents-relay#45)
            if str(r.get("channel") or "") == "delegate":
                continue
            if rid in live_synced:
                kept.append(r)
                continue
            if rid in titles:
                kept.append(r)
                continue
            stamp = str(r.get("last_active") or r.get("created_at") or "")
            try:
                age = (now - datetime.fromisoformat(
                    stamp.replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                age = empty_grace_sec + 1
            if age <= empty_grace_sec:
                kept.append(r)
        rows = kept
    # Union synced-only sessions: no local record, possibly no local
    # traces at all (store.db arrived via ws-share). They survive the
    # grace filter — their last_active comes from the projection.
    have = {str(r.get("id") or "") for r in rows}
    extras = []
    for sid, meta in sorted(
            live_synced.items(),
            key=lambda kv: str(kv[1].get("last_active") or ""),
            reverse=True):
        if sid in have or sid in delegate_ids:
            continue
        mu = str(meta.get("user") or "")
        # under a user= filter an unstamped synced row leaks — its owner
        # is unknown, so it must not surface (agents-relay#45)
        if user and mu != user and not mu.endswith(f":{user}"):
            continue
        row = _synced_row(ws, sid, meta, persona)
        if persona and row["persona"] != persona:
            continue
        extras.append(row)
    out = rows + extras
    out.sort(
        key=lambda r: str(r.get("last_active") or r.get("created_at") or ""),
        reverse=True)
    return out


def get_session(ws: Workspace, persona: str, session_id: str) -> dict | None:
    try:
        store = _session_store(ws)
    except RuntimeError:
        store = None  # no agents-traces: synced fallback still answers
    rec = store.get(session_id, persona=persona) \
        if store is not None else None
    try:
        from .storeproj import synced_dead, synced_session

        if synced_dead(ws, session_id):
            return None
        meta = synced_session(ws, session_id)
    except Exception:
        meta = None
    if rec is None:
        if meta is None:
            return None
        row = _synced_row(ws, session_id, meta, persona)
        if persona and row["persona"] != persona:
            return None
        return row
    out = rec.to_dict()
    if meta:
        # synced-only fields fill gaps; store last_active wins when newer
        if not out.get("title") and meta.get("title"):
            out["title"] = meta["title"]
        for key in ("host", "branch", "repo"):
            if meta.get(key) and not out.get(key):
                out[key] = meta[key]
        if meta.get("wip_unpushed") and not out.get("wip_unpushed"):
            out["wip_unpushed"] = True
        if meta.get("last_active") and str(meta["last_active"]) > str(
                out.get("last_active") or ""):
            out["last_active"] = meta["last_active"]
    return out


def delete_session(ws: Workspace, persona: str, session_id: str) -> bool:
    """Drop the local record AND tombstone the synced projection —
    otherwise a store.db copy resurrects the session on every peer
    (agents-relay#45). Synced-only sessions (no local JSON) delete via
    the tombstone alone."""
    removed = False
    try:
        removed = bool(
            _session_store(ws).delete(session_id, persona=persona))
    except RuntimeError:
        pass  # agents-traces absent — tombstone still applies
    from .storeproj import session_known, tombstone_session

    try:
        known = session_known(ws, session_id)
    except Exception:  # noqa: BLE001 - store unreadable: local verdict
        known = False
    if not removed and not known:
        return False
    try:
        tombstone_session(ws, session_id)
    except Exception:  # noqa: BLE001 - tombstone best-effort on top
        pass
    invalidate_workspace_read_cache()
    return True


def create_session(ws: Workspace, persona: str, *, user: str = "",
                   channel: str = "", workdir: str = "") -> dict:
    """Mint a session via the identity resolve path (API/CLI only)."""
    try:
        from agents_traces.identity import IdentityStore
    except ImportError as exc:
        raise RuntimeError("agents-traces required for session routes") from exc
    store = IdentityStore(workspace=ws)
    res = store.resolve(
        channel=channel or "http",
        user=user or "anonymous",
        persona=persona,
        workdir=workdir,
        new_session=True,
    )
    row = res.session.to_dict()
    invalidate_workspace_read_cache()
    return row
