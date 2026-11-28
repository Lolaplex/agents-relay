"""Regression tests for agents-relay#44 (R07) security contract."""
from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from agents_relay import channel, devices, pairing, web
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
import agents_relay.http_adapter as ha
import agents_relay.channel_client as cc
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
        self.host, self.port = self.server.server_address  # type: ignore[misc]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def req(self, method, path, body=None, headers=None):
        hdrs = dict(headers or {})
        raw = json.dumps(body).encode() if body is not None else None
        if raw is not None:
            hdrs["Content-Type"] = "application/json"
        conn = HTTPConnection(self.host, self.port, timeout=4)
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", errors="replace")
        hdr_list = resp.getheaders()
        status = resp.status
        conn.close()
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            parsed = data
        return status, hdr_list, parsed


class _EnvBase(unittest.TestCase):
    def setUp(self) -> None:
        pairing.clear_pairs()
        web.reset_login_limits()
        self.td = tempfile.TemporaryDirectory()
        host_dir = Path(self.td.name)
        (host_dir / "workspaces" / "default").mkdir(parents=True)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": str(host_dir)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(pairing.clear_pairs)
        self.addCleanup(web.reset_login_limits)


class _PairMixin:
    def _pair_remote(self, srv, label="d"):
        st, _, p = srv.req("POST", "/v1/pair/create", {"mode": "remote"},
                           headers={"Authorization": "Bearer expected"})
        assert st == 200, p
        st, _, r = srv.req("POST", "/v1/pair/redeem",
                           {"token": p["token"], "label": label})
        assert st == 200, r
        return r

    def _pair_ws_member(self, srv, ws="w1"):
        """Owner device mints a ws invite; second redeems as ws_member."""
        owner = self._pair_remote(srv, "owner")
        ha.ws_invite_auth = lambda did, w: did == owner["device_id"]
        ha.ws_grant_hook = lambda did, w, by: None
        st, _, inv = srv.req("POST", "/v1/ws/invite", {"ws": ws},
                             headers={"Authorization": f"Bearer {owner['token']}",
                                      "X-Device-Id": owner["device_id"]})
        assert st == 200, inv
        st, _, m = srv.req("POST", "/v1/pair/redeem",
                           {"token": inv["token"], "label": "guest"})
        assert st == 200, m
        return owner, m

    @staticmethod
    def _auth(dev):
        return {"Authorization": f"Bearer {dev['token']}",
                "X-Device-Id": dev["device_id"]}


class TestApprovalResForgery(unittest.TestCase):
    """C1: channel approval-res forgery — role/ws/status/id validation."""

    def setUp(self):
        channel.reset_hub()
        self.td = tempfile.TemporaryDirectory()
        host_dir = Path(self.td.name)
        (host_dir / "workspaces" / "default").mkdir(parents=True)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": str(host_dir)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(channel.reset_hub)

    def _mk_dev(self, role, allowlist=()):
        return devices.mint_device(label="t", host_id="h", role=role,
                                   allowlist=list(allowlist))

    def test_approval_res_dropped_for_wrong_role(self):
        hub = channel.hub()
        dev = self._mk_dev("host")  # role host — not client
        got = []
        hub.set_approval_handler(lambda did, k, p: got.append((did, k, dict(p))))
        hub.connect(dev["device_id"])
        out = hub.handle_upstream(dev["device_id"], [{
            "kind": "approval-res",
            "payload": {"id": "ap_1", "status": "approved", "ws": "w1"},
        }])
        self.assertEqual(out["malformed"], 1)
        self.assertEqual(got, [])

    def test_approval_res_dropped_bad_status(self):
        hub = channel.hub()
        dev = self._mk_dev("client")
        Host.from_env().create("w1", "w1", sync_policy="synced",
                               sync_hosts=[dev["device_id"]])
        got = []
        hub.set_approval_handler(lambda did, k, p: got.append((did, k, dict(p))))
        hub.connect(dev["device_id"])
        out = hub.handle_upstream(dev["device_id"], [{
            "kind": "approval-res",
            "payload": {"id": "ap_1", "status": "bogus", "ws": "w1"},
        }])
        self.assertEqual(out["malformed"], 1)
        self.assertEqual(got, [])

    def test_approval_res_dropped_unknown_ws(self):
        hub = channel.hub()
        dev = self._mk_dev("client")
        got = []
        hub.set_approval_handler(lambda did, k, p: got.append((did, k, dict(p))))
        hub.connect(dev["device_id"])
        out = hub.handle_upstream(dev["device_id"], [{
            "kind": "approval-res",
            "payload": {"id": "ap_1", "status": "approved", "ws": "nope"},
        }])
        self.assertEqual(out["malformed"], 1)
        self.assertEqual(got, [])

    def test_approval_res_passes_for_allowed_client(self):
        hub = channel.hub()
        dev = self._mk_dev("client")
        Host.from_env().create("w1", "w1", sync_policy="synced",
                               sync_hosts=[dev["device_id"]])
        got = []
        hub.set_approval_handler(lambda did, k, p: got.append((did, k, dict(p))))
        hub.connect(dev["device_id"])
        out = hub.handle_upstream(dev["device_id"], [{
            "kind": "approval-res",
            "payload": {"id": "ap_1", "status": "approved", "ws": "w1"},
        }])
        self.assertEqual(out["malformed"], 0)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], dev["device_id"])

    def test_approval_req_stamps_authenticated_device(self):
        hub = channel.hub()
        dev = self._mk_dev("host")
        got = []
        hub.set_approval_handler(lambda did, k, p: got.append((did, k, dict(p))))
        hub.connect(dev["device_id"])
        hub.handle_upstream(dev["device_id"], [{
            "kind": "approval-req",
            "payload": {"id": "ap_9", "__device": "spoofed-evil"},
        }])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][2]["__device"], dev["device_id"])
        self.assertNotEqual(got[0][2]["__device"], "spoofed-evil")

    def test_rpc_req_does_not_bump_seq(self):
        hub = channel.hub()
        link, _replay, _resync = hub.connect("dseq")
        hub.publish("dseq", "op", {"n": 1})
        seq_after_op = hub._channels["dseq"].seq
        hub.rpc_call("dseq", "ping", {}, timeout_sec=0.05)
        self.assertEqual(hub._channels["dseq"].seq, seq_after_op)


class TestWsMemberRouteBypass(_EnvBase, _PairMixin):
    """C2: ws_member bypasses on turn/stop/inject/stream surfaces."""

    def test_runs_stream_requires_allowlisted_ws(self):
        srv = _Srv(_cfg())
        try:
            _owner, m = self._pair_ws_member(srv)
            st, _, _ = srv.req("GET", "/v1/runs/stream",
                               headers=self._auth(m))
            self.assertEqual(st, 403)
            st, _, _ = srv.req("GET", "/v1/runs/stream?workspace=w2",
                               headers=self._auth(m))
            self.assertEqual(st, 403)
        finally:
            srv.close()

    def test_chat_stop_empty_body_denied_for_member(self):
        srv = _Srv(_cfg())
        try:
            _owner, m = self._pair_ws_member(srv)
            st, _, _ = srv.req("POST", "/v1/chat/stop", {},
                               headers=self._auth(m))
            self.assertEqual(st, 403)
            st, _, _ = srv.req("POST", "/v1/chat/stop",
                               {"workspace": "w2"},
                               headers=self._auth(m))
            self.assertEqual(st, 403)
        finally:
            srv.close()

    def test_inject_denied_for_member_outside_allowlist(self):
        srv = _Srv(_cfg())
        try:
            _owner, m = self._pair_ws_member(srv)
            st, _, _ = srv.req("POST", "/v1/inject",
                               {"chat_id": 1, "text": "hi", "workspace": "w2"},
                               headers=self._auth(m))
            self.assertEqual(st, 403)
            # empty workspace = unscoped inject -> denied for ws_member
            st, _, _ = srv.req("POST", "/v1/inject",
                               {"chat_id": 1, "text": "hi"},
                               headers=self._auth(m))
            self.assertEqual(st, 403)
        finally:
            srv.close()

    def test_turn_workdir_denied_for_member(self):
        srv = _Srv(_cfg())
        try:
            _owner, m = self._pair_ws_member(srv)
            for body in ({"text": "t", "workspace": "w1", "workdir": "/tmp/x"},
                         {"text": "t", "workspace": "w1",
                          "browser_profile": "/etc"},
                         {"text": "t", "workspace": "w1", "force": True}):
                st, _, _ = srv.req("POST", "/v1/turn", body,
                                   headers=self._auth(m))
                self.assertEqual(st, 403, body)
        finally:
            srv.close()

    def test_persona_hard_delete_denied_for_member(self):
        srv = _Srv(_cfg())
        try:
            _owner, m = self._pair_ws_member(srv)
            st, _, _ = srv.req(
                "DELETE", "/v1/workspaces/w1/personas/p1?hard=1",
                headers=self._auth(m))
            self.assertEqual(st, 403)
        finally:
            srv.close()

    def test_remote_proxy_member_needs_ws_sync_hosts(self):
        srv = _Srv(_cfg())
        try:
            _owner, m = self._pair_ws_member(srv)
            # register target remote bound to a channel device not in w1
            from agents_relay import remotes
            remotes.upsert_remote(srv.server.RequestHandlerClass.config,
                                  {"id": "devX", "via": "channel",
                                   "device_id": "targetDev"})
            st, _, _ = srv.req("POST", "/v1/remote/devX/v1/turn",
                               {"text": "t"},
                               headers=self._auth(m))
            self.assertEqual(st, 403)
        finally:
            srv.close()

    def test_remote_proxy_member_turn_reaches_channel_spoke(self):
        srv = _Srv(_cfg())
        try:
            _owner, m = self._pair_ws_member(srv)
            target = self._pair_remote(srv, "target")
            Host.from_env().create(
                "w1", "w1", sync_policy="synced",
                sync_hosts=[m["device_id"], target["device_id"]])
            from agents_relay import remotes
            remotes.upsert_remote(
                srv.server.RequestHandlerClass.config,
                {"id": "devX", "via": "channel",
                 "device_id": target["device_id"]})
            calls = []

            def fake_rpc(device_id, kind, payload, timeout_sec=0):
                calls.append((device_id, kind, payload, timeout_sec))
                return iter([{
                    "status": 200,
                    "content_type": "application/json",
                    "body": json.dumps({"ok": True, "spoke": True}),
                }])

            with patch.object(channel.hub(), "rpc_call", fake_rpc):
                st, _, res = srv.req(
                    "POST", "/v1/remote/devX/v1/turn",
                    {"text": "t", "workspace": "w1"},
                    headers=self._auth(m))
            self.assertEqual(st, 200, res)
            self.assertTrue(res["spoke"])
            self.assertEqual(len(calls), 1)
            device_id, kind, payload, _timeout = calls[0]
            self.assertEqual(device_id, target["device_id"])
            self.assertEqual(kind, "http")
            req = payload["req"]
            self.assertEqual(req["method"], "POST")
            self.assertEqual(req["path"], "/v1/turn")
            import base64

            forwarded = json.loads(base64.b64decode(
                req["body_b64"]).decode("utf-8"))
            self.assertEqual(forwarded["workspace"], "w1")
            self.assertEqual(forwarded["text"], "t")
        finally:
            srv.close()

    def test_remote_proxy_member_body_scope_fail_closed(self):
        srv = _Srv(_cfg())
        try:
            _owner, m = self._pair_ws_member(srv)
            target = self._pair_remote(srv, "target")
            Host.from_env().create(
                "w1", "w1", sync_policy="synced",
                sync_hosts=[m["device_id"], target["device_id"]])
            from agents_relay import remotes
            remotes.upsert_remote(
                srv.server.RequestHandlerClass.config,
                {"id": "devX", "via": "channel",
                 "device_id": target["device_id"]})
            calls = []

            def fake_rpc(device_id, kind, payload, timeout_sec=0):
                calls.append(payload)
                return iter([{"status": 200, "body": "{}"}])

            bodies = (
                {"text": "t"},
                {"text": "t", "workspace": "w2"},
                {"text": "t", "workspace": "w1", "force": True},
                {"text": "t", "workspace": "w1", "workdir": "/tmp/x"},
            )
            with patch.object(channel.hub(), "rpc_call", fake_rpc):
                for body in bodies:
                    st, _, _ = srv.req(
                        "POST", "/v1/remote/devX/v1/turn", body,
                        headers=self._auth(m))
                    self.assertEqual(st, 403, body)
            self.assertEqual(calls, [])
        finally:
            srv.close()


class TestTokenQueryBruteforce(_EnvBase):
    """C4: GET /?token= must count against the login throttle."""

    def test_token_query_rate_limited_after_max_fails(self):
        srv = _Srv(_cfg())
        try:
            for i in range(web._LOGIN_MAX_FAILS):
                st, _, _ = srv.req("GET", "/?token=wrong", headers={})
                # wrong token -> falls through to static/404, never 302
                self.assertNotEqual(st, 302)
            st, _, body = srv.req("GET", "/?token=wrong", headers={})
            self.assertEqual(st, 429)
            # even the CORRECT token is now throttled for this ip
            st, _, body = srv.req("GET", "/?token=tok", headers={})
            self.assertEqual(st, 429)
        finally:
            srv.close()

    def test_login_fails_pruned(self):
        web.reset_login_limits()
        for i in range(3):
            web._login_failed(f"10.0.0.{i}")
        with web._login_lock:
            self.assertLessEqual(len(web._login_fails), 3)
        # force stale entries out
        with web._login_lock:
            for ip in web._login_fails:
                web._login_fails[ip] = [0.0]
        web._login_allowed("9.9.9.9")
        with web._login_lock:
            self.assertEqual(list(web._login_fails.keys()), ["9.9.9.9"]
                             if "9.9.9.9" in web._login_fails else [])


class TestDevicesRegistry(_EnvBase):
    """C6: mtime-cache must not resurrect deleted rows."""

    def test_deleted_registry_does_not_resurrect(self):
        dev = devices.mint_device(label="x", host_id="h")
        did = dev["device_id"]
        self.assertIn(did, devices.load_devices())
        os.remove(devices._path())
        devices._cache["mtime_ns"] = -1  # simulate post-write state
        rows = devices.load_devices()
        self.assertNotIn(did, rows)

    def test_env_path_switch_invalidates(self):
        dev = devices.mint_device(label="x", host_id="h")
        rows_a = devices.load_devices()
        with tempfile.TemporaryDirectory() as td2:
            with patch.dict(os.environ, {"AGENTS_HOST_DIR": td2}):
                rows_b = devices.load_devices()
        self.assertNotIn(dev["device_id"], rows_b)
        self.assertIn(dev["device_id"], rows_a)

    def test_allowlist_deepcopy(self):
        dev = devices.mint_device(label="x", host_id="h",
                                  role="ws_member", allowlist=["w1"])
        r1 = devices.load_devices()[dev["device_id"]]
        r1["allowlist"].append("EVIL")
        r2 = devices.load_devices()[dev["device_id"]]
        self.assertNotIn("EVIL", r2["allowlist"])

    def test_add_allowlist_no_wildcard_narrow(self):
        dev = devices.mint_device(label="x", host_id="h", role="host")
        self.assertTrue(devices.add_allowlist(dev["device_id"], "w9"))
        self.assertEqual(
            devices.load_devices()[dev["device_id"]]["allowlist"], [])
        # ws_member still accumulates
        m = devices.mint_device(label="y", host_id="h", role="ws_member",
                                allowlist=["w1"])
        devices.add_allowlist(m["device_id"], "w2")
        self.assertIn("w2", devices.load_devices()
                      [m["device_id"]]["allowlist"])


class TestWsInviteCookieAuth(_EnvBase):
    """W02: /v1/ws/invite accepts operator cookie auth."""

    def test_cookie_auth_can_mint_invite(self):
        srv = _Srv(_cfg())
        try:
            cookie_val = web.issue_cookie(srv.server.RequestHandlerClass.config)
            ha.ws_invite_auth = lambda did, ws: did == "__admin__"
            ha.ws_grant_hook = lambda d, w, b: None
            st, _, res = srv.req(
                "POST", "/v1/ws/invite", {"ws": "w1"},
                headers={"Cookie": f"relay_web={cookie_val}"})
            self.assertEqual(st, 200)
            self.assertEqual(res["ws"], "w1")
        finally:
            srv.close()


class TestConnectHubReuse(_EnvBase):
    """connect_hub: reuse sends Bearer, token=='' keeps stored device_id."""

    def test_reuse_grant_keeps_device_id(self):
        captured = {}

        def fake_redeem(hub_url, cred, *, label="", bearer="", client_host_id=""):
            captured["bearer"] = bearer
            captured["label"] = label
            return {"ok": True, "mode": "remote", "host_id": "h1",
                    "device_id": "d123", "token": ""}

        cc._store_hubs({"h1": {"hub_url": "http://hub:1", "device_id": "d123",
                               "last_seq": 7, "label": "h1",
                               "connected_at": 1}})
        with patch.object(cc, "redeem_device", fake_redeem), \
             patch.object(cc, "hub_token", lambda hid: "OLDTOK"):
            res = cc.connect_hub("http://hub:1", "PAIRCODE", label="lab")
        self.assertEqual(captured["bearer"], "OLDTOK")
        self.assertEqual(captured["label"], "lab")
        self.assertTrue(res["reused"])
        self.assertEqual(res["device_id"], "d123")
        self.assertEqual(res["last_seq"], 7)


class TestRpcHttpStream(_EnvBase):
    """_rpc_http must use read1 + close, and deny the new pivot list."""

    def test_denylist_covers_new_paths(self):
        for p in ("/v1/inject", "/v1/jobs", "/v1/favorites", "/v1/push",
                  "/v1/a2a", "/v1/webhook/x", "/v1/alert",
                  "/v1/klanker/skills", "/v1/klanker/memory",
                  "/v1/ws/invite", "/v1/pair/create"):
            self.assertTrue(cc._rpc_denied("POST", p), p)
        # provider probe denied even on GET
        self.assertTrue(cc._rpc_denied("GET", "/v1/providers/acp.x/probe"))
        # writes to settings/workspaces denied
        self.assertTrue(cc._rpc_denied("POST", "/v1/settings"))
        self.assertTrue(cc._rpc_denied("POST", "/v1/workspaces"))
        # reads still pass on non-denied surfaces
        self.assertFalse(cc._rpc_denied("GET", "/v1/workspaces"))
        self.assertFalse(cc._rpc_denied("GET", "/v1/providers"))

    def test_stream_path_uses_read1_and_closes(self):
        chunks = [b'data: {"a":1}\n\n', b'data: {"b":2}\n\n', b""]
        closed = {"v": False}
        read1_calls = {"n": 0}

        class FakeResp:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def read1(self, n=8192):
                read1_calls["n"] += 1
                return chunks.pop(0)

            def read(self, n=-1):
                raise AssertionError("must use read1 not read")

            def close(self):
                closed["v"] = True

        def fake_urlopen(req, timeout=0):
            return FakeResp()

        with patch.object(cc.urllib.request, "urlopen", fake_urlopen):
            out = list(cc._rpc_http(
                {"req": {"method": "GET", "path": "/v1/stream"}},
                _cfg()))
        self.assertGreaterEqual(read1_calls["n"], 2)
        self.assertTrue(closed["v"])
        self.assertIn({"done": True}, out)


class TestLeaseRelease(_EnvBase):
    """W10: lease released on early-400 after acquire; turn fails loud."""

    def test_chat_stream_400_releases_lease(self):
        released = []
        ha.turn_lease_hook = lambda h, b, c: None  # acquired ok
        ha.turn_lease_release_hook = lambda b, c: released.append(dict(b))
        srv = _Srv(_cfg())
        try:
            # invalid body: no text, no attachments -> 400 inside handler
            st, _, _ = srv.req("POST", "/v1/chat/stream", {"text": ""},
                               headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 400)
            self.assertEqual(len(released), 1)
        finally:
            ha.turn_lease_hook = None
            ha.turn_lease_release_hook = None
            srv.close()

    def test_turn_fails_loud_on_dispatch_error(self):
        def boom(h, p, b):
            raise RuntimeError("spoke gone")
        ha.remote_turn_dispatch = boom
        srv = _Srv(_cfg())
        try:
            st, _, body = srv.req(
                "POST", "/v1/turn", {"text": "hi"},
                headers={"Authorization": "Bearer expected"})
            self.assertEqual(st, 500)
        finally:
            ha.remote_turn_dispatch = None
            srv.close()


class TestApprovalFailClosed(_EnvBase):
    def test_run_local_approve_rejects_mismatched_request(self):
        from agents_relay import approvals
        cfg = _cfg()
        req = {"id": "ap_test1", "tool": "shell", "args": {"cmd": "ls"}}
        rec = approvals.create_pending(cfg, 0, dict(req))
        approvals.decide_approval(cfg, "ap_test1", allow=True)
        # replay with DIFFERENT args must not inherit the grant
        evil = {"id": "ap_test1", "tool": "shell",
                "args": {"cmd": "rm -rf /"}}
        code, note = approvals.run_local_approve(cfg, evil, 0.1)
        self.assertEqual(code, 2)
        self.assertIn("changed", note)


class TestPairUrlShapes(_EnvBase):
    def test_url_kinds(self):
        st = pairing.PairingStore()
        web_p = st.create_pair(mode="web", base_url="http://h:1")
        rem_p = st.create_pair(mode="remote", base_url="http://h:1")
        ws_p = st.create_pair(mode="ws", ws="w1", base_url="http://h:1")
        self.assertIn("/#pair=", web_p["pair_url"])
        self.assertNotIn("kind=", web_p["pair_url"])
        self.assertIn("/#kind=remote&pair=", rem_p["pair_url"])
        self.assertIn("/#kind=ws&ws=w1&pair=", ws_p["pair_url"])


class TestStoreAppendActor(_EnvBase):
    def test_device_caller_stamps_device_actor(self):
        mods = __import__("agents_relay.store_api", fromlist=["x"])
        captured = {}

        class FakeSurface:
            @staticmethod
            def default_registry():
                return object()

            @staticmethod
            def append(domain, payload, actor, conn=None, reg=None):
                captured["actor"] = actor
                return "e1"

        class FakeDB:
            @staticmethod
            def connect(path):
                class C:
                    def execute(self, s):
                        pass

                    def close(self):
                        pass
                return C()

        with patch.object(mods, "_load_store_modules",
                          return_value=(FakeDB, FakeSurface, type("WR", (), {}))):
            ws = Host.from_env().workspace("default")
            code, res = mods.handle_store_append(
                ws, {"domain": "records", "payload": {"x": 1}},
                device_id="dev-abc")
        self.assertEqual(res["actor"], "device:dev-abc")
        self.assertEqual(captured["actor"], "device:dev-abc")


if __name__ == "__main__":
    unittest.main()
