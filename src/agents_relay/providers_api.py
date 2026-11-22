"""Provider write API, session model picks, and favorites.

``/v1/providers`` POST/DELETE/probe write manifest files under
``<host>/providers``. Rows resolved from any other directory (package
defaults, ``AGENTS_PROVIDERS_DIR``, env-dropped ACP manifests) are
``source=env`` and read-only through this API — env-managed rows must be
changed on the host, never silently shadowed by a file.

Session picks live in ``<host>/clients/sessions.json`` — the web mirror of
the telegram per-chat model pref. Favorites live in
``<host>/clients/favorites.json`` so every surface on this host sees the
same stars.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from .state import read_json, write_json_atomic

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class ProviderError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _host_providers_dir() -> Path | None:
    try:
        from agents_workspace import Host

        return Host.from_env().dir / "providers"
    except Exception:
        return None


def _manifest_path(name: str) -> Path | None:
    base = _host_providers_dir()
    return base / f"{name}.json" if base is not None else None


def _find(name: str) -> dict[str, Any] | None:
    try:
        from runner.providers import find_provider

        return find_provider(name)
    except Exception:
        return None


def source_of(row: dict[str, Any]) -> str:
    """``file`` = deletable manifest under ``<host>/providers``; ``env`` = read-only."""
    name = str(row.get("name") or "")
    kind = str(row.get("kind") or "")
    if str(row.get("source") or "") == "env":
        return "env"
    if kind == "acp-agent":
        # File-managed agents carry their own command; env-managed rows rely
        # on AGENTS_ACP_CMD* even when the manifest file exists.
        return "file" if str(row.get("command") or "").strip() else "env"
    path = _manifest_path(name)
    return "file" if path is not None and path.is_file() else "env"


def provider_rows(config: Any = None, refresh: bool = False) -> list[dict[str, Any]]:
    """Merged manifest + ACP rows annotated with ``source``/``configurable``."""
    from runner.providers import list_providers

    rows: list[dict[str, Any]] = []
    for raw in list_providers():
        row = {
            "name": raw.get("name"),
            "kind": raw.get("kind"),
            "model": raw.get("model"),
            "when": raw.get("when"),
            "source": source_of(raw),
        }
        if raw.get("kind") != "acp-agent" and isinstance(raw.get("models"), list):
            row["models"] = raw["models"]
        rows.append(row)
    from .acp import acp_provider_rows

    rows = [r for r in rows if r.get("kind") != "acp-agent"]
    for raw in acp_provider_rows(config, refresh=refresh):
        row = dict(raw)
        row["source"] = source_of(raw)
        rows.append(row)
    for row in rows:
        row["configurable"] = row.get("source") == "file"
    return rows


def create_provider(body: dict[str, Any]) -> dict[str, Any]:
    """Write ``<host>/providers/<name>.json``; refuses to shadow env rows."""
    name = str(body.get("name") or "").strip()
    if not _NAME.match(name):
        raise ProviderError(400, f"invalid provider name: {name!r}")
    existing = _find(name)
    if existing is not None and source_of(existing) != "file":
        raise ProviderError(409, f"provider {name} is managed by host env")
    kind = str(body.get("kind") or "openai_compat").strip() or "openai_compat"
    manifest: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "when": "on_request",
        "cadence": "on_request",
        "verb": "chat.completions",
        "rests_on": "Completion provider manifest written via POST /v1/providers",
        "expected_exit": 0,
    }
    if kind == "acp-agent":
        if not name.startswith("acp."):
            raise ProviderError(400, "acp-agent providers must be named acp.<name>")
        command = str(body.get("command") or "").strip()
        if not command:
            raise ProviderError(400, "command is required for acp-agent providers")
        manifest["command"] = command
        manifest["rests_on"] = (
            "Foreign ACP agent loop; agents-relay acp-run spawns it per turn"
        )
    else:
        url = str(body.get("url") or "").strip()
        url_env = str(body.get("url_env") or "").strip()
        if not url and not url_env:
            raise ProviderError(400, "url or url_env is required")
        if url:
            manifest["url"] = url
        if url_env:
            manifest["url_env"] = url_env
        manifest["key_env"] = str(body.get("key_env") or "LLM_API_KEY").strip()
    model = str(body.get("model") or "").strip()
    if model:
        manifest["model"] = model
    models = body.get("models")
    if isinstance(models, list):
        manifest["models"] = [str(m).strip() for m in models if str(m).strip()]
    path = _manifest_path(name)
    if path is None:
        raise ProviderError(500, "host providers dir unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, manifest)
    from .acp import acp_models_cache_path

    cache = acp_models_cache_path(name) if kind == "acp-agent" else None
    if cache is not None and cache.is_file():
        cache.unlink()
    row = dict(manifest)
    row["source"] = "file"
    row["configurable"] = True
    return row


def delete_provider(name: str) -> None:
    row = _find(name)
    if row is None:
        raise ProviderError(404, f"unknown provider: {name}")
    if source_of(row) != "file":
        raise ProviderError(409, f"provider {name} is managed by host env")
    path = _manifest_path(name)
    assert path is not None
    path.unlink()
    from .acp import acp_models_cache_path

    cache = acp_models_cache_path(name)
    if cache.is_file():
        cache.unlink()


def acp_models_cache_clear(name: str) -> None:
    from .acp import acp_models_cache_path

    cache = acp_models_cache_path(name)
    if cache.is_file():
        cache.unlink()


def probe_provider(config: Any, name: str) -> dict[str, Any]:
    """Health/auth status for the settings test button."""
    row = _find(name)
    if row is None:
        raise ProviderError(404, f"unknown provider: {name}")
    kind = str(row.get("kind") or "")
    if kind == "acp-agent":
        from .acp import acp_agent_cmd, acp_agent_info

        if not acp_agent_cmd(name):
            return {"status": "error", "detail": "no agent command configured"}
        info = acp_agent_info(config, name, refresh=True)
        models = info.get("models") or []
        if info.get("error") and not models:
            return {"status": "error", "detail": str(info["error"]), "models": []}
        return {
            "status": "ok",
            "detail": f"{len(models)} model(s)" if models else "agent reachable",
            "models": models,
            "modes": info.get("modes") or [],
        }
    if kind in ("echo", "scripted"):
        return {"status": "ok", "detail": f"{kind} provider"}
    url = os.environ.get(str(row.get("url_env") or ""), "") or str(
        row.get("url") or ""
    )
    if not url.strip():
        return {"status": "missing-url", "detail": "no url/url_env configured"}
    key_env = str(row.get("key_env") or "").strip()
    if key_env:
        try:
            from agents_workspace import resolve_secret

            has_key = bool(str(resolve_secret(key_env) or "").strip())
        except Exception:
            has_key = bool(os.environ.get(key_env, "").strip())
        if not has_key:
            return {
                "status": "missing-key",
                "detail": f"{key_env} not set in env or host vault",
            }
    return {"status": "configured", "detail": key_env or "no key required"}


# ---------------------------------------------------------------------------
# Session-scoped model picks + favorites (<host>/clients, web mirror of
# telegram chat prefs).


def _clients_file(filename: str) -> Path:
    try:
        from agents_workspace import Host

        return Host.from_env().clients / filename
    except Exception:
        return Path.home() / ".agents" / "clients" / filename


def session_pick(session: str) -> dict[str, str]:
    sid = session.strip()
    if not sid:
        return {"provider": "", "model": ""}
    data = read_json(_clients_file("sessions.json")) or {}
    row = data.get(sid)
    if not isinstance(row, dict):
        return {"provider": "", "model": ""}
    return {
        "provider": str(row.get("provider") or ""),
        "model": str(row.get("model") or ""),
    }


def set_session_pick(session: str, provider: str, model: str) -> dict[str, str]:
    sid = session.strip()
    if not sid:
        raise ProviderError(400, "session is required")
    provider = provider.strip()
    if not provider:
        raise ProviderError(400, "provider is required")
    path = _clients_file("sessions.json")
    data = read_json(path) or {}
    data[sid] = {"provider": provider, "model": model.strip(), "ts": time.time()}
    write_json_atomic(path, data)
    return {"provider": provider, "model": model.strip()}


def clear_session_pick(session: str) -> dict[str, str]:
    sid = session.strip()
    if not sid:
        raise ProviderError(400, "session is required")
    path = _clients_file("sessions.json")
    data = read_json(path) or {}
    data.pop(sid, None)
    write_json_atomic(path, data)
    return {"provider": "", "model": ""}


def get_favorites() -> list[dict[str, str]]:
    data = read_json_any_list(_clients_file("favorites.json"))
    return data


def read_json_any_list(path: Path) -> list[dict[str, str]]:
    from .state import read_json_any

    raw = read_json_any(path)
    rows = raw.get("favorites") if isinstance(raw, dict) else raw
    out: list[dict[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider") or "").strip()
        if not provider:
            continue
        out.append({"provider": provider, "model": str(row.get("model") or "")})
    return out


def set_favorites(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    clean: list[dict[str, str]] = []
    for row in rows[:200]:
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider") or "").strip()
        if not provider:
            continue
        entry = {"provider": provider, "model": str(row.get("model") or "")}
        if entry not in clean:
            clean.append(entry)
    write_json_atomic(
        _clients_file("favorites.json"), {"favorites": clean, "ts": time.time()}
    )
    return clean


def star_favorite(provider: str, model: str, star: bool) -> list[dict[str, str]]:
    provider = provider.strip()
    if not provider:
        raise ProviderError(400, "provider is required")
    rows = get_favorites()
    entry = {"provider": provider, "model": model.strip()}
    rows = [r for r in rows if not (r["provider"] == provider and r["model"] == entry["model"])]
    if star:
        rows.insert(0, entry)
    return set_favorites(rows)
