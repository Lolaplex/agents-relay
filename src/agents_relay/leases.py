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

import threading
import time

TTL_SEC = 45.0


class LeaseBook:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._slots: dict[tuple[str, str], dict] = {}
        self._fence = 0

    def _slot(self, key, now):
        s = self._slots.get(key)
        if s is not None and s["expires_mono"] <= now:
            del self._slots[key]
            return None
        return s

    def acquire(self, ws: str, session: str, holder: str,
                run_id: str = "", ttl: float = TTL_SEC) -> dict:
        key = (ws, session)
        with self._lock:
            now = self._clock()
            cur = self._slot(key, now)
            if cur is not None and (cur["holder"], cur["run_id"]) != (holder, run_id):
                return {
                    "granted": False,
                    "holder": cur["holder"],
                    "run_id": cur["run_id"],
                    "expires_in": round(cur["expires_mono"] - now, 3),
                }
            self._fence += 1
            cur = {
                "holder": holder, "run_id": run_id,
                "fence": self._fence, "expires_mono": now + ttl,
            }
            self._slots[key] = cur
            return {"granted": True, "fence": self._fence,
                    "expires_in": ttl}

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
        with self._lock:
            now = self._clock()
            cur = self._slot(key, now)
            if cur is not None and cur["holder"] == holder \
                    and cur["fence"] == int(fence):
                del self._slots[key]
            return {"released": True}

    def snapshot(self) -> list[dict]:
        with self._lock:
            now = self._clock()
            out = []
            for (ws, session), s in self._slots.items():
                if s["expires_mono"] <= now:
                    continue
                out.append({
                    "ws": ws, "session": session,
                    "holder": s["holder"], "run_id": s["run_id"],
                    "fence": s["fence"],
                    "expires_in": round(s["expires_mono"] - now, 3),
                })
            return out


_book: LeaseBook | None = None


def hub_leases() -> LeaseBook:
    global _book
    if _book is None:
        _book = LeaseBook()
    return _book
