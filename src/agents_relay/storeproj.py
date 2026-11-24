"""Optional read of the synced ``msg:*``/``session:*``/``frame:*``
projection in ``<ws>/store.db``.

Klanker's msgsync projects trace messages into the workspace store; a
host that received ``store.db`` via ws-share holds the projection without
the peer's traces or ``personas/*/sessions`` records. Session surfaces
union these rows with local JSONL/JSON state so synced-only sessions stay
listable and readable. Soft-import of ``agents_store`` only — absent
file, absent package, or an empty projection degrades to no rows.

Live-message semantics mirror ``klanker.msgsync``: a msg is hidden when
``msg:deleted`` or when its ``msg:frame_id`` names a retired frame;
ordering is ``(ts, ord, entity)``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def _connect(ws):
    db = getattr(ws, "store_db", None) or Path(ws.dir) / "store.db"
    db = Path(db)
    if not db.is_file():
        return None
    try:
        from agents_store.db import connect
    except ImportError:
        return None
    try:
        return connect(str(db), check_same_thread=False)
    except Exception:
        return None


def _retired_frames(conn, session_id: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT r.e FROM v_current r JOIN v_current s ON s.e = r.e"
            " WHERE r.a = 'frame:retired' AND r.v = 'true'"
            " AND s.a = 'frame:session' AND s.v = ?",
            (session_id,),
        )
    }


def _live(conn, session_id: str) -> list[dict]:
    rows: dict[str, dict] = {}
    for e, a, v in conn.execute(
        "SELECT e, a, v FROM v_current WHERE e IN ("
        " SELECT e FROM v_current WHERE a = 'msg:session' AND v = ?)",
        (session_id,),
    ):
        rows.setdefault(e, {"e": e})[a.split(":", 1)[1]] = v
    retired = _retired_frames(conn, session_id)
    live = [
        r
        for r in rows.values()
        if r.get("deleted") != "true"
        and str(r.get("frame_id") or "") not in retired
    ]
    if live:
        marks = ",".join("?" for _ in live)
        ords = {
            e: str(o or "")
            for e, o in conn.execute(
                f"SELECT e, MAX(ord) FROM datom WHERE e IN ({marks})"
                " GROUP BY e",
                [r["e"] for r in live],
            )
        }
        live.sort(
            key=lambda r: (
                str(r.get("ts") or ""),
                ords.get(r["e"], ""),
                r["e"],
            )
        )
    return live


def _session_attrs(conn, session_id: str) -> dict[str, str]:
    return {
        a.split(":", 1)[1]: str(v)
        for a, v in conn.execute(
            "SELECT a, v FROM v_current WHERE e = ? AND a LIKE 'session:%'",
            (f"session:{session_id}",),
        )
    }


def _meta_for(conn, session_id: str, live: list[dict]) -> dict[str, Any]:
    """session:* binding + title/last_active + first persona/user seen."""
    attrs = _session_attrs(conn, session_id)
    title = str(attrs.get("title") or "")
    if not title:
        title = next(
            (
                " ".join(str(r.get("text") or "").split())[:160]
                for r in live
                if r.get("role") == "user" and str(r.get("text") or "").strip()
            ),
            "",
        )
    last_active = str(attrs.get("last_active") or "")
    if not last_active and live:
        last_active = str(live[-1].get("ts") or "")
    persona = user = ""
    for r in live:
        if r.get("persona") or r.get("user"):
            persona = str(r.get("persona") or "")
            user = str(r.get("user") or "")
            break
    return {
        "id": session_id,
        "title": title,
        "last_active": last_active,
        # session:host binding when present; else the last projecting host
        "host": str(
            attrs.get("host") or (live[-1].get("host") if live else "") or ""
        ),
        "branch": str(attrs.get("branch") or ""),
        "repo": str(attrs.get("repo") or ""),
        "wip_unpushed": str(attrs.get("wip_unpushed") or "") == "true",
        "persona": persona,
        "user": user,
    }


def synced_sessions(ws) -> dict:
    """All projected sessions: ``{"live": {sid: meta}, "dead": set}``.

    ``dead`` holds sessions whose msgs are all deleted — synced tombstones
    that must suppress any resurrecting local record.
    """
    conn = _connect(ws)
    if conn is None:
        return {"live": {}, "dead": set()}
    try:
        sids = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT v FROM v_current WHERE a = 'msg:session'"
            )
        ]
        live_map: dict[str, dict] = {}
        dead: set[str] = set()
        for sid in sids:
            msgs = _live(conn, sid)
            if msgs:
                live_map[sid] = _meta_for(conn, sid, msgs)
            else:
                dead.add(sid)
        return {"live": live_map, "dead": dead}
    except Exception:
        return {"live": {}, "dead": set()}
    finally:
        conn.close()


def synced_session(ws, session_id: str) -> dict | None:
    """Meta for one live synced session, or None when absent/dead."""
    conn = _connect(ws)
    if conn is None:
        return None
    try:
        msgs = _live(conn, session_id)
        if not msgs:
            return None
        return _meta_for(conn, session_id, msgs)
    except Exception:
        return None
    finally:
        conn.close()


def synced_dead(ws, session_id: str) -> bool:
    """The session has projected msgs and every one is deleted — a synced
    tombstone that suppresses any resurrecting local record."""
    conn = _connect(ws)
    if conn is None:
        return False
    try:
        has = conn.execute(
            "SELECT 1 FROM v_current WHERE a = 'msg:session' AND v = ?"
            " LIMIT 1", (session_id,)).fetchone()
        if not has:
            return False
        return not _live(conn, session_id)
    except Exception:
        return False
    finally:
        conn.close()


def synced_messages(ws, session_id: str) -> list[dict]:
    """Live ``msg:*`` rows ``{role,text,ts,host,persona,user}``."""
    conn = _connect(ws)
    if conn is None:
        return []
    try:
        return [
            {
                "role": str(r.get("role") or "user"),
                "text": str(r.get("text") or ""),
                "ts": str(r.get("ts") or ""),
                "host": str(r.get("host") or ""),
                "persona": str(r.get("persona") or ""),
                "user": str(r.get("user") or ""),
            }
            for r in _live(conn, session_id)
        ]
    except Exception:
        return []
    finally:
        conn.close()


def union_events(ws, session_id: str, local_events: list[dict]) -> list[dict]:
    """Local trace events + synced msgs the local trace lacks.

    Dedupe is the message body hash (``sha256`` metadata or the raw
    content hash), so turns both sides hold never duplicate. Non-message
    events pass through unchanged.
    """
    remote = synced_messages(ws, session_id)
    if not remote:
        return local_events
    have = set()
    for e in local_events:
        if e.get("type") != "message":
            continue
        md = e.get("metadata") or {}
        sha = str(md.get("sha256") or "")
        if not sha:
            sha = hashlib.sha256(
                str(md.get("content") or "").encode("utf-8")
            ).hexdigest()
        have.add(sha)
    out = list(local_events)
    for m in remote:
        sha = hashlib.sha256(m["text"].encode("utf-8")).hexdigest()
        if sha in have:
            continue
        md: dict[str, Any] = {"role": m["role"], "content": m["text"],
                              "sha256": sha, "source": "store"}
        if m.get("persona"):
            md["persona"] = m["persona"]
        if m.get("user"):
            md["user"] = m["user"]
        out.append(
            {
                "ts": m["ts"],
                "session": session_id,
                "type": "message",
                "origin": "store",
                "metadata": md,
            }
        )
    out.sort(key=lambda e: str(e.get("ts") or ""))
    return out
