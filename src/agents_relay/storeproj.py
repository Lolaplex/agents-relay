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

Session tombstones (agents-relay#45): ``session:<sid>`` carrying
``session:deleted='true'`` suppresses the session everywhere — written
by ``tombstone_session`` via the same ingest path msgsync uses so the
mark survives ws-share replication on both directions.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

# SQLite caps bound variables at 999 — chunk every literal IN () well
# under that (agents-relay#45: >999-msg sessions silently vanished).
_CHUNK = 500


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


def _msg_attrs(conn, session_id: str | None = None) -> dict[str, dict]:
    """``e -> attr-map`` for projected msgs (one session or all)."""
    if session_id is None:
        sql = ("SELECT e, a, v FROM v_current WHERE e IN ("
               " SELECT e FROM v_current WHERE a = 'msg:session')")
        args: tuple = ()
    else:
        sql = ("SELECT e, a, v FROM v_current WHERE e IN ("
               " SELECT e FROM v_current WHERE a = 'msg:session'"
               " AND v = ?)")
        args = (session_id,)
    rows: dict[str, dict] = {}
    for e, a, v in conn.execute(sql, args):
        rows.setdefault(e, {"e": e})[a.split(":", 1)[1]] = v
    return rows


def _ords_for(conn, entities) -> dict[str, str]:
    """``e -> max ord`` — literal IN () chunked under the variable cap."""
    ents = [str(e) for e in entities]
    ords: dict[str, str] = {}
    for i in range(0, len(ents), _CHUNK):
        part = ents[i:i + _CHUNK]
        marks = ",".join("?" for _ in part)
        for e, o in conn.execute(
                f"SELECT e, MAX(ord) FROM datom WHERE e IN ({marks})"
                " GROUP BY e", part):
            ords[e] = str(o or "")
    return ords


def _retired_frames(conn, session_id: str | None = None) -> set[str]:
    sql = ("SELECT e FROM v_current"
           " WHERE a = 'frame:retired' AND v = 'true'")
    if session_id is None:
        return {r[0] for r in conn.execute(sql)}
    # session filter needs the join (a retired frame id is globally
    # unique anyway — the batch path skips it entirely)
    return {
        r[0]
        for r in conn.execute(
            "SELECT r.e FROM v_current r JOIN v_current s ON s.e = r.e"
            " WHERE r.a = 'frame:retired' AND r.v = 'true'"
            " AND s.a = 'frame:session' AND s.v = ?",
            (session_id,),
        )
    }


def _active_spans(conn, session_id: str) -> list[tuple[str, str]]:
    """``(first_ts, last_ts)`` of frames that are NOT retired — msgs
    under them were superseded upstream (/undo, compaction) and must
    suppress unsynced local events at the same ts (agents-relay#45)."""
    frames: dict[str, dict] = {}
    for e, a, v in conn.execute(
            "SELECT e, a, v FROM v_current WHERE e IN ("
            " SELECT e FROM v_current"
            " WHERE a = 'frame:session' AND v = ?)",
            (session_id,)):
        frames.setdefault(e, {})[a.split(":", 1)[1]] = str(v)
    return [
        (str(f["first_ts"]), str(f["last_ts"]))
        for f in frames.values()
        if f.get("retired") != "true"
        and f.get("first_ts") and f.get("last_ts")
    ]


def _live_rows(rows: dict[str, dict], retired: set[str],
               ords: dict[str, str]) -> list[dict]:
    live = [
        r
        for r in rows.values()
        if r.get("deleted") != "true"
        and str(r.get("frame_id") or "") not in retired
    ]
    live.sort(
        key=lambda r: (
            str(r.get("ts") or ""),
            ords.get(r["e"], ""),
            r["e"],
        )
    )
    return live


def _live(conn, session_id: str) -> list[dict]:
    rows = _msg_attrs(conn, session_id)
    return _live_rows(
        rows, _retired_frames(conn, session_id), _ords_for(conn, rows))


def _session_attrs(conn, session_id: str) -> dict[str, str]:
    return {
        a.split(":", 1)[1]: str(v)
        for a, v in conn.execute(
            "SELECT a, v FROM v_current WHERE e = ? AND a LIKE 'session:%'",
            (f"session:{session_id}",),
        )
    }


def _all_session_attrs(conn) -> dict[str, dict[str, str]]:
    """``sid -> {attr: v}`` for every ``session:<sid>`` entity — one
    query, no per-session N+1 (agents-relay#45)."""
    out: dict[str, dict[str, str]] = {}
    for e, a, v in conn.execute(
            "SELECT e, a, v FROM v_current WHERE a LIKE 'session:%'"):
        sid = str(e).split(":", 1)[1] if str(e).startswith("session:") \
            else ""
        if sid:
            out.setdefault(sid, {})[a.split(":", 1)[1]] = str(v)
    return out


def _meta_from(session_id: str, attrs: dict[str, str],
               live: list[dict]) -> dict[str, Any]:
    """session:* binding + title/last_active + first persona/user seen."""
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


def _meta_for(conn, session_id: str, live: list[dict]) -> dict[str, Any]:
    return _meta_from(
        session_id, _session_attrs(conn, session_id), live)


def synced_sessions(ws) -> dict:
    """All projected sessions: ``{"live": {sid: meta}, "dead": set}``.

    ``dead`` holds sessions whose msgs are all deleted OR whose
    ``session:<sid>`` entity carries ``session:deleted='true'`` — synced
    tombstones that must suppress any resurrecting local record.
    Batched queries only: no per-session N+1 (agents-relay#45).
    """
    conn = _connect(ws)
    if conn is None:
        return {"live": {}, "dead": set()}
    try:
        msg_sid = {
            str(e): str(v)
            for e, v in conn.execute(
                "SELECT e, v FROM v_current WHERE a = 'msg:session'")
        }
        all_rows = _msg_attrs(conn)
        msgs: dict[str, dict[str, dict]] = {}
        for e, row in all_rows.items():
            sid = msg_sid.get(e)
            if sid is not None:
                msgs.setdefault(sid, {})[e] = row
        retired = _retired_frames(conn)
        ords = _ords_for(conn, all_rows)
        sattrs = _all_session_attrs(conn)
        live_map: dict[str, dict] = {}
        dead: set[str] = set()
        for sid in set(msg_sid.values()) | set(sattrs):
            if str((sattrs.get(sid) or {}).get("deleted") or "") == "true":
                dead.add(sid)
                continue
            live = _live_rows(msgs.get(sid) or {}, retired, ords)
            if live:
                live_map[sid] = _meta_from(
                    sid, sattrs.get(sid) or {}, live)
            elif sid in msgs:
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
        attrs = _session_attrs(conn, session_id)
        if attrs.get("deleted") == "true":
            return None
        msgs = _live(conn, session_id)
        if not msgs:
            return None
        return _meta_from(session_id, attrs, msgs)
    except Exception:
        return None
    finally:
        conn.close()


def synced_dead(ws, session_id: str) -> bool:
    """The session is tombstoned (``session:deleted='true'``) or has
    projected msgs and every one is deleted — a synced tombstone that
    suppresses any resurrecting local record."""
    conn = _connect(ws)
    if conn is None:
        return False
    try:
        attrs = _session_attrs(conn, session_id)
        if attrs.get("deleted") == "true":
            return True
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


def session_known(ws, session_id: str) -> bool:
    """Any projected trace of the session — msgs or a session:* entity."""
    conn = _connect(ws)
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM v_current WHERE a = 'msg:session' AND v = ?"
            " LIMIT 1", (session_id,)).fetchone()
        if row:
            return True
        row = conn.execute(
            "SELECT 1 FROM v_current WHERE e = ? AND a LIKE 'session:%'"
            " LIMIT 1", (f"session:{session_id}",)).fetchone()
        return bool(row)
    except Exception:
        return False
    finally:
        conn.close()


def _node() -> str:
    try:
        from agents_workspace import Host

        return str(Host.from_env().host_id())
    except Exception:  # noqa: BLE001 - no host dir (tests, bare CLI)
        import socket

        return f"host_{socket.gethostname()}"


def _node_part(node: str) -> str:
    try:
        from agents_store.hlc import normalize_node_id

        return normalize_node_id(node)
    except Exception:  # noqa: BLE001 - older agents-store without hlc
        import re

        return re.sub(r"[^A-Za-z0-9_-]", "_", node or "relay")[:32] \
            or "relay"


def _next_seq(conn, origin_actor: str) -> int:
    """Next origin_seq — continues past anything the store already
    holds so a lost cursor can never reuse a seq (klanker msgsync)."""
    row = conn.execute(
        "SELECT MAX(origin_seq) FROM tx WHERE origin_actor = ?",
        (origin_actor,)).fetchone()
    return int(row[0] or 0) + 1 if row else 1


def _fresh_ord(conn, node: str) -> str:
    """Ord strictly above every datom already in the store — tombstones
    are rewrites relative to the projected stream and must win
    v_current's max-ord rule on every host, replay included."""
    ms = int(time.time() * 1000)
    row = conn.execute("SELECT MAX(ord) FROM datom").fetchone()
    head = str(row[0]).split(".", 1)[0] if row and row[0] else ""
    if head.isdigit():
        ms = max(ms, int(head) + 1)
    return f"{ms:013d}.00000.{_node_part(node)}"


def tombstone_session(ws, session_id: str, *, node: str = "") -> bool:
    """Write ``session:deleted='true'`` into store.db so a synced copy
    of the session can never resurrect on this host or any peer it
    re-shares to (agents-relay#45). Same ingest path msgsync uses."""
    conn = _connect(ws)
    if conn is None:
        return False
    try:
        conn.execute(
            "INSERT OR IGNORE INTO attr"
            " (name, value_type, cardinality, doc)"
            " VALUES ('session:deleted', 'bool', 'one',"
            " 'session tombstone')")
        from agents_store.ingest import ingest

        node = node or _node()
        origin = f"relay@{node}:{session_id}"
        res = ingest(
            conn, origin, _next_seq(conn, origin),
            _fresh_ord(conn, node),
            [(f"session:{session_id}", "session:deleted", "true", True)],
        )
        return bool(getattr(res, "applied", 0) or getattr(res, "dupes", 0))
    except Exception:  # noqa: BLE001 - tombstone must never crash delete
        return False
    finally:
        conn.close()


def synced_messages(ws, session_id: str) -> list[dict]:
    """Live ``msg:*`` rows ``{role,text,ts,host,persona,user}``."""
    conn = _connect(ws)
    if conn is None:
        return []
    try:
        return _shape_msgs(_live(conn, session_id))
    except Exception:
        return []
    finally:
        conn.close()


def _shape_msgs(live: list[dict]) -> list[dict]:
    return [
        {
            "role": str(r.get("role") or "user"),
            "text": str(r.get("text") or ""),
            "ts": str(r.get("ts") or ""),
            "host": str(r.get("host") or ""),
            "persona": str(r.get("persona") or ""),
            "user": str(r.get("user") or ""),
        }
        for r in live
    ]


def _norm(text: str) -> str:
    """Whitespace-normalized message body — dedupe fallback so a
    spilled ``<ref>`` payload or a reformatted body still matches."""
    return " ".join(str(text or "").split())


def union_events(ws, session_id: str, local_events: list[dict]) -> list[dict]:
    """Local trace events + synced msgs the local trace lacks.

    Dedupe is ``(ts, role)`` plus a content-hash fallback (raw and
    whitespace-normalized sha256 — covers ``metadata.sha256`` from
    spilled ``<ref>`` msgs), so repeated turns (``"ok"``, ``"yes"``)
    survive while genuinely shared msgs never duplicate. Local message
    events covered by ``msg:deleted='true'`` or an active ``frame:*``
    span in store.db are suppressed — a remote /undo must not resurrect
    turns the peer already dropped (agents-relay#45). Output is always
    ts-ascending, even when the remote side is empty.
    """
    conn = _connect(ws)
    remote: list[dict] = []
    dead_ts: set[str] = set()
    spans: list[tuple[str, str]] = []
    if conn is not None:
        try:
            rows = _msg_attrs(conn, session_id)
            retired = _retired_frames(conn, session_id)
            ords = _ords_for(conn, rows)
            for r in rows.values():
                ts = str(r.get("ts") or "")
                if r.get("deleted") == "true" \
                        or str(r.get("frame_id") or "") in retired:
                    if ts:
                        dead_ts.add(ts)
            remote = _shape_msgs(_live_rows(rows, retired, ords))
            spans = _active_spans(conn, session_id)
        except Exception:
            remote = []
        finally:
            conn.close()

    def _suppressed(e: dict) -> bool:
        if e.get("type") != "message":
            return False
        ts = str(e.get("ts") or "")
        if ts and ts in dead_ts:
            return True
        return bool(ts) and any(f <= ts <= l for f, l in spans)

    kept = [e for e in local_events if not _suppressed(e)]
    have_keys: set[tuple[str, str]] = set()
    # (declared sha256, ts) — the fallback for spilled <ref> locals whose
    # stored content is the marker, not the body. Content alone never
    # dedupes: repeated "ok"/"yes" turns at distinct ts are real msgs.
    have_hash: set[tuple[str, str]] = set()
    for e in kept:
        if e.get("type") != "message":
            continue
        md = e.get("metadata") or {}
        ts = str(e.get("ts") or "")
        role = str(md.get("role") or e.get("role") or "")
        have_keys.add((ts, role))
        sha = str(md.get("sha256") or "")
        if sha:
            have_hash.add((sha, ts))
        content = str(md.get("content") or "")
        if content:
            have_hash.add((hashlib.sha256(
                content.encode("utf-8")).hexdigest(), ts))
            have_hash.add((hashlib.sha256(
                _norm(content).encode("utf-8")).hexdigest(), ts))
    out = list(kept)
    for m in remote:
        if (m["ts"], m["role"]) in have_keys:
            continue
        raw = hashlib.sha256(m["text"].encode("utf-8")).hexdigest()
        normed = hashlib.sha256(
            _norm(m["text"]).encode("utf-8")).hexdigest()
        if (raw, m["ts"]) in have_hash \
                or (normed, m["ts"]) in have_hash:
            continue
        md: dict[str, Any] = {"role": m["role"], "content": m["text"],
                              "sha256": raw, "source": "store"}
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
