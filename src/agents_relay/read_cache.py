"""Tiny thread-safe TTL cache for hot relay read endpoints."""

from __future__ import annotations

import copy
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

CacheKey = tuple[str, ...]


def cache_key(namespace: str, *parts: object) -> CacheKey:
    return (str(namespace),) + tuple(str(part) for part in parts)


def _key_tuple(key: str | CacheKey | tuple[object, ...]) -> CacheKey:
    if isinstance(key, str):
        return (key,)
    return tuple(str(part) for part in key)


def host_dir_key() -> str:
    """Canonical host-dir cache dimension; tests swapping hosts never collide."""
    raw = ""
    try:
        from agents_workspace import Host

        raw = str(Host.from_env().dir or "")
    except Exception:
        raw = ""
    if not raw:
        raw = os.environ.get("AGENTS_HOST_DIR", "").strip() or str(Path.home() / ".agents")
    try:
        return os.path.normcase(str(Path(raw).expanduser().resolve()))
    except Exception:
        return os.path.normcase(os.path.abspath(os.path.expanduser(raw)))


def _copy(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


class ReadCache:
    """Small lock-protected TTL map with prefix invalidation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[CacheKey, tuple[float, Any]] = {}

    def get_or_load(
        self,
        key: str | CacheKey | tuple[object, ...],
        ttl_sec: float,
        loader: Callable[[], Any],
        *,
        refresh: bool = False,
    ) -> Any:
        normalized = _key_tuple(key)
        now = time.monotonic()
        if not refresh:
            with self._lock:
                hit = self._entries.get(normalized)
                if hit is not None and now - hit[0] < ttl_sec:
                    return _copy(hit[1])
        value = loader()
        with self._lock:
            self._entries[normalized] = (time.monotonic(), _copy(value))
        return _copy(value)

    def invalidate(self, prefix: str | CacheKey | tuple[object, ...] = ()) -> int:
        normalized = _key_tuple(prefix)
        with self._lock:
            if not normalized:
                count = len(self._entries)
                self._entries.clear()
                return count
            doomed = [
                key for key in self._entries
                if key[: len(normalized)] == normalized
            ]
            for key in doomed:
                self._entries.pop(key, None)
            return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


relay_read_cache = ReadCache()
