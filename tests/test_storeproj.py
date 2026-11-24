"""Synced store.db projections unioned into session surfaces."""
import json

import pytest

pytest.importorskip("agents_store")
from agents_store.db import connect  # noqa: E402
from agents_store.ingest import ingest  # noqa: E402

from agents_workspace import Host  # noqa: E402

from agents_relay.storeproj import (  # noqa: E402
    synced_dead, synced_messages, synced_sessions, tombstone_session,
    union_events)
from agents_relay.workspaces import (  # noqa: E402
    delete_session, get_session, list_sessions)

ATTRS = {
    "msg:session": ("text", "one"),
    "msg:role": ("text", "one"),
    "msg:text": ("text", "one"),
    "msg:ts": ("instant", "one"),
    "msg:host": ("text", "one"),
    "msg:persona": ("text", "one"),
    "msg:user": ("text", "one"),
    "msg:frame_id": ("text", "one"),
    "msg:deleted": ("bool", "one"),
    "session:host": ("text", "one"),
    "session:branch": ("text", "one"),
    "session:repo": ("text", "one"),
    "session:wip_unpushed": ("bool", "one"),
    "session:title": ("text", "one"),
    "session:last_active": ("instant", "one"),
    "frame:session": ("text", "one"),
    "frame:kind": ("text", "one"),
    "frame:first_ts": ("instant", "one"),
    "frame:last_ts": ("instant", "one"),
    "frame:ts": ("instant", "one"),
    "frame:host": ("text", "one"),
    "frame:retired": ("bool", "one"),
}


def _domain(conn):
    for name, (vt, card) in ATTRS.items():
        conn.execute(
            "INSERT OR IGNORE INTO attr (name, value_type, cardinality, doc)"
            " VALUES (?, ?, ?, '')", (name, vt, card))


def _hlc(ts):
    from datetime import datetime

    ms = int(datetime.fromisoformat(
        ts.replace("Z", "+00:00")).timestamp() * 1000)
    return f"{ms:013d}.00000.proj"


def _msg(conn, sid, ts, role, text, *, seq, user="", persona="", deleted=False):
    e = f"msg:{sid}:{role}:{ts}"
    datoms = [
        (e, "msg:session", sid, True),
        (e, "msg:role", role, True),
        (e, "msg:text", text, True),
        (e, "msg:ts", ts, True),
        (e, "msg:host", "hostA", True),
    ]
    if user:
        datoms.append((e, "msg:user", user, True))
    if persona:
        datoms.append((e, "msg:persona", persona, True))
    if deleted:
        datoms.append((e, "msg:deleted", "true", True))
    ingest(conn, "proj@hostA:sid", seq, _hlc(ts), datoms)


def _bind(conn, sid, *, host="hostA", branch="feat/x", wip=True, seq=99):
    e = f"session:{sid}"
    ingest(conn, "bind@hostA:sid", seq, _hlc("2026-11-22T12:00:00Z"), [
        (e, "session:host", host, True),
        (e, "session:branch", branch, True),
        (e, "session:wip_unpushed", "true" if wip else "false", True),
    ])


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_HOST_DIR", str(tmp_path / "host"))
    ws = Host.from_env().create("w1")
    ws.traces.mkdir(parents=True, exist_ok=True)
    yield ws


@pytest.fixture
def store_db(ws):
    conn = connect(ws.store_db)
    _domain(conn)
    yield conn
    conn.close()


def test_synced_only_session_lists(ws, store_db):
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", "day one opener",
         seq=1, user="me")
    _msg(store_db, "s1", "2026-11-20T09:00:05Z", "assistant", "reply", seq=2)
    rows = list_sessions(ws, persona="default", empty_grace_sec=0)
    assert [r["id"] for r in rows] == ["s1"]
    assert rows[0]["title"] == "day one opener"
    assert rows[0]["last_active"] == "2026-11-20T09:00:05Z"
    assert rows[0]["host"] == "hostA"
    assert rows[0]["persona"] == "default"


def test_synced_binding_fields_exposed(ws, store_db):
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", "hi", seq=1)
    _bind(store_db, "s1", host="hostA", branch="feat/x", wip=True)
    rows = list_sessions(ws, persona="default")
    assert rows[0]["host"] == "hostA"
    assert rows[0]["branch"] == "feat/x"
    assert rows[0]["wip_unpushed"] is True


def test_deleted_session_does_not_resurrect(ws, store_db):
    # local record exists, but the synced projection deleted every msg
    sdir = ws.personas / "default" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "s1.json").write_text(json.dumps(
        {"id": "s1", "user": "u_x", "start_date": "2026-11-20"}) + "\n",
        encoding="utf-8")
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", "gone",
         seq=1, deleted=True)
    rows = list_sessions(ws, persona="default", include_empty=True)
    assert rows == []
    assert get_session(ws, "default", "s1") is None


def test_get_session_synced_only(ws, store_db):
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", "hi", seq=1,
         persona="ops")
    rec = get_session(ws, "ops", "s1")
    assert rec is not None
    assert rec["title"] == "hi"
    # persona scoping: an "ops" session is not a "default" session
    assert get_session(ws, "default", "s1") is None


def test_union_events_merges_only_missing(ws, store_db):
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", "shared", seq=1)
    _msg(store_db, "s1", "2026-11-22T10:00:00Z", "user", "remote only",
         seq=2)
    local = [{
        "ts": "2026-11-20T09:00:00Z", "session": "s1", "type": "message",
        "metadata": {"role": "user", "content": "shared"},
    }]
    out = union_events(ws, "s1", local)
    bodies = [e["metadata"]["content"] for e in out]
    assert bodies == ["shared", "remote only"]


# ---- agents-relay#45 (R08): dedupe/undo/tombstone/listing fixes ----


def _frame(conn, sid, first_ts, last_ts, *, seq, kind="undo",
           retired=False):
    e = f"frame:{sid}:{kind}:{first_ts}"
    datoms = [
        (e, "frame:session", sid, True),
        (e, "frame:kind", kind, True),
        (e, "frame:first_ts", first_ts, True),
        (e, "frame:last_ts", last_ts, True),
        (e, "frame:ts", last_ts, True),
        (e, "frame:host", "hostA", True),
    ]
    if retired:
        datoms.append((e, "frame:retired", "true", True))
    ingest(conn, "proj@hostA:sid", seq, _hlc(last_ts), datoms)


def test_union_events_preserves_repeated_messages(ws, store_db):
    # two identical remote turns ("ok") at different ts both survive —
    # dedupe is (ts, role) + content hash, never content alone
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", "ok", seq=1)
    _msg(store_db, "s1", "2026-11-20T09:05:00Z", "user", "ok", seq=2)
    local = [{
        "ts": "2026-11-20T09:00:00Z", "session": "s1", "type": "message",
        "metadata": {"role": "user", "content": "ok"},
    }]
    out = union_events(ws, "s1", local)
    bodies = [e["metadata"]["content"] for e in out]
    assert bodies == ["ok", "ok"]


def test_union_events_spilled_ref_dedupes_by_sha(ws, store_db):
    # >20KB local content spills to <ref>; metadata.sha256 holds the
    # real body hash — the synced full-text msg must still dedupe
    import hashlib

    big = "x" * 25000
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", big, seq=1)
    local = [{
        "ts": "2026-11-20T09:00:00Z", "session": "s1", "type": "message",
        "metadata": {
            "role": "user",
            "content": '<ref digest="abc" bytes="25000">',
            "sha256": hashlib.sha256(big.encode()).hexdigest(),
        },
    }]
    out = union_events(ws, "s1", local)
    assert len(out) == 1
    assert out[0]["metadata"]["content"].startswith("<ref")


def test_union_events_remote_undo_suppresses_local(ws, store_db):
    # remote marked the msg deleted (/undo); the unsynced local copy
    # must not resurrect it
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", "undo me",
         seq=1, deleted=True)
    _msg(store_db, "s1", "2026-11-20T09:01:00Z", "user", "keep", seq=2)
    local = [{
        "ts": "2026-11-20T09:00:00Z", "session": "s1", "type": "message",
        "metadata": {"role": "user", "content": "undo me"},
    }]
    out = union_events(ws, "s1", local)
    bodies = [e["metadata"]["content"] for e in out]
    assert bodies == ["keep"]


def test_union_events_active_frame_span_suppresses_local(ws, store_db):
    # a live coverage frame suppresses any local msg inside its span —
    # even msgs never individually stamped deleted
    _frame(store_db, "s1", "2026-11-20T09:00:00Z",
           "2026-11-20T09:30:00Z", seq=3)
    local = [{
        "ts": "2026-11-20T09:10:00Z", "session": "s1", "type": "message",
        "metadata": {"role": "user", "content": "inside span"},
    }, {
        "ts": "2026-11-20T10:00:00Z", "session": "s1", "type": "message",
        "metadata": {"role": "user", "content": "outside span"},
    }]
    out = union_events(ws, "s1", local)
    bodies = [e["metadata"]["content"] for e in out]
    assert bodies == ["outside span"]


def test_union_events_retired_frame_does_not_suppress(ws, store_db):
    _frame(store_db, "s1", "2026-11-20T09:00:00Z",
           "2026-11-20T09:30:00Z", seq=3, retired=True)
    local = [{
        "ts": "2026-11-20T09:10:00Z", "session": "s1", "type": "message",
        "metadata": {"role": "user", "content": "revived"},
    }]
    out = union_events(ws, "s1", local)
    bodies = [e["metadata"]["content"] for e in out]
    assert bodies == ["revived"]


def test_union_events_sorts_when_remote_empty(ws, store_db):
    # TraceStore.iter_events yields newest-day-first — callers rely on
    # union_events to restore ascending order regardless of remote state
    local = [{
        "ts": "2026-11-22T09:00:00Z", "session": "s1", "type": "message",
        "metadata": {"role": "user", "content": "newer"},
    }, {
        "ts": "2026-11-20T09:00:00Z", "session": "s1", "type": "message",
        "metadata": {"role": "user", "content": "older"},
    }]
    out = union_events(ws, "s1", local)
    assert [e["metadata"]["content"] for e in out] == ["older", "newer"]


def test_synced_session_past_sqlite_variable_limit(ws, store_db):
    # 1200 msgs: the per-msg ord lookup must chunk its IN () clauses —
    # a raw WHERE e IN (>999 params) raises OperationalError and the
    # session silently vanishes
    for i in range(1200):
        ts = f"2026-11-20T09:{i // 60:02d}:{i % 60:02d}Z"
        _msg(store_db, "s2", ts, "user" if i % 2 else "assistant",
             f"m{i}", seq=i + 1)
    msgs = synced_messages(ws, "s2")
    assert len(msgs) == 1200
    assert msgs[0]["text"] == "m0"
    assert msgs[-1]["text"] == "m1199"
    rows = list_sessions(ws, persona="default")
    assert rows[0]["id"] == "s2"


def test_tombstone_blocks_synced_resurrection(ws, store_db):
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", "hi", seq=1)
    # synced-only session deletes via tombstone — no local JSON exists
    assert delete_session(ws, "default", "s1") is True
    assert synced_dead(ws, "s1") is True
    assert "s1" in synced_sessions(ws)["dead"]
    assert list_sessions(ws, persona="default") == []
    assert get_session(ws, "default", "s1") is None


def test_delete_session_unknown_returns_false(ws, store_db):
    assert delete_session(ws, "default", "ghost") is False


def test_tombstone_beats_live_msgs(ws, store_db):
    # explicit session:deleted wins even while live msgs remain —
    # the tombstone lands before the peer's msg retraction propagates
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", "hi", seq=1)
    assert tombstone_session(ws, "s1") is True
    assert "s1" in synced_sessions(ws)["dead"]
    assert "s1" not in synced_sessions(ws)["live"]


def test_list_sessions_user_filter_rejects_unstamped(ws, store_db):
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", "hi", seq=1,
         user="alice")
    _msg(store_db, "s2", "2026-11-21T09:00:00Z", "user", "yo", seq=2)
    rows = list_sessions(ws, persona="default", user="alice")
    ids = {r["id"] for r in rows}
    # s2 carries no msg:user stamp — unknown owner must not leak
    assert ids == {"s1"}


def test_list_sessions_live_synced_survives_grace(ws, store_db):
    # stale local record + live synced projection -> stays listed
    sdir = ws.personas / "default" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "s1.json").write_text(json.dumps(
        {"id": "s1", "user": "u_x", "start_date": "2026-11-19",
         "last_active": "2026-11-19T09:00:00Z"}) + "\n",
        encoding="utf-8")
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", "synced turn",
         seq=1)
    rows = list_sessions(ws, persona="default",
                         include_empty=False, empty_grace_sec=1)
    ids = {r["id"] for r in rows}
    assert "s1" in ids


def test_delegate_session_no_extras_resurrection(ws, store_db):
    sdir = ws.personas / "default" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "s1.json").write_text(json.dumps(
        {"id": "s1", "user": "u_x", "channel": "delegate",
         "start_date": "2026-11-19",
         "last_active": "2026-11-19T09:00:00Z"}) + "\n",
        encoding="utf-8")
    # synced projection still holds live msgs — the extras union must
    # not resurrect the dropped delegate row
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", "worker turn",
         seq=1)
    rows = list_sessions(ws, persona="default",
                         include_empty=False, empty_grace_sec=1)
    ids = {r["id"] for r in rows}
    assert "s1" not in ids


def test_list_sessions_resorts_rows_plus_extras(ws, store_db):
    sdir = ws.personas / "default" / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "old.json").write_text(json.dumps(
        {"id": "old", "user": "u_x", "start_date": "2026-11-18",
         "last_active": "2026-11-18T09:00:00Z"}) + "\n",
        encoding="utf-8")
    _msg(store_db, "new", "2026-11-21T09:00:00Z", "user", "fresh",
         seq=1, persona="default")
    rows = list_sessions(ws, persona="default", include_empty=True)
    ids = [r["id"] for r in rows]
    assert ids[0] == "new"  # most recent activity first
    assert "old" in ids


def test_list_sessions_no_traces_still_serves_synced(ws, store_db,
                                                   monkeypatch):
    # agents-traces absent -> synced-only view, never a hard 501
    import agents_relay.workspaces as wsmod

    def _no_store(_ws):
        raise RuntimeError("agents-traces required for session routes")

    monkeypatch.setattr(wsmod, "_session_store", _no_store)
    _msg(store_db, "s1", "2026-11-20T09:00:00Z", "user", "hi", seq=1)
    rows = list_sessions(ws, persona="default")
    assert [r["id"] for r in rows] == ["s1"]
    # get/delete degrade the same way
    assert get_session(ws, "default", "s1")["title"] == "hi"
    assert delete_session(ws, "default", "s1") is True
