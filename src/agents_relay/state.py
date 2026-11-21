"""Relay-local state and host runtime helpers."""

from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from agents_workspace import Host, Workspace

from .config import RelayConfig


def resolve_state_dir(config: RelayConfig | None = None) -> Path:
    """Directory for approvals and downloaded attachments.

    `AGENTS_RELAY_STATE`/`AGENTS_RELAY_DIR` wins, then the workspace relay
    domain. The legacy `~/.agents-relay` is never read at runtime; run
    `agents-workspace migrate` to fold it into a workspace.
    """
    if config is not None and config.state_dir:
        return Path(config.state_dir).expanduser()
    ws = Workspace.from_env()
    legacy = Path.home() / ".agents-relay"
    global _legacy_warned
    if legacy.is_dir() and not _legacy_warned:
        _legacy_warned = True
        import warnings

        warnings.warn(
            f"legacy relay state dir {legacy} is ignored; run "
            "`agents-workspace migrate` to fold it into a workspace",
            DeprecationWarning,
            stacklevel=2,
        )
    ws.relay_state.mkdir(parents=True, exist_ok=True)
    return ws.relay_state


_legacy_warned = False


def legacy_relay_dirs(config: RelayConfig | None = None, *, ws_ref: str = "") -> list[Path]:
    """Candidate legacy ``<ws>/relay`` (and explicit ``state_dir``) paths for 1-release reads.

    Never resolves ``""`` to CWD and never creates workspaces on disk.
    """
    candidates: list[Path] = []
    try:
        host = Host.from_env()
    except Exception:
        host = None

    if host is not None and ws_ref.strip():
        try:
            if host.workspace_exists(ws_ref.strip()):
                candidates.append(host.workspace(ws_ref.strip()).relay_state)
        except Exception:
            pass

    raw_state = str(getattr(config, "state_dir", "") or "").strip() if config is not None else ""
    if raw_state:
        candidates.append(Path(raw_state).expanduser())

    if host is not None:
        env_ws = os.environ.get("AGENTS_WORKSPACE", "").strip()
        if env_ws:
            try:
                if host.workspace_exists(env_ws):
                    candidates.append(host.workspace(env_ws).relay_state)
            except Exception:
                pass
        if not ws_ref.strip():
            try:
                for ws in host.list_workspaces():
                    candidates.append(ws.relay_state)
            except Exception:
                pass

    out: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            key = str(path.resolve(strict=False))
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def approval_dir(config: RelayConfig | None = None) -> Path:
    """Host-owned approval dir ``<host>/run/approvals``."""
    del config
    return Host.from_env().run / "approvals"


def inbox_dir(config: RelayConfig | None = None) -> Path:
    return resolve_state_dir(config) / "inbox"


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_json_any(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _is_posix() -> bool:
    return os.name != "nt"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if _is_posix():
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def write_json_atomic(path: Path, payload: Any, *, mode: int | None = None) -> None:
    if mode is not None:
        ensure_private_dir(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    raw = json.dumps(payload, ensure_ascii=False)
    if mode is not None and _is_posix():
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(raw)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        os.chmod(tmp, mode)
    else:
        tmp.write_text(raw, encoding="utf-8")
    os.replace(tmp, path)
    if mode is not None and _is_posix():
        os.chmod(path, mode)


@contextmanager
def exclusive_lock(lock_path: Path, timeout: float = 5.0) -> Iterator[None]:
    """Create-exclusive lock file. Stale locks older than 30s are removed."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0
            if age > 30:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"lock busy: {lock_path.name}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass
