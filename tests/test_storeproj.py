"""Synced store.db projections unioned into session surfaces."""
import json

import pytest

pytest.importorskip("agents_store")
from agents_store.db import connect  # noqa: E402
from agents_store.ingest import ingest  # noqa: E402

from agents_workspace import Host  # noqa: E402

from agents_relay.storeproj import union_events  # noqa: E402
from agents_relay.workspaces import get_session, list_sessions  # noqa: E402

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
