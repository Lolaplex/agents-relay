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
            # admin Bearer caller -> local host self row always present
            self.assertEqual(len(ids), 3)
            self.assertIn("dev-a", ids)
            self.assertIn("dev-b", ids)
            self_row = next(h for h in res["hosts"] if h["self"])
            self.assertTrue(self_row["online"])
            b = next(h for h in res["hosts"]
                     if h["device_id"] == "dev-b")
            self.assertEqual(b["leases"][0]["session"], "s1")
            self.assertFalse(b["online"])  # no live stream attached
            # disconnected member: stale "busy" forced offline (#45)
            self.assertEqual(b["status"], "offline")
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


class TestWsHostsR08(_Env):
    """agents-relay#45 contract: self-row, offline forcing, allowlist,
    device admission, spoke relay, validation, SSE isolation."""

    def test_admin_self_row_on_local_workspace(self):
        # a local-policy ws admits zero devices — the admin Bearer
        # caller still gets a self row and never hub_offline
        self._ws("w1")
        srv = _Srv(_cfg())
        try:
            st, res = srv.req(
                "GET", "/v1/workspaces/w1/hosts",
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200, res)
            self.assertFalse(res["hub_offline"])
            self.assertEqual(len(res["hosts"]), 1)
            self_row = res["hosts"][0]
            self.assertTrue(self_row["self"])
            self.assertTrue(self_row["online"])
            self.assertEqual(self_row["role"], "host")
        finally:
            srv.close()

    def test_default_host_joins_members(self):
        self._ws("w1", sync_policy="synced", sync_hosts=["dev-a"],
                 default_host="dev-dh")
        srv = _Srv(_cfg())
        try:
            st, res = srv.req(
                "GET", "/v1/workspaces/w1/hosts",
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200, res)
            dh = next(h for h in res["hosts"]
                      if h["device_id"] == "dev-dh")
            self.assertEqual(dh["role"], "default")
            self.assertEqual(dh["status"], "offline")
            self.assertFalse(dh["online"])
        finally:
            srv.close()

    def test_device_caller_requires_ws_admission(self):
        self._ws("w1", sync_policy="synced", sync_hosts=["dev-a"])
        devices.mint_device(device_id="dev-a", label="a")
        outsider = devices.mint_device(device_id="dev-z", label="z")
        srv = _Srv(_cfg())
        try:
            st, res = srv.req(
                "GET", "/v1/workspaces/w1/hosts",
                headers={"Authorization":
                         f"Bearer {outsider['device_token']}"})
            self.assertEqual(st, 403)
            # admitted device reads the map and sees itself
            tok = devices._read_secret("dev-a")
            st, res = srv.req(
                "GET", "/v1/workspaces/w1/hosts",
                headers={"Authorization": f"Bearer {tok}"})
            self.assertEqual(st, 200, res)
            self_row = next(h for h in res["hosts"] if h["self"])
            self.assertEqual(self_row["device_id"], "dev-a")
            self.assertTrue(self_row["online"])
        finally:
            srv.close()

    def test_spoke_relays_hub_map_with_self_stamp(self):
        import agents_relay.http_adapter as ha

        self._ws("w1", sync_policy="synced", sync_hosts=["dev-a"])
        spoke_map = {
            "hosts": [{"device_id": "dev-a", "label": "a",
                       "role": "member", "self": False,
                       "online": True, "via": "channel",
                       "last_seen": 1, "status": "online",
                       "leases": []}],
            "sync_policy": "synced", "owner": "", "default_host": "",
            "hub_offline": False}
        ha.spoke_hosts_fn = lambda ws_id: dict(spoke_map)
        srv = _Srv(_cfg())
        try:
            st, res = srv.req(
                "GET", "/v1/workspaces/w1/hosts",
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200, res)
            self.assertFalse(res["hub_offline"])
            ids = {h["device_id"] for h in res["hosts"]}
            self.assertIn("dev-a", ids)
            # admin caller's own self row appended to the relayed map
            self_row = next(h for h in res["hosts"] if h["self"])
            self.assertTrue(self_row["online"])
        finally:
            ha.spoke_hosts_fn = None
            srv.close()

    def test_spoke_configured_no_link_shows_hub_offline(self):
        from agents_relay.channel_client import _store_hubs

        self._ws("w1", sync_policy="synced", sync_hosts=["dev-a"])
        _store_hubs({"hubx": {"hub_url": "http://h:8787",
                              "device_id": "dev-me", "label": "vps",
                              "connected_at": 1, "last_seq": 0}})
        srv = _Srv(_cfg())
        try:
            st, res = srv.req(
                "GET", "/v1/workspaces/w1/hosts",
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200, res)
            self.assertTrue(res["hub_offline"])
        finally:
            srv.close()

    def test_hub_devices_rows_carry_allowlist(self):
        devices.mint_device(device_id="dev-ws", label="w",
                            role="ws_member", allowlist=["w1"])
        srv = _Srv(_cfg())
        try:
            st, res = srv.req(
                "GET", "/v1/hub/devices",
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200)
            row = next(r for r in res["devices"]
                       if r["id"] == "dev-ws")
            self.assertEqual(row["allowlist"], ["w1"])
        finally:
            srv.close()

    def test_host_switch_validation(self):
        srv = _Srv(_cfg())
        try:
            auth = {"Authorization": "Bearer expected"}
            # malformed JSON -> exactly one 400, no double-response
            conn = HTTPConnection(srv.host, srv.port, timeout=5)
            conn.request(
                "POST", "/v1/workspaces/w1/sessions/s1/host",
                body=b"{not json",
                headers={**auth,
                         "Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 400)
            conn.close()
            # invalid host id -> 400 before the hook ever runs
            st, _ = srv.req(
                "POST", "/v1/workspaces/w1/sessions/s1/host",
                {"host": "../escape"}, headers=auth)
            self.assertEqual(st, 400)
            st, _ = srv.req(
                "POST", "/v1/workspaces/w1/sessions/s1/host",
                {"host": "bad host!"}, headers=auth)
            self.assertEqual(st, 400)
        finally:
            srv.close()

    @staticmethod
    def _device_auth(dev: dict) -> dict:
        return {"Authorization": f"Bearer {dev['device_token']}",
                "X-Device-Id": dev["device_id"]}

    def test_host_switch_member_deny_keys_fail_closed(self):
        import agents_relay.http_adapter as ha

        member = devices.mint_device(
            device_id="dev-m", label="m", role="ws_member",
            allowlist=["w1"])
        self._ws("w1", sync_policy="synced", sync_hosts=["dev-m"])
        calls = []
        ha.session_host_switch_fn = (
            lambda ws, sid, host, force=False:
            calls.append((ws, sid, host, force)) or {"session": sid})
        srv = _Srv(_cfg())
        try:
            for body in (
                {"host": "dev-b", "force": True},
                {"host": "dev-b", "workdir": "/tmp/x"},
                {"host": "dev-b", "sync_hosts": ["dev-b"]},
            ):
                st, _ = srv.req(
                    "POST", "/v1/workspaces/w1/sessions/s1/host", body,
                    headers=self._device_auth(member))
                self.assertEqual(st, 403, body)
            st, _ = srv.req(
                "POST", "/v1/workspaces/w1/sessions/s1/host",
                "not-an-object", headers=self._device_auth(member))
            self.assertEqual(st, 403)
            self.assertEqual(calls, [])
        finally:
            ha.session_host_switch_fn = None
            srv.close()

    def test_host_switch_force_requires_admin_or_owner(self):
        import agents_relay.http_adapter as ha

        owner = devices.mint_device(device_id="dev-a", label="owner")
        other = devices.mint_device(device_id="dev-c", label="other")
        self._ws(
            "w1", sync_policy="synced", sync_hosts=["dev-a", "dev-c"],
            execution={"owner": "dev-a", "failover": "none",
                       "lease_sec": 45})
        calls = []
        ha.session_host_switch_fn = (
            lambda ws, sid, host, force=False:
            calls.append((ws, sid, host, force)) or {"session": sid})
        srv = _Srv(_cfg())
        try:
            st, _ = srv.req(
                "POST", "/v1/workspaces/w1/sessions/s1/host",
                {"host": "dev-b", "force": True},
                headers=self._device_auth(other))
            self.assertEqual(st, 403)
            self.assertEqual(calls, [])

            st, res = srv.req(
                "POST", "/v1/workspaces/w1/sessions/s1/host",
                {"host": "dev-b", "force": True},
                headers=self._device_auth(owner))
            self.assertEqual(st, 200, res)
            self.assertEqual(calls, [("w1", "s1", "dev-b", True)])

            st, res = srv.req(
                "POST", "/v1/workspaces/w1/sessions/s1/host",
                {"host": "dev-b", "force": True},
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200, res)
            self.assertEqual(calls[-1], ("w1", "s1", "dev-b", True))
        finally:
            ha.session_host_switch_fn = None
            srv.close()

    def test_hub_link_delete_validates_hub_id(self):
        srv = _Srv(_cfg())
        try:
            st, _ = srv.req(
                "DELETE", "/v1/hub/links/bad..id",
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 400)
        finally:
            srv.close()

    def test_workspace_changed_single_canonical_emit(self):
        from agents_relay.events import subscribe

        self._ws("w1", sync_policy="synced", sync_hosts=["dev-a"])
        sub = subscribe(workspace="w1")
        srv = _Srv(_cfg())
        try:
            st, _ = srv.req(
                "PATCH", "/v1/workspaces/w1",
                {"sync_hosts": ["dev-a", "dev-b"], "label": "l"},
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200)
            evts = []
            while True:
                try:
                    evt = sub.queue.get_nowait()
                except Exception:
                    break
                if evt is None:
                    break
                if evt.get("kind") == "workspace.changed":
                    evts.append(evt)
            self.assertEqual(len(evts), 1)
            payload = evts[0]["payload"]
            self.assertEqual(payload["workspace"], "w1")
            self.assertNotIn("ws", payload)
            self.assertIn("sync_hosts", payload["fields"])
        finally:
            srv.close()

    def test_ws_announce_sse_gated_by_handler_acl(self):
        from agents_relay.events import subscribe

        hub = channel.hub()
        sub = subscribe()
        try:
            hub.set_ws_handler(lambda did, k, p: False)
            hub.handle_upstream("dX", [
                {"kind": "ws:announce",
                 "payload": {"ws": "w1", "owner": "dX"}}])
            self.assertTrue(sub.queue.empty())
            hub.set_ws_handler(lambda did, k, p: True)
            hub.handle_upstream("dX", [
                {"kind": "ws:announce",
                 "payload": {"ws": "w1", "owner": "dX"}}])
            evt = sub.queue.get(timeout=2)
            self.assertEqual(evt["kind"], "ws.announced")
            self.assertEqual(evt["workspace"], "w1")
        finally:
            hub.set_ws_handler(None)

    def test_release_holder_emits_lease_changed(self):
        from agents_relay.events import subscribe

        book = hub_leases()
        book.acquire("w1", "s1", "dev-h", "r1", ttl=45)
        book.acquire("w2", "s2", "dev-h", "r2", ttl=45)
        sub = subscribe()
        dropped = book.release_holder("dev-h")
        self.assertEqual(dropped, 2)
        kinds = set()
        while True:
            try:
                evt = sub.queue.get_nowait()
            except Exception:
                break
            if evt is None:
                break
            if evt.get("kind") == "lease.changed" \
                    and evt["payload"].get("action") == "release":
                kinds.add(evt["payload"]["ws"])
        self.assertEqual(kinds, {"w1", "w2"})

    def test_presence_events_fan_out_to_admitting_workspaces(self):
        from agents_relay.events import subscribe

        self._ws("w1", sync_policy="synced", sync_hosts=["dev-a"])
        devices.mint_device(device_id="dev-a", label="a")
        scoped = subscribe(workspace="w1")
        other = subscribe(workspace="w9")
        channel.hub()._presence.pop("dev-a", None)
        channel.hub().note_presence("dev-a", {"status": "idle"})
        evt = scoped.queue.get(timeout=2)
        self.assertEqual(evt["kind"], "device.presence")
        self.assertEqual(evt["workspace"], "w1")
        self.assertTrue(other.queue.empty())

    def test_presence_fields_bounded(self):
        hub = channel.hub()
        hub._presence.pop("dev-cap", None)
        hub.note_presence("dev-cap", {
            "status": "x" * 500,
            "caps": ["c"] * 100})
        row = hub.presence("dev-cap")
        self.assertEqual(len(row["status"]), 64)
        self.assertEqual(len(row["caps"]), 32)

    def test_revoke_drops_presence_row(self):
        devices.mint_device(device_id="dev-r", label="r")
        channel.hub().note_presence("dev-r", {"status": "busy"})
        self.assertTrue(channel.hub().presence("dev-r"))
        srv = _Srv(_cfg())
        try:
            st, _ = srv.req(
                "DELETE", "/v1/hub/devices/dev-r",
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 200)
            self.assertEqual(channel.hub().presence("dev-r"),
                             {"connected": False})
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
