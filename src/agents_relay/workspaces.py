"""Workspace + persona + session registry for the relay HTTP surface."""

from __future__ import annotations

import json
from pathlib import Path

from agents_workspace import Host, Workspace
from agents_workspace.workspace import _slug, _valid_id


def list_workspaces() -> list[Workspace]:
    return Workspace.list_all()


def create_workspace(label: str = "", ws_id: str = "") -> Workspace:
    """Create by explicit id (preferred) or by label slug."""
    host = Host.from_env()
    return host.create(ws_id or _slug(label), label=label)


def get_workspace(ws_id: str) -> Workspace:
    host = Host.from_env()
    if not host.workspace_exists(ws_id):
        raise KeyError(ws_id)
    return host.workspace(ws_id)


def select_workspace() -> Workspace:
    """AGENTS_WORKSPACE (serve-pinned) > single existing workspace."""
    import os

    return Host.from_env().select_workspace(env=dict(os.environ))


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
    """Mint an equal persona: <ws>/personas/<id>/persona.json."""
    _valid_id(persona_id)
    pdir = Path(ws.personas) / persona_id
    manifest = pdir / "persona.json"
    data = dict(body or {})
    data["id"] = persona_id
    if manifest.is_file():
        return data
    pdir.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return data


def _session_store(ws: Workspace):
    """agents-traces is an optional relay dep: lazy import, 501 handled up."""
    try:
        from agents_traces.identity import SessionStore
    except ImportError as exc:
        raise RuntimeError("agents-traces required for session routes") from exc
    return SessionStore(ws)


def list_sessions(ws: Workspace, persona: str = "", user: str = "") -> list[dict]:
    return [r.to_dict() for r in _session_store(ws).list(persona=persona, user=user)]


def get_session(ws: Workspace, persona: str, session_id: str) -> dict | None:
    rec = _session_store(ws).get(session_id, persona=persona)
    return rec.to_dict() if rec else None


def delete_session(ws: Workspace, persona: str, session_id: str) -> bool:
    return _session_store(ws).delete(session_id, persona=persona)


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
    return res.session.to_dict()
