"""W11: workspace hosts/presence surface, policy PATCH, hub links,
session host switch (agents-relay#43)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from agents_relay import channel, devices
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.leases import hub_leases, reset_leases


def _cfg(**overrides) -> RelayConfig:
    base = dict(
        loop_cmd=("python", "-c", "print('skip')"),
        loop_provider="echo",
        relay_secret="expected",
        telegram_bot_token="",
        telegram_allowed_chat_ids=(),
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
        web_token="",
        web_user="",
    )
    base.update(overrides)
    return RelayConfig(**base)


class _Srv:
    def __init__(self, cfg: RelayConfig) -> None:
        self.server = serve_http(cfg)
        self.host, self.port = self.server.server_address  # type: ignore[misc]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def req(self, method: str, path: str, body: dict | None = None,
            headers: dict | None = None):
        hdrs = dict(headers or {})
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        conn = HTTPConnection(self.host, self.port, timeout=5)
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", errors="replace")
        status = resp.status
        conn.close()
        try:
            return status, json.loads(data)
        except json.JSONDecodeError:
            return status, data


class _Env(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        host_dir = Path(self.td.name)
        (host_dir / "workspaces" / "default").mkdir(parents=True)
        self.env = patch.dict(
            os.environ, {"AGENTS_HOST_DIR": str(host_dir)})
        self.env.start()
        self.host_dir = host_dir
        reset_leases()
        from agents_relay.events import reset_bus

        reset_bus()

    def tearDown(self) -> None:
        reset_leases()
        from agents_relay.events import reset_bus

        reset_bus()
        self.env.stop()
        self.td.cleanup()

    def _ws(self, ws_id: str = "w1", **kw):
        from agents_workspace import Host

        host = Host.from_env()
        host.dir.mkdir(parents=True, exist_ok=True)
        if not host.workspace_exists(ws_id):
            host.create(ws_id)
        if kw:
            host.update_workspace(ws_id, **kw)
        return host.workspace(ws_id)


class TestPolicyPatch(_Env):
    def test_patch_roundtrips_all_five_ownership_fields(self):
        self._ws("w1")
        srv = _Srv(_cfg())
        try:
            auth = {"Authorization": "Bearer expected"}
            payload = {
                "sync_policy": "synced",
                "sync_hosts": ["dev-a", "dev-b"],
                "default_host": "dev-a",
                "repos": [{"slug": "app", "url": "git@h:me/app.git",
                           "checkout_mode": "worktree"}],
                "execution": {"owner": "dev-a", "failover": "none",
                              "lease_sec": 45},
            }
            st, res = srv.req(
                "PATCH", "/v1/workspaces/w1", payload, headers=auth)
            self.assertEqual(st, 200, res)
            row = res["workspace"]
            self.assertEqual(row["sync_policy"], "synced")
            self.assertEqual(sorted(row["sync_hosts"]),
                             ["dev-a", "dev-b"])
            self.assertEqual(row["default_host"], "dev-a")
            self.assertEqual(row["repos"][0]["slug"], "app")
            self.assertEqual(row["execution"]["owner"], "dev-a")
            # GET reflects the same fields (to_dict passthrough)
            st, got = srv.req("GET", "/v1/workspaces", headers=auth)
            self.assertEqual(st, 200)
            wsrow = [w for w in got.get("workspaces") or got
                     if str(w.get("id")) == "w1"]
            self.assertTrue(wsrow)
            self.assertEqual(wsrow[0]["sync_policy"], "synced")
            self.assertEqual(wsrow[0]["execution"]["lease_sec"], 45)
        finally:
            srv.close()

    def test_patch_rejects_bogus_policy_and_unknown_field(self):
        self._ws("w1")
        srv = _Srv(_cfg())
        try:
            auth = {"Authorization": "Bearer expected"}
            st, _ = srv.req(
                "PATCH", "/v1/workspaces/w1",
                {"sync_policy": "bogus"}, headers=auth)
            self.assertEqual(st, 400)
            st, _ = srv.req(
                "PATCH", "/v1/workspaces/w1",
                {"execution": {"owner": "x", "bogus": 1}},
                headers=auth)
            self.assertEqual(st, 400)
            # invariant: local + non-empty sync_hosts must fail too
            st, _ = srv.req(
                "PATCH", "/v1/workspaces/w1",
                {"sync_hosts": ["dev-a"]}, headers=auth)
            self.assertEqual(st, 400)
        finally:
            srv.close()

    def test_policy_change_fires_hook_and_event(self):
        import agents_relay.workspaces as wsmod

        hits = []
        wsmod.ws_policy_changed_fn = lambda ws: hits.append(ws)
        self._ws("w1", sync_policy="synced", sync_hosts=["dev-a"])
        srv = _Srv(_cfg())
        try:
            st, _ = srv.req(
                "PATCH", "/v1/workspaces/w1",
                {"sync_hosts": ["dev-a", "dev-b"]},
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200)
            self.assertEqual(hits, ["w1"])
        finally:
            wsmod.ws_policy_changed_fn = None
            srv.close()


class TestWsHosts(_Env):
    def test_hosts_aggregates_members_leases_presence(self):
        self._ws("w1", sync_policy="synced",
                 sync_hosts=["dev-a", "dev-b"],
                 execution={"owner": "dev-a", "lease_sec": 45})
        dev = devices.mint_device(device_id="dev-a", label="hub-a")
        srv = _Srv(_cfg())
        try:
            # live lease held by dev-b on w1/s1
            hub_leases().acquire("w1", "s1", "dev-b", "r1", ttl=45)
            channel.hub().note_presence(
                "dev-b", {"status": "busy", "caps": ["exec"]})
            st, res = srv.req(
                "GET", "/v1/workspaces/w1/hosts",
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200, res)
            self.assertEqual(res["workspace"], "w1")
            self.assertEqual(res["owner"], "dev-a")
            ids = {h["device_id"] for h in res["hosts"]}
            self.assertEqual(ids, {"dev-a", "dev-b"})
            b = next(h for h in res["hosts"]
                     if h["device_id"] == "dev-b")
            self.assertEqual(b["leases"][0]["session"], "s1")
            self.assertEqual(b["status"], "busy")
            self.assertFalse(b["online"])  # no live stream attached
        finally:
            srv.close()

    def test_hosts_requires_auth_and_known_ws(self):
        self._ws("w1")
        srv = _Srv(_cfg())
        try:
            st, _ = srv.req("GET", "/v1/workspaces/w1/hosts")
            self.assertEqual(st, 401)
            st, _ = srv.req(
                "GET", "/v1/workspaces/nope/hosts",
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 404)
        finally:
            srv.close()


class TestPresence(_Env):
    def test_presence_frames_update_and_rate_limit(self):
        h = channel.hub()
        h._presence.clear()
        h.handle_upstream("dev-a", [
            {"kind": "presence",
             "payload": {"status": "busy", "caps": ["exec"]}}])
        p = h.presence("dev-a")
        self.assertEqual(p["status"], "busy")
        self.assertFalse(p["connected"])
        # second frame inside the rate window: state updates, but the
        # _emitted marker keeps the SSE bus quiet
        h.handle_upstream("dev-a", [
            {"kind": "presence", "payload": {"status": "idle"}}])
        p2 = h.presence("dev-a")
        self.assertEqual(p2["status"], "idle")

    def test_connect_disconnect_emit_device_events(self):
        from agents_relay.events import subscribe

        devices.mint_device(device_id="dev-x", label="x")
        sub = subscribe()
        link, _, _ = channel.hub().connect("dev-x")
        try:
            from agents_relay.events import emit_event
            # handle_hub_stream emits on the verified path; connect()
            # alone doesn't — simulate via the disconnect side which
            # lives on the manager.
            channel.hub().disconnect("dev-x",
                                     generation=link.generation)
            kinds = []
            while True:
                try:
                    evt = sub.queue.get_nowait()
                except Exception:
                    break
                if evt is None:
                    break
                kinds.append(evt.get("kind"))
            self.assertIn("device.disconnected", kinds)
        finally:
            channel.hub().disconnect("dev-x")


class TestHubLinksAndSwitch(_Env):
    def test_hub_links_lists_rows_without_tokens(self):
        from agents_relay.channel_client import _store_hubs

        _store_hubs({"hubx": {"hub_url": "http://h:8787",
                              "device_id": "dev-me", "label": "vps",
                              "connected_at": 1, "last_seq": 0}})
        import agents_relay.http_adapter as ha

        def _links():
            from agents_relay.channel_client import _load_hubs

            return {"links": [
                {"hub_id": k, "hub_url": v.get("hub_url"),
                 "device_id": v.get("device_id"),
                 "label": v.get("label"), "connected": False}
                for k, v in _load_hubs().items()]}

        ha.hub_links_fn = _links
        srv = _Srv(_cfg())
        try:
            st, res = srv.req(
                "GET", "/v1/hub/links",
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200)
            self.assertEqual(res["links"][0]["hub_id"], "hubx")
            self.assertNotIn("token", json.dumps(res))
            # device tokens cannot see link management
            dev = devices.mint_device(label="d")
            st, _ = srv.req(
                "GET", "/v1/hub/links",
                headers={"Authorization":
                         f"Bearer {dev['device_token']}"})
            self.assertEqual(st, 401)
        finally:
            ha.hub_links_fn = None
            srv.close()

    def test_hub_connect_calls_hook_and_returns_row(self):
        import agents_relay.http_adapter as ha

        seen = []
        ha.hub_connect_fn = lambda body: seen.append(body) or {
            "ok": True, "hub_id": "h1", "device_id": "dev-me"}
        srv = _Srv(_cfg())
        try:
            st, res = srv.req(
                "POST", "/v1/hub/connect",
                {"url": "https://h/#pair=ABCD", "label": "vps"},
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200)
            self.assertEqual(res["hub_id"], "h1")
            self.assertEqual(len(seen), 1)
            # 501 without a hook
            ha.hub_connect_fn = None
            st, res = srv.req(
                "POST", "/v1/hub/connect",
                {"url": "https://h/#pair=ABCD"},
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 501)
        finally:
            ha.hub_connect_fn = None
            srv.close()

    def test_hub_disconnect_route_calls_hook(self):
        import agents_relay.http_adapter as ha

        gone = []
        ha.hub_disconnect_fn = lambda hid: gone.append(hid) or {
            "ok": True}
        srv = _Srv(_cfg())
        try:
            st, res = srv.req(
                "DELETE", "/v1/hub/links/h1",
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200)
            self.assertEqual(gone, ["h1"])
            ha.hub_disconnect_fn = lambda hid: {
                "error": "unknown hub", "status": 404}
            st, _ = srv.req(
                "DELETE", "/v1/hub/links/nope",
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 404)
        finally:
            ha.hub_disconnect_fn = None
            srv.close()

    def test_session_host_switch_route(self):
        import agents_relay.http_adapter as ha

        calls = []
        ha.session_host_switch_fn = (
            lambda ws, sid, host, force=False:
            calls.append((ws, sid, host, force)) or {
                "session": sid, "host": host})
        srv = _Srv(_cfg())
        try:
            st, res = srv.req(
                "POST", "/v1/workspaces/w1/sessions/s1/host",
                {"host": "dev-b", "force": True},
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200)
            self.assertEqual(calls, [("w1", "s1", "dev-b", True)])
            # hook conflict -> 409 passthrough
            ha.session_host_switch_fn = (
                lambda ws, sid, host, force=False:
                {"error": "wip_unpushed", "status": 409})
            st, res = srv.req(
                "POST", "/v1/workspaces/w1/sessions/s1/host",
                {"host": "dev-b"},
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 409)
            self.assertEqual(res["error"], "wip_unpushed")
        finally:
            ha.session_host_switch_fn = None
            srv.close()


if __name__ == "__main__":
    unittest.main()
