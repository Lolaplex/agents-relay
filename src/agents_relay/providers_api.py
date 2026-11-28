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

from .read_cache import cache_key, host_dir_key, relay_read_cache
from .state import read_json, write_json_atomic

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_SECRET_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_FORBIDDEN_SECRET_PREFIXES = (
    "DEVICE_",
    "HUB_",
    "REMOTE_",
    "RELAY_",
    "TELEGRAM_",
    "AGENTS_APPROVAL_",
    "GITHUB_",
    "GH_",
)
_ALLOWED_PROVIDER_SECRET_KEYS = frozenset({
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
    "AGENTS_ACP_API_KEY",
})


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


def _hidden_path() -> Path | None:
    try:
        from agents_workspace import Host

        return Host.from_env().dir / "clients" / "hidden_providers.json"
    except Exception:
        return None


def hidden_providers() -> list[str]:
    """Host-scoped list of hidden (typically built-in/env) provider names."""
    path = _hidden_path()
    if path is None or not path.is_file():
        return []
    raw = read_json(path)
    items = raw.get("hidden") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return sorted({str(x).strip() for x in items if isinstance(x, str) and x.strip()})


def set_provider_hidden(name: str, hidden: bool) -> list[str]:
    """Hide or unhide a provider name without altering read-only env manifests."""
    clean = str(name or "").strip()
    if not _NAME.match(clean):
        raise ProviderError(400, f"invalid provider name: {clean!r}")
    path = _hidden_path()
    if path is None:
        raise ProviderError(500, "host providers dir unavailable")
    cur = set(hidden_providers())
    if hidden:
        cur.add(clean)
    else:
        cur.discard(clean)
    out = sorted(cur)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {"hidden": out})
    relay_read_cache.invalidate("providers")
    relay_read_cache.invalidate("provider-agent-exe")
    return out


def restore_hidden_providers() -> list[str]:
    """Unhide every previously hidden provider on this host."""
    path = _hidden_path()
    if path is None:
        raise ProviderError(500, "host providers dir unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {"hidden": []})
    relay_read_cache.invalidate("providers")
    relay_read_cache.invalidate("provider-agent-exe")
    return []


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


def _is_key_configured(row: dict[str, Any]) -> bool:
    """Return True if this provider has credentials or requires none (never returns secret)."""
    kind = str(row.get("kind") or "")
    name = str(row.get("name") or "")
    if kind in ("echo", "scripted"):
        return True
    if kind == "acp-agent" or name.startswith("acp."):
        from .acp import acp_api_key

        return bool(acp_api_key(name))
    key_env = str(row.get("key_env") or "LLM_API_KEY").strip() or "LLM_API_KEY"
    if os.environ.get(key_env, "").strip():
        return True
    try:
        from agents_workspace import resolve_secret

        return bool(str(resolve_secret(key_env) or "").strip())
    except Exception:
        return False


def provider_rows(
    config: Any = None,
    refresh: bool = False,
    include_hidden: bool = False,
) -> list[dict[str, Any]]:
    """Merged manifest + ACP rows annotated with ``source``/``configurable``/``key_configured``."""
    from runner.providers import list_providers

    hidden_set = set(hidden_providers())
    rows: list[dict[str, Any]] = []
    for raw in list_providers():
        row = {
            "name": raw.get("name"),
            "kind": raw.get("kind"),
            "model": raw.get("model"),
            "when": raw.get("when"),
            "source": source_of(raw),
            "key_configured": _is_key_configured(raw),
        }
        if isinstance(raw.get("label"), str) and raw["label"].strip():
            row["label"] = raw["label"].strip()
        if isinstance(raw.get("key_env"), str) and raw.get("key_env"):
            row["key_env"] = raw["key_env"]
        if isinstance(raw.get("url"), str) and raw.get("url"):
            row["url"] = raw["url"]
        if isinstance(raw.get("url_env"), str) and raw.get("url_env"):
            row["url_env"] = raw["url_env"]
        if raw.get("kind") != "acp-agent" and isinstance(raw.get("models"), list):
            row["models"] = raw["models"]
        rows.append(row)
    from .acp import acp_provider_rows

    rows = [r for r in rows if r.get("kind") != "acp-agent"]
    for raw in acp_provider_rows(config, refresh=refresh):
        row = dict(raw)
        row["source"] = source_of(raw)
        row["key_configured"] = _is_key_configured(row)
        rows.append(row)
    out: list[dict[str, Any]] = []
    for row in rows:
        row["configurable"] = row.get("source") == "file"
        is_hidden = str(row.get("name") or "") in hidden_set
        if is_hidden and not include_hidden:
            continue
        if include_hidden:
            row["hidden"] = is_hidden
        out.append(row)
    return out


_ACP_AGENT_CANDIDATES = (
    {
        "id": "devin",
        "provider_name": "acp.devin",
        "label": "Devin CLI",
        "command": "devin acp",
        "binary": "devin",
        "description": "Local Devin CLI over ACP (uses your devin auth login)",
    },
    {
        "id": "claude",
        "provider_name": "acp.claude",
        "label": "Claude Code (ACP)",
        "command": "claude-code-acp",
        "binary": "claude-code-acp",
        "description": "Claude Code ACP adapter",
    },
    {
        "id": "codex",
        "provider_name": "acp.codex",
        "label": "Codex CLI (ACP)",
        "command": "codex acp",
        "binary": "codex",
        "description": "OpenAI Codex CLI over ACP",
    },
)


_AGENT_DETECT_TTL_SEC = 30.0


def _resolve_agent_exe_cached(binary: str, *, refresh: bool = False) -> str | None:
    from .acp import resolve_agent_exe

    key = cache_key("provider-agent-exe", host_dir_key(), binary)
    return relay_read_cache.get_or_load(
        key,
        _AGENT_DETECT_TTL_SEC,
        lambda: resolve_agent_exe(binary),
        refresh=refresh,
    )


def acp_agent_candidates(*, refresh: bool = False) -> list[dict[str, Any]]:
    """Curated ACP CLI detection rows; read-only and secret-free."""
    try:
        from runner.providers import list_providers

        configured = {
            str(row.get("name") or "")
            for row in list_providers()
            if row.get("name")
        }
    except Exception:
        configured = set()
    rows: list[dict[str, Any]] = []
    for spec in _ACP_AGENT_CANDIDATES:
        row = dict(spec)
        resolved = _resolve_agent_exe_cached(str(spec["binary"]), refresh=refresh)
        row["installed"] = bool(resolved)
        row["resolved_path"] = resolved
        row["configured"] = str(spec["provider_name"]) in configured
        rows.append(row)
    return rows


def set_provider_secret(key_env: str, value: str) -> dict[str, Any]:
    """Persist a provider API key into the host secret vault (never returns the value)."""
    name = str(key_env or "").strip()
    if not _SECRET_ENV_NAME.match(name):
        raise ProviderError(400, f"invalid key_env name: {name!r}")
    from agents_workspace.secrets import INFRA_SECRET_KEYS, write_secret

    if name in INFRA_SECRET_KEYS or name.startswith(_FORBIDDEN_SECRET_PREFIXES):
        raise ProviderError(403, f"cannot overwrite infrastructure secret: {name}")
    known_key_envs = {
        str(r.get("key_env") or "").strip()
        for r in provider_rows()
        if str(r.get("key_env") or "").strip()
    }
    if (
        name not in _ALLOWED_PROVIDER_SECRET_KEYS
        and not name.startswith("AGENTS_ACP_API_KEY_")
        and name not in known_key_envs
    ):
        raise ProviderError(400, f"unrecognized provider secret env: {name}")
    secret_val = str(value or "").strip()
    if not secret_val:
        raise ProviderError(400, "secret value is required")
    write_secret(name, secret_val, scope="host")
    relay_read_cache.invalidate("providers")
    relay_read_cache.invalidate("provider-agent-exe")
    return {"key_env": name, "configured": True}


def create_provider(body: dict[str, Any]) -> dict[str, Any]:
    """Write ``<host>/providers/<name>.json``.

    Shadowing env-sourced rows is the edit path: a file manifest outranks
    package/env rows at load precedence, and deleting the file restores
    the env row."""
    name = str(body.get("name") or "").strip()
    if not _NAME.match(name):
        raise ProviderError(400, f"invalid provider name: {name!r}")
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
    path = _manifest_path(name)
    if path is None:
        raise ProviderError(500, "host providers dir unavailable")
    existing = read_json(path) if path.is_file() else None
    existing_dict = existing if isinstance(existing, dict) else {}
    label_raw = body.get("label")
    if isinstance(label_raw, str):
        clean_label = label_raw.strip()[:80]
        if clean_label:
            manifest["label"] = clean_label
    elif isinstance(existing_dict.get("label"), str) and existing_dict["label"].strip():
        manifest["label"] = existing_dict["label"].strip()[:80]
    model = str(body.get("model") or "").strip()
    if model:
        manifest["model"] = model
    models = body.get("models")
    if isinstance(models, list):
        cleaned_models = [str(m).strip() for m in models if str(m).strip()]
        if cleaned_models:
            manifest["models"] = cleaned_models
    elif models is None and isinstance(existing_dict.get("models"), list):
        manifest["models"] = [str(m).strip() for m in existing_dict["models"] if str(m).strip()]
    if isinstance(existing_dict.get("reasoning"), dict) and "reasoning" not in body:
        manifest["reasoning"] = existing_dict["reasoning"]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, manifest)
    from .acp import acp_models_cache_path

    cache = acp_models_cache_path(name) if kind == "acp-agent" else None
    if cache is not None and cache.is_file():
        cache.unlink()
    row = dict(manifest)
    row["source"] = "file"
    row["configurable"] = True
    relay_read_cache.invalidate("providers")
    relay_read_cache.invalidate("provider-agent-exe")
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
    relay_read_cache.invalidate("providers")
    relay_read_cache.invalidate("provider-agent-exe")


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
    if kind == "acp-agent" or name.startswith("acp."):
        from .acp import acp_agent_cmd, acp_agent_info

        if not acp_agent_cmd(name):
            return {
                "status": "error",
                "detail": "no agent command — edit the row to set one (e.g. 'devin acp')",
            }
        info = acp_agent_info(config, name, refresh=True)
        models = info.get("models") or []
        if info.get("auth_required") and not models:
            suffix = name.split(".", 1)[1].upper() if "." in name else ""
            return {
                "status": "auth",
                "detail": (
                    f"agent requires auth — set AGENTS_ACP_API_KEY_{suffix} "
                    "or AGENTS_ACP_API_KEY in host env/vault, or run the "
                    "agent's own login on the host"
                ),
                "auth_methods": info.get("auth_methods") or [],
                "models": [],
            }
        if info.get("error") and not models:
            detail = str(info["error"])
            if "not on PATH" in detail or "WinError 2" in detail:
                detail = f"{detail} — fix the command via edit"
            return {"status": "error", "detail": detail, "models": []}
        return {
            "status": "ok",
            "detail": f"{len(models)} model(s)" if models else "agent reachable",
            "models": models,
            "modes": info.get("modes") or [],
            "auth_methods": info.get("auth_methods") or [],
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
