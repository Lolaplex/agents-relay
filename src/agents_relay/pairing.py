"""Ephemeral in-memory single-use pairing codes and tokens (60s TTL, never persisted)."""
from __future__ import annotations

import hmac
import re
import secrets
import threading
import time
from typing import Any, Callable

# Excludes visually ambiguous 0/O/1/I characters for manual mobile code entry.
_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_CODE_LEN = 8
_MODES = frozenset({"web", "remote"})
_MIN_TTL_SEC = 15
_MAX_TTL_SEC = 120
_DEFAULT_TTL_SEC = 60
_MAX_PENDING = 64
_RATE_WINDOW_SEC = 60.0
_MAX_FAILS_PER_WINDOW = 5


def _mint_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
    return f"{raw[:4]}-{raw[4:]}"


def _norm_code(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


class PairingStore:
    """In-memory single-use pairing registry with per-IP rate limiting."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._lock = threading.Lock()
        self._clock = clock or time.monotonic
        self._entries: dict[str, dict[str, Any]] = {}
        self._fails: dict[str, list[float]] = {}

    def _prune_locked(self, now: float) -> None:
        expired = [k for k, v in self._entries.items() if float(v["expires_mono"]) <= now]
        for k in expired:
            self._entries.pop(k, None)
        cutoff = now - _RATE_WINDOW_SEC
        for ip in list(self._fails):
            kept = [t for t in self._fails[ip] if t > cutoff]
            if kept:
                self._fails[ip] = kept
            else:
                self._fails.pop(ip, None)

    def create_pair(
        self,
        *,
        mode: str = "web",
        ttl_sec: int = _DEFAULT_TTL_SEC,
        host_id: str = "",
        base_url: str = "",
    ) -> dict[str, Any]:
        clean_mode = (mode or "web").strip().lower()
        if clean_mode not in _MODES:
            raise ValueError(f"mode must be 'web' or 'remote', got {mode!r}")
        try:
            ttl = int(ttl_sec)
        except (TypeError, ValueError):
            ttl = _DEFAULT_TTL_SEC
        ttl = max(_MIN_TTL_SEC, min(_MAX_TTL_SEC, ttl))

        now_mono = self._clock()
        now_wall = int(time.time())
        token = secrets.token_urlsafe(32)
        code = _mint_code()
        code_norm = _norm_code(code)
        pair_id = secrets.token_hex(8)

        with self._lock:
            self._prune_locked(now_mono)
            while len(self._entries) >= _MAX_PENDING:
                oldest = next(iter(self._entries))
                self._entries.pop(oldest, None)
            self._entries[pair_id] = {
                "token": token,
                "code": code,
                "code_norm": code_norm,
                "mode": clean_mode,
                "host_id": host_id,
                "expires_mono": now_mono + ttl,
            }

        clean_base = (base_url or "").strip().rstrip("/")
        if clean_base:
            pair_url = (
                f"{clean_base}/#pair={token}"
                if clean_mode == "web"
                else f"{clean_base}/v1/pair/redeem"
            )
        else:
            pair_url = f"/#pair={token}" if clean_mode == "web" else "/v1/pair/redeem"

        return {
            "mode": clean_mode,
            "code": code,
            "token": token,
            "pair_url": pair_url,
            "expires_in": ttl,
            "expires_at": now_wall + ttl,
            "host_id": host_id,
        }

    def redeem_pair(
        self,
        credential: str,
        *,
        client_ip: str = "",
    ) -> tuple[str, dict[str, Any] | None]:
        raw = str(credential or "").strip()
        norm = _norm_code(raw)
        ip = (client_ip or "").strip() or "unknown"
        now_mono = self._clock()

        with self._lock:
            self._prune_locked(now_mono)
            recent_fails = self._fails.get(ip, [])
            if len(recent_fails) >= _MAX_FAILS_PER_WINDOW:
                return "rate_limited", None

            if not raw and not norm:
                self._fails.setdefault(ip, []).append(now_mono)
                return "invalid", None

            raw_b = raw.encode("utf-8")
            norm_b = norm.encode("utf-8")
            matched_id: str | None = None
            for pid, entry in self._entries.items():
                tok_b = str(entry["token"]).encode("utf-8")
                cod_b = str(entry["code_norm"]).encode("utf-8")
                tok_ok = bool(raw) and hmac.compare_digest(raw_b, tok_b)
                cod_ok = bool(norm) and hmac.compare_digest(norm_b, cod_b)
                if tok_ok or cod_ok:
                    matched_id = pid
            if matched_id is not None:
                consumed = self._entries.pop(matched_id)
                return "ok", {
                    "mode": consumed["mode"],
                    "host_id": consumed["host_id"],
                }

            self._fails.setdefault(ip, []).append(now_mono)
            return "invalid", None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._fails.clear()


_STORE = PairingStore()


def create_pair(
    *,
    mode: str = "web",
    ttl_sec: int = _DEFAULT_TTL_SEC,
    host_id: str = "",
    base_url: str = "",
) -> dict[str, Any]:
    return _STORE.create_pair(mode=mode, ttl_sec=ttl_sec, host_id=host_id, base_url=base_url)


def redeem_pair(credential: str, *, client_ip: str = "") -> tuple[str, dict[str, Any] | None]:
    return _STORE.redeem_pair(credential, client_ip=client_ip)


def clear_pairs() -> None:
    _STORE.clear()
