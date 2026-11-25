"""Session run-lock leases — in-memory, hub-local, TTL+fencing.

Per klanker#136/frontier design: session locks are out-of-band hub
leases, NOT store datoms (heartbeats would explode the append-only
log and LWW cannot express lock expiry). One live run per
(ws, session): acquire grants {fence, expires_in}; the holder renews
every ~15s (TTL 45s); a dropped device simply stops renewing and the
lease expires — no deadlock, the next claim wins.

`fence` is a monotonically increasing token per grant: a stale holder
that comes back online after its lease expired can be told apart from
the current owner downstream.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

TTL_SEC = 45.0


def _emit_lease(action: str, ws: str, session: str, holder: str,
                run_id: str = "") -> None:
    """lease.changed SSE for presence/host surfaces (agents-relay#43)."""
    try:
        from .events import emit_event

        emit_event("lease.changed", {
            "action": action, "ws": ws, "session": session,
            "holder": holder, "run_id": run_id}, workspace=str(ws or ""))
    except Exception:  # noqa: BLE001 - events never block leasing
        pass


class LeaseBook:
    """In-memory lease slots; only the fence high-water mark persists.

    Leases themselves die with the hub (holders re-acquire after their
    TTL), but fence tokens must stay monotonic across restarts or a
    stale pre-restart holder could present a fence equal to a fresh
    grant. With ``state_path`` the last issued fence is written before a
    grant is returned and reloaded on start; without a readable state
    the counter seeds from wall-clock milliseconds, which is above any
    fence a counter could have reached in practice.
    """

    def __init__(self, clock=time.monotonic, state_path=None,
                 wall=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._slots: dict[tuple[str, str], dict] = {}
        self._state_path = Path(state_path) if state_path else None
        self._fence = self._load_fence(wall)

    def _load_fence(self, wall) -> int:
        seed = 0
        if self._state_path is not None:
            seed = int(wall() * 1000)
            try:
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                seed = max(seed, int(data.get("fence") or 0))
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        return seed

    def _persist_fence(self) -> None:
        if self._state_path is None:
            return
        path = self._state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps({"fence": self._fence}) + "\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _slot(self, key, now):
        s = self._slots.get(key)
        if s is not None and s["expires_mono"] <= now:
            del self._slots[key]
            return None
        return s

    def sweep(self) -> int:
        """Drop every expired slot — run on writes + snapshots so dead
        leases never accumulate or shadow a snapshot (klanker#148)."""
        with self._lock:
            now = self._clock()
            dead = [k for k, s in self._slots.items()
                    if s["expires_mono"] <= now]
            for k in dead:
                del self._slots[k]
        return len(dead)

    def acquire(self, ws: str, session: str, holder: str,
                run_id: str = "", ttl: float = TTL_SEC) -> dict:
        key = (ws, session)
        emit = None
        with self._lock:
            now = self._clock()
            # sweep inside the lock so an expired sibling slot elsewhere
            # is dropped in the same write op (klanker#148)
            for k, s in list(self._slots.items()):
                if s["expires_mono"] <= now:
                    del self._slots[k]
            cur = self._slot(key, now)
            if cur is not None and (cur["holder"], cur["run_id"]) != (holder, run_id):
                result = {
                    "granted": False,
                    "holder": cur["holder"],
                    "run_id": cur["run_id"],
                    "expires_in": round(cur["expires_mono"] - now, 3),
                }
            elif cur is not None:
                # Idempotent re-acquire by the same (holder, run_id), e.g. a
                # retried request: keep the fence, extend the TTL.
                cur["expires_mono"] = now + ttl
                result = {"granted": True, "fence": cur["fence"],
                          "expires_in": ttl}
            else:
                self._fence += 1
                self._persist_fence()
                cur = {
                    "holder": holder, "run_id": run_id,
                    "fence": self._fence, "expires_mono": now + ttl,
                }
                self._slots[key] = cur
                emit = ("acquire", ws, session, holder, run_id)
                result = {"granted": True, "fence": self._fence,
                          "expires_in": ttl}
        # SSE outside _lock — a slow subscriber must never hold the
        # lease mutex (agents-relay#45)
        if emit is not None:
            _emit_lease(*emit)
        return result

    def renew(self, ws: str, session: str, holder: str, fence: int,
              ttl: float = TTL_SEC) -> dict:
        key = (ws, session)
        with self._lock:
            now = self._clock()
            cur = self._slot(key, now)
            if cur is None or cur["holder"] != holder or cur["fence"] != int(fence):
                return {"granted": False}
            cur["expires_mono"] = now + ttl
            return {"granted": True, "fence": int(fence), "expires_in": ttl}

    def release(self, ws: str, session: str, holder: str, fence: int) -> dict:
        key = (ws, session)
        emit = None
        with self._lock:
            now = self._clock()
            cur = self._slot(key, now)
            if cur is not None and cur["holder"] == holder \
                    and cur["fence"] == int(fence):
                del self._slots[key]
                emit = ("release", ws, session, holder,
                        cur.get("run_id") or "")
        if emit is not None:
            _emit_lease(*emit)
        return {"released": True}

    def release_holder(self, holder: str) -> int:
        """Drop every lease held by ``holder`` — device revocation must
        not leave dead sessions fencing live ones (agents-relay#42).
        Each dropped slot emits ``lease.changed`` outside the lock so
        ws-scoped SSE sees the holder vanish (agents-relay#45)."""
        with self._lock:
            dropped = [(k, s) for k, s in self._slots.items()
                       if s["holder"] == holder]
            for k, _s in dropped:
                del self._slots[k]
        for (ws, session), s in dropped:
            _emit_lease("release", ws, session, holder,
                        s.get("run_id") or "")
        return len(dropped)

    def snapshot(self) -> list[dict]:
        with self._lock:
            now = self._clock()
            out = []
            for (ws, session), s in list(self._slots.items()):
                if s["expires_mono"] <= now:
                    del self._slots[(ws, session)]
                    continue
                out.append({
                    "ws": ws, "session": session,
                    "holder": s["holder"], "run_id": s["run_id"],
                    "fence": s["fence"],
                    "expires_in": round(s["expires_mono"] - now, 3),
                })
            return out


_book: LeaseBook | None = None
_book_lock = threading.Lock()

# klanker serves wire workspace policy in: fn(ws_id) -> lease ttl seconds
# (ws.execution.lease_sec, agents-workspace#44 / klanker#148).
lease_ttl_fn = None


def default_ttl(ws_id: str) -> float:
    if lease_ttl_fn is not None:
        try:
            ttl = float(lease_ttl_fn(ws_id) or 0)
            if ttl > 0:
                return ttl
        except Exception:  # noqa: BLE001 - policy lookup never blocks leases
            pass
    return TTL_SEC


def _default_state_path():
    try:
        from agents_workspace import Host

        return Host.from_env().clients / "hub_leases.json"
    except Exception:  # noqa: BLE001 - no host dir: in-memory epoch only
        return None


def hub_leases() -> LeaseBook:
    global _book
    if _book is not None:
        return _book
    with _book_lock:
        if _book is None:
            _book = LeaseBook(state_path=_default_state_path())
    return _book


def reset_leases() -> None:
    """Drop the process-wide book (tests; hub reset)."""
    global _book
    with _book_lock:
        _book = None
