"""Per-workspace agents-store HTTP API, CalDAV config, and capabilities tests."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import tempfile
import threading
import types
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from agents_relay import events, store_api
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_workspace import Host


def _cfg(**overrides) -> RelayConfig:
    base = dict(
        loop_cmd=("python", "-c", "print('skip')"),
        loop_provider="openai.default",
        relay_secret="expected",
        telegram_bot_token="",
        telegram_allowed_chat_ids=(),
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
        web_token="tok",
        web_user="web",
    )
    base.update(overrides)
    return RelayConfig(**base)


class _Srv:
    def __init__(self, cfg: RelayConfig) -> None:
        self.server = serve_http(cfg)
        self.server.events_heartbeat_sec = 0.05  # type: ignore[attr-defined]
        self.host, self.port = self.server.server_address  # type: ignore[misc]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _req(srv: _Srv, method: str, path: str, body: dict | None = None, *, auth: bool = True) -> tuple[int, dict]:
    conn = HTTPConnection(srv.host, srv.port, timeout=3)
    hdrs: dict[str, str] = {}
    if auth:
        hdrs["Authorization"] = "Bearer expected"
    raw = None
    if body is not None:
        hdrs["Content-Type"] = "application/json"
        raw = json.dumps(body).encode("utf-8")
    conn.request(method, path, body=raw, headers=hdrs)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8") or "{}")
    status = resp.status
    conn.close()
    return status, data


def _patch_agents_keys_spec():
    orig = importlib.util.find_spec

    def _find(name: str, package: str | None = None):
        if name == "agents_keys":
            return types.SimpleNamespace(origin="fake")
        return orig(name, package)

    return patch("importlib.util.find_spec", side_effect=_find)


class TestStoreApi(unittest.TestCase):
    def tearDown(self) -> None:
        events.shutdown_all()
        events.reset_bus()
        store_api._DID_CACHE.clear()

    def test_returns_501_when_agents_store_missing(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"AGENTS_HOST_DIR": td}):
                Host.from_env().create("alpha", "Alpha")
                srv = _Srv(_cfg())
                try:
                    with patch.object(store_api, "_load_store_modules", return_value=None):
                        st1, d1 = _req(srv, "GET", "/v1/workspaces/alpha/store/items")
                        st2, d2 = _req(srv, "GET", "/v1/workspaces/alpha/store/pane/ent-1")
                        st3, d3 = _req(
                            srv,
                            "POST",
                            "/v1/workspaces/alpha/store/append",
                            {"domain": "records", "payload": {"kind": "note", "title": "t", "body": "b"}},
                        )
                    self.assertEqual(st1, 501)
                    self.assertEqual(st2, 501)
                    self.assertEqual(st3, 501)
                    self.assertIn("not installed", d1.get("error", ""))
                    self.assertIn("not installed", d2.get("error", ""))
                    self.assertIn("not installed", d3.get("error", ""))
                finally:
                    srv.close()

    @unittest.skipUnless(
        importlib.util.find_spec("agents_store") is not None,
        "agents-store optional extra not installed",
    )
    def test_store_roundtrip_anti_spoofing_sse_and_delete(self):
        with tempfile.TemporaryDirectory() as td:
            env = {k: v for k, v in os.environ.items() if k != "AGENTS_ACTOR_DID"}
            env["AGENTS_HOST_DIR"] = td
            with patch.dict(os.environ, env, clear=True):
                host = Host.from_env()
                host.create("alpha", "Alpha")
                host.create("beta", "Beta")
                srv = _Srv(_cfg())
                try:
                    stream = HTTPConnection(srv.host, srv.port, timeout=3)
                    stream.request("GET", "/v1/events", headers={"Authorization": "Bearer expected"})
                    ev_resp = stream.getresponse()
                    self.assertEqual(ev_resp.status, 200)
                    _ = ev_resp.readline()

                    # Spoofed actor in both top-level body and payload MUST be ignored.
                    st_ev, res_ev = _req(
                        srv,
                        "POST",
                        "/v1/workspaces/alpha/store/append",
                        {
                            "domain": "records",
                            "actor": "did:key:evil",
                            "payload": {
                                "actor": "did:key:evil",
                                "kind": "event",
                                "title": "Standup",
                                "when": "2026-12-01T09:00:00Z",
                            },
                        },
                    )
                    self.assertEqual(st_ev, 200)
                    self.assertTrue(res_ev["ok"])
                    ev_id = res_ev["id"]
                    self.assertNotEqual(res_ev["actor"], "did:key:evil")
                    expected_actor = res_ev["actor"]

                    st_tk, res_tk = _req(
                        srv,
                        "POST",
                        "/v1/workspaces/alpha/store/append",
                        {
                            "domain": "records",
                            "payload": {
                                "kind": "task",
                                "title": "Ship release",
                                "status": "open",
                            },
                        },
                    )
                    self.assertEqual(st_tk, 200)
                    self.assertTrue(res_tk["ok"])

                    st_list, listed = _req(srv, "GET", "/v1/workspaces/alpha/store/items?kind=event")
                    self.assertEqual(st_list, 200)
                    self.assertEqual(len(listed["items"]), 1)
                    item = listed["items"][0]
                    self.assertEqual(item["id"], ev_id)
                    self.assertEqual(item["attrs"]["title"], "Standup")
                    self.assertEqual(item["actor"], expected_actor)

                    st_ical, ical = _req(srv, "GET", f"/v1/workspaces/alpha/store/pane/{ev_id}?target=ical")
                    self.assertEqual(st_ical, 200)
                    self.assertIn("BEGIN:VCALENDAR", ical["content"])
                    self.assertIn("BEGIN:VEVENT", ical["content"])

                    st_bad, bad_pane = _req(
                        srv,
                        "GET",
                        f"/v1/workspaces/alpha/store/pane/{ev_id}?target=unknown-fmt",
                    )
                    self.assertEqual(st_bad, 422)
                    self.assertFalse(bad_pane["ok"])
                    self.assertIn("refusal", bad_pane)

                    st_patch, patched = _req(
                        srv,
                        "PATCH",
                        "/v1/workspaces/alpha",
                        {"label": "Alpha Renamed"},
                    )
                    self.assertEqual(st_patch, 200)
                    self.assertEqual(patched["workspace"]["label"], "Alpha Renamed")

                    seen_kinds: list[str] = []
                    for _ in range(500):
                        raw_line = ev_resp.readline().decode("utf-8", errors="replace").strip()
                        if raw_line.startswith("event: "):
                            seen_kinds.append(raw_line[7:])
                        if "store.changed" in seen_kinds and "workspace.changed" in seen_kinds:
                            break
                    self.assertIn("store.changed", seen_kinds)
                    self.assertIn("workspace.changed", seen_kinds)
                    stream.close()

                    # Hard delete after SQLite store operations must not hit locked file handles.
                    st_del, del_res = _req(srv, "DELETE", "/v1/workspaces/alpha?hard=1")
                    self.assertEqual(st_del, 200)
                    self.assertTrue(del_res["ok"])
                finally:
                    srv.close()

    def test_capabilities_auth_and_optional_caldav_config(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"AGENTS_HOST_DIR": td}):
                host = Host.from_env()
                host.create("work", "Work")
                self.assertIsNone(store_api.caldav_config_for_host(host))

                srv = _Srv(_cfg())
                try:
                    conn = HTTPConnection(srv.host, srv.port, timeout=3)
                    conn.request("GET", "/v1/capabilities")
                    r = conn.getresponse()
                    r.read()
                    conn.close()
                    self.assertEqual(r.status, 401)

                    st, caps = _req(srv, "GET", "/v1/capabilities")
                    self.assertEqual(st, 200)
                    self.assertTrue(caps["ok"])
                    self.assertIn("store", caps)
                    self.assertIn("voice", caps)
                    self.assertFalse(caps["caldav"])
                finally:
                    srv.close()

                fake_rad = types.SimpleNamespace(origin="fake")
                with patch.dict(os.environ, {"AGENTS_CALDAV_ENABLE": "1"}), patch(
                    "importlib.util.find_spec",
                    side_effect=lambda name: fake_rad if name == "radicale_agents_store" else None,
                ):
                    cfg_cal = store_api.caldav_config_for_host(host)
                self.assertIsNotNone(cfg_cal)
                assert cfg_cal is not None
                self.assertTrue(cfg_cal["enabled"])
                self.assertEqual(cfg_cal["storage_type"], "radicale_agents_store")
                self.assertEqual(cfg_cal["collections"][0]["workspace"], "work")

    def test_store_append_strips_spoofed_actor_and_emits_event_without_real_pkg(self):
        class _FakeRefusal:
            def __init__(self, wrapper="records", reason="invalid_value", payload="bad"):
                self.wrapper = wrapper
                self.reason = reason
                self.e = None
                self.a = None
                self.payload = payload

        class _FakeConn:
            def __init__(self):
                self.closed = False

            def execute(self, *_a, **_kw):
                return None

            def close(self):
                self.closed = True

        seen_calls: list[dict] = []
        fake_db = types.SimpleNamespace(connect=lambda _p: _FakeConn())

        def _fake_append(domain, payload, actor, *, conn, reg=None):
            seen_calls.append({"domain": domain, "payload": dict(payload), "actor": actor})
            if payload.get("refuse"):
                return _FakeRefusal()
            return "ent-fake-1"

        fake_surface = types.SimpleNamespace(
            default_registry=lambda: None,
            append=_fake_append,
            query_entities=lambda _c, **_kw: [{"id": "ent-fake-1"}],
            render_pane=lambda _c, _r, eid, target="markdown": {"target": target, "content": f"hi {eid}"},
        )

        with tempfile.TemporaryDirectory() as td:
            env = {k: v for k, v in os.environ.items() if k != "AGENTS_ACTOR_DID"}
            env["AGENTS_HOST_DIR"] = td
            with patch.dict(os.environ, env, clear=True):
                Host.from_env().create("alpha", "Alpha")
                srv = _Srv(_cfg())
                try:
                    stream = HTTPConnection(srv.host, srv.port, timeout=3)
                    stream.request("GET", "/v1/events", headers={"Authorization": "Bearer expected"})
                    ev_resp = stream.getresponse()
                    self.assertEqual(ev_resp.status, 200)
                    _ = ev_resp.readline()

                    with patch.object(
                        store_api,
                        "_load_store_modules",
                        return_value=(fake_db, fake_surface, _FakeRefusal),
                    ):
                        st, res = _req(
                            srv,
                            "POST",
                            "/v1/workspaces/alpha/store/append",
                            {
                                "domain": "records",
                                "actor": "did:key:evil",
                                "payload": {"actor": "did:key:evil", "title": "Clean"},
                            },
                        )
                        self.assertEqual(st, 200)
                        self.assertEqual(res["id"], "ent-fake-1")
                        self.assertNotEqual(res["actor"], "did:key:evil")
                        self.assertEqual(len(seen_calls), 1)
                        self.assertNotIn("actor", seen_calls[0]["payload"])
                        self.assertNotEqual(seen_calls[0]["actor"], "did:key:evil")

                        st_ref, ref_res = _req(
                            srv,
                            "POST",
                            "/v1/workspaces/alpha/store/append",
                            {"domain": "records", "payload": {"refuse": True}},
                        )
                        self.assertEqual(st_ref, 422)
                        self.assertEqual(ref_res["refusal"]["reason"], "invalid_value")

                    st_p, _ = _req(srv, "PATCH", "/v1/workspaces/alpha", {"label": "Renamed"})
                    self.assertEqual(st_p, 200)

                    seen_kinds: list[str] = []
                    for _ in range(500):
                        raw_line = ev_resp.readline().decode("utf-8", errors="replace").strip()
                        if raw_line.startswith("event: "):
                            seen_kinds.append(raw_line[7:])
                        if "store.changed" in seen_kinds and "workspace.changed" in seen_kinds:
                            break
                    self.assertIn("store.changed", seen_kinds)
                    self.assertIn("workspace.changed", seen_kinds)
                    stream.close()
                finally:
                    srv.close()

    def test_store_append_ignores_body_persona_spoof(self):
        class _FakeConn:
            def execute(self, *_a, **_kw):
                return None

            def close(self):
                return None

        seen_actors: list[str] = []
        fake_db = types.SimpleNamespace(connect=lambda _p: _FakeConn())
        fake_surface = types.SimpleNamespace(
            default_registry=lambda: None,
            append=lambda _d, _p, actor, *, conn, reg=None: seen_actors.append(actor) or "ent-1",
        )
        with tempfile.TemporaryDirectory() as td:
            env = {k: v for k, v in os.environ.items() if k != "AGENTS_ACTOR_DID"}
            env["AGENTS_HOST_DIR"] = td
            with patch.dict(os.environ, env, clear=True):
                host = Host.from_env()
                ws = host.create("alpha", "Alpha")
                expected_host = f"host:{host.host_id()}"
                host.keys.mkdir(parents=True, exist_ok=True)
                (host.keys / "alice.ed25519").write_bytes(b"k1" * 32)
                fake_sub = types.SimpleNamespace(
                    run=lambda *_a, **_kw: types.SimpleNamespace(returncode=0, stdout="did:key:z6MkAlice\n")
                )
                with (
                    _patch_agents_keys_spec(),
                    patch.object(store_api, "_load_store_modules", return_value=(fake_db, fake_surface, ValueError)),
                    patch.object(store_api, "subprocess", fake_sub),
                ):
                    st, res = store_api.handle_store_append(
                        ws,
                        {"domain": "records", "persona": "alice", "payload": {"kind": "note", "body": "hi"}},
                    )
                self.assertEqual(st, 200)
                self.assertEqual(seen_actors, [expected_host])
                self.assertEqual(res["actor"], expected_host)

    def test_resolve_store_actor_fast_path_and_rotation_invalidation(self):
        with tempfile.TemporaryDirectory() as td:
            env = {k: v for k, v in os.environ.items() if k != "AGENTS_ACTOR_DID"}
            env["AGENTS_HOST_DIR"] = td
            with patch.dict(os.environ, env, clear=True):
                host = Host.from_env()
                ws = host.create("alpha", "Alpha")
                expected_host = f"host:{host.host_id()}"
                calls: list[list[str]] = []
                responses = iter(["did:key:z6MkFirst\n", "did:key:z6MkRotated\n"])

                def _fake_run(cmd, **_kw):
                    calls.append(list(cmd))
                    return types.SimpleNamespace(returncode=0, stdout=next(responses))

                with (
                    _patch_agents_keys_spec(),
                    patch.object(store_api, "subprocess", types.SimpleNamespace(run=_fake_run)),
                ):
                    did0 = store_api.resolve_store_actor(ws, persona="alice")
                    self.assertEqual(did0, expected_host)
                    self.assertEqual(calls, [])

                    host.keys.mkdir(parents=True, exist_ok=True)
                    key_path = Path(host.keys) / "alice.ed25519"
                    key_path.write_bytes(b"a" * 64)
                    self.assertEqual(store_api.resolve_store_actor(ws, persona="alice"), "did:key:z6MkFirst")
                    self.assertEqual(store_api.resolve_store_actor(ws, persona="alice"), "did:key:z6MkFirst")
                    self.assertEqual(len(calls), 1)

                    key_path.write_bytes(b"b" * 65)
                    self.assertEqual(store_api.resolve_store_actor(ws, persona="alice"), "did:key:z6MkRotated")
                    self.assertEqual(len(calls), 2)

    def test_event_bus_logs_warning_when_queue_drops_oldest(self):
        bus = events.EventBus(max_queue=1)
        sub = bus.subscribe()
        try:
            with self.assertLogs("agents_relay.events", level=logging.WARNING) as cm:
                bus.emit("store.changed", {"id": "1"})
                bus.emit("store.changed", {"id": "2"})
            self.assertTrue(any("dropped oldest event" in line for line in cm.output))
            kept = sub.queue.get_nowait()
            self.assertIsNotNone(kept)
            assert kept is not None
            self.assertEqual(kept["payload"]["id"], "2")
        finally:
            bus.unsubscribe(sub)

    def test_container_rewrite_populates_actor_did_for_turn_persona(self):
        from agents_relay import turn as relay_turn

        with tempfile.TemporaryDirectory() as td:
            env = {k: v for k, v in os.environ.items() if k != "AGENTS_ACTOR_DID"}
            env["AGENTS_HOST_DIR"] = td
            with patch.dict(os.environ, env, clear=True):
                host = Host.from_env()
                host.create("alpha", "Alpha")
                host.keys.mkdir(parents=True, exist_ok=True)
                (host.keys / "researcher.ed25519").write_bytes(b"r" * 64)
                with (
                    _patch_agents_keys_spec(),
                    patch.object(
                        store_api,
                        "subprocess",
                        types.SimpleNamespace(
                            run=lambda *_a, **_kw: types.SimpleNamespace(returncode=0, stdout="did:key:z6MkResearcher\n")
                        ),
                    ),
                ):
                    _argv, out_env, _ws = relay_turn._container_rewrite(
                        ["python", "-m", "runner.loop", "--persona", "researcher"],
                        "alpha",
                        "",
                    )
                self.assertIsNotNone(out_env)
                assert out_env is not None
                self.assertEqual(out_env.get("AGENTS_ACTOR_DID"), "did:key:z6MkResearcher")


if __name__ == "__main__":
    unittest.main()
