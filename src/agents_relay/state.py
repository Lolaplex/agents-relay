"""Relay-local state directory. Never writes under ~/.agents."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import RelayConfig


def resolve_state_dir(config: RelayConfig | None = None) -> Path:
    """Directory for approvals and downloaded attachments.

    `AGENTS_RELAY_STATE` wins, then `RelayConfig.state_dir`, else `~/.agents-relay`.
    """
    if config is not None and config.state_dir:
        return Path(config.state_dir).expanduser()
    env = os.environ.get("AGENTS_RELAY_STATE", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agents-relay"


def approval_dir(config: RelayConfig | None = None) -> Path:
    return resolve_state_dir(config) / "approvals"


def inbox_dir(config: RelayConfig | None = None) -> Path:
    return resolve_state_dir(config) / "inbox"


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


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
