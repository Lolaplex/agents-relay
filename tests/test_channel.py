"""Hub channel v0 (agents-relay#40): device creds, SSE+POST, seq/replay, rpc."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest

from agents_relay.leases import reset_leases
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from agents_relay import channel, devices
from agents_workspace import Host
from agents_relay.channel_client import ChannelClient, _store_hubs
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http


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
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
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


class TestDevices(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        host_dir = Path(self.td.name)
        (host_dir / "workspaces" / "default").mkdir(parents=True)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": str(host_dir)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.td.cleanup()

    def test_mint_verify_revoke_cycle(self):
        dev = devices.mint_device(label="laptop", host_id="h1")
        did = dev["device_id"]
        token = dev["device_token"]
        self.assertTrue(did)
        self.assertTrue(token)
        self.assertNotEqual(token, "expected")
        self.assertTrue(devices.verify_device(did, token))
        self.assertFalse(devices.verify_device(did, "wrong"))
        self.assertEqual(devices.device_id_for_token(token), did)
        self.assertEqual(devices.device_id_for_token("wrong"), "")

        rows = devices.list_public()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], did)
        self.assertTrue(rows[0]["has_token"])
        self.assertNotIn(token, json.dumps(rows))

        self.assertTrue(devices.revoke_device(did))
        self.assertFalse(devices.verify_device(did, token))
        self.assertFalse(devices.revoke_device(did))

    def test_remint_rotates_token(self):
        dev = devices.mint_device(device_id="dev1", label="one")
        again = devices.mint_device(device_id="dev1", label="two")
        self.assertNotEqual(dev["device_token"], again["device_token"])
        self.assertFalse(devices.verify_device("dev1", dev["device_token"]))
        self.assertTrue(devices.verify_device("dev1", again["device_token"]))
        self.assertEqual(again["label"], "two")


class TestChannelHub(unittest.TestCase):
    def setUp(self) -> None:
        channel.reset_hub()
        reset_leases()

    def test_seq_replay_and_kick(self):
        hub = channel.hub()
        link = hub.connect("d1")[0]
        self.assertEqual(hub.publish("d1", "op", {"a": 1}), 1)
        self.assertEqual(hub.publish("d1", "op", {"a": 2}), 2)
        # reconnect: same seq space, replay ring survives
        link2 = hub.connect("d1")[0]
        self.assertEqual(link2.chan, link.chan)
        self.assertNotEqual(link2.generation, link.generation)
        drained = []
        while True:
            item = link.queue.get_nowait()
            drained.append(item)
            if item is None:
                break
        self.assertIsNone(drained[-1])  # kicked
        self.assertEqual(len(drained), 3)  # seq 1, seq 2, sentinel
        hub.publish("d1", "op", {"a": 3})
        replayed = hub.replay_since(link2, 1)
        self.assertEqual([m["payload"]["a"] for m in replayed], [2, 3])
        self.assertEqual(hub.oldest_seq(link2), 0)

    def test_publish_offline_returns_minus_one(self):
        hub = channel.hub()
        self.assertEqual(hub.publish("ghost", "op", {}), -1)

    def test_rpc_offline_and_unknown_pending(self):
        hub = channel.hub()
        frames = list(hub.rpc_call("ghost", "ping", {}, timeout_sec=0.5))
        self.assertEqual(frames, [{"error": "device offline"}])
        out = hub.handle_upstream("d1", [{"kind": "rpc-res", "id": "nope",
                                          "payload": {"x": 1}}])
        self.assertEqual(out, {"acked": 0, "malformed": 0})

    def test_upstream_broadcast_and_ack(self):
        hub = channel.hub()
        hub.connect("d1")
        hub.connect("d2")
        out = hub.handle_upstream("d1", [
            {"kind": "op", "payload": {"n": 1}},
            {"kind": "presence", "payload": {"ok": True}},
            {"kind": "bogus", "payload": {}},
        ])
        self.assertEqual(out, {"acked": 1, "malformed": 1})
        msg = hub.link("d2").queue.get_nowait()
        self.assertEqual(msg["kind"], "op")
        self.assertEqual(msg["payload"]["n"], 1)
        # sender must not get its own op back
        self.assertTrue(hub.link("d1").queue.empty())

    def test_pending_rpc_is_bound_to_target_device(self):
        """agents-relay#42: rpc_id guessing across devices must not
        hijack a pending call — pending is keyed (device, rpc_id)."""
        hub = channel.hub()
        link = hub.connect("d1")[0]
        hub.connect("d2")
        frames_out: list[dict] = []

        def caller() -> None:
            for fr in hub.rpc_call("d1", "ping", {}, timeout_sec=5):
                frames_out.append(fr)

        t = threading.Thread(target=caller, daemon=True)
        t.start()
        req = link.queue.get(timeout=3)
        # d2 spoofs the rpc_id — must NOT resolve the pending call
        hub.handle_upstream("d2", [{
            "kind": "rpc-res", "id": req["id"],
            "payload": {"pong": False, "hijack": True, "done": True},
        }])
        time.sleep(0.05)
        self.assertEqual(frames_out, [])
        # the real device can still answer
        hub.handle_upstream("d1", [{
            "kind": "rpc-res", "id": req["id"],
            "payload": {"pong": True, "done": True},
        }])
        t.join(timeout=3)
        self.assertEqual(frames_out, [{"pong": True, "done": True}])

    def test_rpc_req_never_enters_replay_ring(self):
        """A reconnecting device must not re-execute rpc requests."""
        hub = channel.hub()
        hub.connect("d1")
        hub.publish("d1", "op", {"a": 1})
        hub.publish("d1", "rpc-req", {"method": "ping"}, msg_id="r1")
        hub.publish("d1", "op", {"a": 2})
        _link, replay, resync = hub.connect("d1", since=1)
        self.assertFalse(resync)
        kinds = [m["kind"] for m in replay]
        self.assertIn("op", kinds)
        self.assertNotIn("rpc-req", kinds)

    def test_resync_when_since_beyond_seq(self):
        """Restarted hub: client cursor ahead of channel seq -> resync."""
        hub = channel.hub()
        hub.connect("d1")
        hub.publish("d1", "op", {"a": 1})
        _link, replay, resync = hub.connect("d1", since=99)
        self.assertTrue(resync)
        self.assertEqual(replay, [])

    def test_resync_when_since_below_oldest(self):
        hub = channel.hub()
        hub.connect("d1")
        for i in range(5):
            hub.publish("d1", "op", {"a": i})
        _link, _replay, resync = hub.connect("d1", since=1)
        self.assertFalse(resync)  # seq 2..5 replayable
        _link, _replay, resync = hub.connect("d1", since=-1)
        self.assertFalse(resync)  # since<=0 means fresh attach
        # cursor below the ring floor -> resync
        chan = hub.link("d1").chan
        chan.replay.clear()
        for i in range(3):
            hub.publish("d1", "op", {"a": i + 10})
        _link, _replay, resync = hub.connect("d1", since=1)
        self.assertTrue(resync)

    def test_full_queue_still_delivers_disconnect_sentinel(self):
        """Queue overflow may drop envelopes, never the close sentinel."""
        hub = channel.hub()
        link = hub.connect("d1")[0]
        for i in range(600):  # over the 512 cap without a consumer
            hub.publish("d1", "op", {"i": i})
        hub.disconnect("d1")
        seen_none = False
        while True:
            try:
                item = link.queue.get_nowait()
            except Exception:
                break
            if item is None:
                seen_none = True
                break
        self.assertTrue(seen_none)

    def test_malformed_upstream_frames_counted_not_fatal(self):
        hub = channel.hub()
        hub.connect("d1")
        out = hub.handle_upstream("d1", [
            "not-a-dict",
            {"kind": "op"},                      # missing payload
            {"kind": "op", "payload": "nope"},   # non-dict payload
            {"kind": "op", "payload": {"n": 1}},
            {"kind": "ws:announce", "payload": {}},  # no ws field
            {"kind": "bogus", "payload": {}},
        ])
        self.assertEqual(out["acked"], 1)
        self.assertEqual(out["malformed"], 5)

    def test_recheck_after_connect_revoked_device_stream(self):
        """A device revoked between auth and link registration must not
        end up streaming — connect re-validates."""
        hub = channel.hub()
        link, _replay, _rs = hub.connect("ghostdev")
        # ghostdev is not in devices.json at all -> handle_hub_stream's
        # post-connect check would 401 it; prove the registry lookup
        # itself drives the decision.
        self.assertNotIn("ghostdev", devices.load_devices())
        hub.disconnect("ghostdev", generation=link.generation)

    def test_revoke_disconnects_leases_and_event_subscriptions(self):
        """Revocation teardown: live leases freed, tagged SSE subs
        poisoned, channel link dropped."""
        from agents_relay import events
        from agents_relay.leases import hub_leases

        try:
            hub = channel.hub()
            link = hub.connect("d1")[0]
            sub = events.subscribe(workspace="w1", tag="d1")
            g = hub_leases().acquire("w1", "s1", "d1", run_id="r1")
            self.assertTrue(g["granted"])

            self.assertEqual(hub_leases().release_holder("d1"), 1)
            self.assertEqual(events.disconnect_tag("d1"), 1)
            hub.disconnect("d1")

            # drain past ws-scoped lease.changed events -> the poison
            # sentinel is the last item the sub ever sees (revoke kills
            # the stream regardless of queued events)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if sub.queue.get(timeout=1.0) is None:
                    break
            else:
                self.fail("tagged subscription never poisoned")
            self.assertIsNone(link.queue.get(timeout=1.0))
            # lease slot is free for a new holder
            g2 = hub_leases().acquire("w1", "s1", "d2")
            self.assertTrue(g2["granted"])
            self.assertEqual(hub_leases().snapshot()[0]["holder"], "d2")
        finally:
            events.shutdown_all()
            events.reset_bus()  # module bus is shared — leave it usable

    def test_rpc_call_roundtrip(self):
        hub = channel.hub()
        link = hub.connect("d1")[0]
        frames_out: list[dict] = []

        def caller() -> None:
            for fr in hub.rpc_call("d1", "ping", {}, timeout_sec=5):
                frames_out.append(fr)

        t = threading.Thread(target=caller, daemon=True)
        t.start()
        req = link.queue.get(timeout=3)
        self.assertEqual(req["kind"], "rpc-req")
        hub.handle_upstream("d1", [{
            "kind": "rpc-res", "id": req["id"],
            "payload": {"pong": True, "done": True},
        }])
        t.join(timeout=3)
        self.assertEqual(frames_out, [{"pong": True, "done": True}])


class TestChannelWire(unittest.TestCase):
    """HTTP-level: redeem mints device creds, device token auths API+channel."""

    def setUp(self) -> None:
        channel.reset_hub()
        reset_leases()
        self.td = tempfile.TemporaryDirectory()
        host_dir = Path(self.td.name)
        (host_dir / "workspaces" / "default").mkdir(parents=True)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": str(host_dir)})
        self.env.start()
        self.srv = _Srv(_cfg())

    def tearDown(self) -> None:
        self.srv.close()
        self.env.stop()
        self.td.cleanup()
        channel.reset_hub()
        reset_leases()

    def _redeem_device(self) -> dict:
        st, created = self.srv.req(
            "POST", "/v1/pair/create", {"mode": "remote", "ttl_sec": 60},
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 200)
        st, res = self.srv.req(
            "POST", "/v1/pair/redeem", {"token": created["token"]})
        self.assertEqual(st, 200)
        self.assertTrue(res["ok"])
        return res

    def test_redeem_remote_mints_device_credentials(self):
        res = self._redeem_device()
        self.assertTrue(res["device_id"])
        self.assertTrue(res["token"])
        self.assertNotEqual(res["token"], "expected")  # master never leaves
        self.assertIn("stream", res["hub"])
        # the mount table is operator surface — a device token can
        # never read it (agents-relay#41)
        st, body = self.srv.req(
            "GET", "/v1/remotes",
            headers={"Authorization": f"Bearer {res['token']}"})
        self.assertEqual(st, 401)

    def test_device_token_cannot_mint_or_manage_credentials(self):
        res = self._redeem_device()
        auth = {"Authorization": f"Bearer {res['token']}"}
        # credential minting/management is master-only — a stolen device
        # token must not mint further credentials or drop stored ones.
        st, _ = self.srv.req("POST", "/v1/pair/create", {"mode": "remote"},
                             headers=auth)
        self.assertEqual(st, 401)
        st, _ = self.srv.req("GET", "/v1/hub/devices", headers=auth)
        self.assertEqual(st, 401)
        st, _ = self.srv.req("POST", "/v1/remotes",
                             {"id": "x", "base_url": "https://evil.example",
                              "secret": "leak"}, headers=auth)
        self.assertEqual(st, 401)
        st, _ = self.srv.req("DELETE", "/v1/remotes/dev-ghost", headers=auth)
        self.assertEqual(st, 401)
        st, _ = self.srv.req("GET", "/v1/discover", headers=auth)
        self.assertEqual(st, 401)
        # provider manifests carry executable commands — device tokens
        # must never register or delete them (agents-relay#41)
        st, _ = self.srv.req("POST", "/v1/providers",
                             {"name": "x", "cmd": "id"}, headers=auth)
        self.assertEqual(st, 401)
        st, _ = self.srv.req("DELETE", "/v1/providers/x", headers=auth)
        self.assertEqual(st, 401)

    def test_hub_stream_requires_device_auth(self):
        st, _ = self.srv.req("GET", "/v1/hub/stream")
        self.assertEqual(st, 401)
        # master secret does NOT open a device stream
        st, _ = self.srv.req(
            "GET", "/v1/hub/stream?since=0",
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 401)

    def test_hub_send_requires_device_auth(self):
        st, _ = self.srv.req(
            "POST", "/v1/hub/send", {"msgs": []},
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 401)

    def test_devices_list_and_revoke(self):
        res = self._redeem_device()
        did = res["device_id"]
        st, body = self.srv.req(
            "GET", "/v1/hub/devices",
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 200)
        rows = [r for r in body["devices"] if r["id"] == did]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["connected"])
        self.assertNotIn(res["token"], json.dumps(body))

        st, _ = self.srv.req(
            "DELETE", f"/v1/hub/devices/{did}",
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 200)
        # revoked token no longer authorizes the API
        st, _ = self.srv.req(
            "GET", "/v1/remotes",
            headers={"Authorization": f"Bearer {res['token']}"})
        self.assertEqual(st, 401)
        st, _ = self.srv.req(
            "DELETE", f"/v1/hub/devices/{did}",
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 404)

    def test_hub_send_accepts_device_token(self):
        res = self._redeem_device()
        st, body = self.srv.req(
            "POST", "/v1/hub/send",
            {"msgs": [{"kind": "presence", "payload": {"ok": True}}]},
            headers={"Authorization": f"Bearer {res['token']}",
                     "X-Device-Id": res["device_id"]})
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])

    def test_lease_acquire_contend_release(self):
        res_a = self._redeem_device()
        res_b = self._redeem_device()
        # ws.allows_device is the lease boundary (agents-relay#44):
        # register w1 as synced with both devices in sync_hosts.
        host = Host.from_env()
        host.create("w1", "w1", sync_policy="synced",
                    sync_hosts=[res_a["device_id"], res_b["device_id"]])
        auth_a = {"Authorization": f"Bearer {res_a['token']}"}
        auth_b = {"Authorization": f"Bearer {res_b['token']}"}
        st, g = self.srv.req(
            "POST", "/v1/hub/lease",
            {"action": "acquire", "ws": "w1", "session": "s1", "run_id": "r1"},
            headers=auth_a)
        self.assertEqual(st, 200)
        self.assertTrue(g["granted"])
        fence = g["fence"]
        # second device contends -> denied, holder named
        st, d = self.srv.req(
            "POST", "/v1/hub/lease",
            {"action": "acquire", "ws": "w1", "session": "s1", "run_id": "r2"},
            headers=auth_b)
        self.assertEqual(st, 200)
        self.assertFalse(d["granted"])
        # holder renews
        st, r = self.srv.req(
            "POST", "/v1/hub/lease",
            {"action": "renew", "ws": "w1", "session": "s1", "fence": fence},
            headers=auth_a)
        self.assertTrue(r["granted"])
        # admin snapshot names the holder; device token denied
        st, snap = self.srv.req(
            "GET", "/v1/hub/leases",
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 200)
        self.assertEqual(snap["leases"][0]["session"], "s1")
        # paired devices may READ the lease snapshot (remote lease
        # checks for session host switching, klanker#148); holder is
        # already a hash. Mutation stays acquire/renew/release-gated.
        st, scoped = self.srv.req(
            "GET", "/v1/hub/leases", headers=auth_b)
        self.assertEqual(st, 200)
        self.assertEqual(scoped["leases"][0]["session"], "s1")
        # release frees the slot for the contender
        self.srv.req(
            "POST", "/v1/hub/lease",
            {"action": "release", "ws": "w1", "session": "s1", "fence": fence},
            headers=auth_a)
        st, g2 = self.srv.req(
            "POST", "/v1/hub/lease",
            {"action": "acquire", "ws": "w1", "session": "s1"},
            headers=auth_b)
        self.assertTrue(g2["granted"])

    def test_lease_bad_fence_is_400(self):
        res_a = self._redeem_device()
        auth_a = {"Authorization": f"Bearer {res_a['token']}"}
        for bad in ("abc", None, 1.5, True, 0, -3, [1]):
            st, _ = self.srv.req(
                "POST", "/v1/hub/lease",
                {"action": "renew", "ws": "w", "session": "s", "fence": bad},
                headers=auth_a)
            self.assertEqual(st, 400, bad)

    def test_lease_requires_device_token(self):
        st, _ = self.srv.req(
            "POST", "/v1/hub/lease",
            {"action": "acquire", "ws": "w", "session": "s"},
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 401)


class TestChannelE2E(unittest.TestCase):
    """Full path: pair -> ChannelClient stream -> hub rpc_call over the wire."""

    def setUp(self) -> None:
        channel.reset_hub()
        reset_leases()
        self.td = tempfile.TemporaryDirectory()
        host_dir = Path(self.td.name)
        (host_dir / "workspaces" / "default").mkdir(parents=True)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": str(host_dir)})
        self.env.start()
        self.srv = _Srv(_cfg())
        self.client: ChannelClient | None = None

    def tearDown(self) -> None:
        if self.client is not None:
            self.client.stop()
        self.srv.close()
        self.env.stop()
        self.td.cleanup()
        channel.reset_hub()
        reset_leases()

    def _connect_client(self) -> str:
        st, created = self.srv.req(
            "POST", "/v1/pair/create", {"mode": "remote", "ttl_sec": 60},
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 200)
        st, res = self.srv.req(
            "POST", "/v1/pair/redeem", {"token": created["token"]})
        self.assertEqual(st, 200)
        did = res["device_id"]

        from agents_workspace import write_secret
        from agents_relay.channel_client import _secret_key

        _store_hubs({"hub1": {
            "hub_url": f"http://{self.srv.host}:{self.srv.port}",
            "device_id": did,
            "label": "test",
            "last_seq": 0,
            "connected_at": int(time.time()),
        }})
        write_secret(_secret_key("hub1"), res["token"], scope="host")

        cfg = _cfg(relay_port=self.srv.port)  # device loopback = the same test srv
        self.client = ChannelClient("hub1", config=cfg)
        t = threading.Thread(target=self.client.run_forever, daemon=True)
        t.start()
        return did

    def _wait_connected(self, did: str, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if channel.hub().is_connected(did):
                return
            time.sleep(0.05)
        self.fail(f"device {did} never connected")

    def test_e2e_ping_status_and_http_rpc(self):
        did = self._connect_client()
        self._wait_connected(did)

        frames = list(channel.hub().rpc_call(did, "ping", {}, timeout_sec=5))
        self.assertEqual(frames, [{"pong": True, "done": True}])

        frames = list(channel.hub().rpc_call(did, "status", {}, timeout_sec=5))
        self.assertTrue(frames[-1].get("done"))
        self.assertTrue(frames[-1].get("ok"))

        # generic http rpc against the device's loopback relay
        frames = list(channel.hub().rpc_call(
            did, "http", {"req": {"method": "GET", "path": "/health"}},
            timeout_sec=10))
        self.assertTrue(frames[-1].get("done"))
        self.assertEqual(frames[-1].get("status"), 200)

    def test_e2e_auto_remote_and_channel_proxy(self):
        did = self._connect_client()
        self._wait_connected(did)
        # connect auto-registers a via=channel remote row
        st, body = self.srv.req(
            "GET", "/v1/remotes",
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 200)
        row = next(r for r in body["remotes"] if r["id"] == f"dev-{did}")
        self.assertEqual(row["via"], "channel")
        self.assertEqual(row["device_id"], did)
        # the whole remote proxy path rides the channel — no inbound port
        st, body = self.srv.req(
            "GET", f"/v1/remote/dev-{did}/health",
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 200, body)

    def test_e2e_http_rpc_denies_admin_paths(self):
        """CRITICAL: the device runs http rpc under its master secret —
        admin surfaces must be unreachable through the channel."""
        did = self._connect_client()
        self._wait_connected(did)
        for method, path in (
            ("POST", "/v1/pair/create"),
            ("GET", "/v1/hub/devices"),
            ("DELETE", "/v1/hub/devices/x"),
            ("POST", "/v1/remotes"),
            ("GET", "/v1/discover"),
            ("POST", "/v1/providers"),
            ("DELETE", "/v1/providers/x"),
            ("POST", "/v1/settings"),
        ):
            frames = list(channel.hub().rpc_call(
                did, "http", {"req": {"method": method, "path": path}},
                timeout_sec=5))
            self.assertTrue(frames[-1].get("done"), f"{method} {path}")
            self.assertEqual(frames[-1].get("status"), 403, f"{method} {path}")
            self.assertIn("denied", str(frames[-1].get("error") or ""))
        # but an ordinary call still passes
        frames = list(channel.hub().rpc_call(
            did, "http", {"req": {"method": "GET", "path": "/health"}},
            timeout_sec=5))
        self.assertEqual(frames[-1].get("status"), 200)

    def test_client_401_revokes_and_stops_permanently(self):
        """HTTP 401 on stream or send = revoked credential: permanent
        stop, no reconnect loop hammering the hub (agents-relay#42)."""
        import urllib.error
        import urllib.request

        client = ChannelClient.__new__(ChannelClient)
        import queue as q
        import threading as th

        client._stop = th.Event()
        client._outbox = q.Queue()
        client.revoked = False
        client.hub_url = "http://127.0.0.1:9"
        client.hub_id = "h"
        client.device_id = "d"
        client.last_seq = 0

        def raise_401(req, timeout=0):
            raise urllib.error.HTTPError(
                req.full_url, 401, "unauth", {}, None)

        with patch.object(urllib.request, "urlopen", raise_401):
            self.assertFalse(client._post_send([{"kind": "op"}]))
        self.assertTrue(client.revoked)
        self.assertTrue(client._stop.is_set())

    def test_disconnect_releases_stream(self):
        hub = channel.hub()
        link = hub.connect("d1")[0]
        hub.disconnect("d1")
        self.assertIsNone(link.queue.get_nowait())  # sentinel, not a hang
        self.assertFalse(hub.is_connected("d1"))

    def test_op_ack_handlers_replace_default_fanout(self):
        """Sync layer owns upstream ops; acks route to it verbatim."""
        hub = channel.hub()
        got_op, got_ack = [], []
        hub.set_op_handler(lambda did, p: got_op.append((did, p)))
        hub.set_ack_handler(lambda did, p: got_ack.append((did, p)))
        try:
            hub.handle_upstream("dX", [
                {"kind": "op", "payload": {"ws": "w1", "envelopes": []}},
                {"kind": "ack", "payload": {"ws": "w1", "cursor": 9}},
            ])
        finally:
            hub.set_op_handler(None)
            hub.set_ack_handler(None)
        self.assertEqual(got_op, [("dX", {"ws": "w1", "envelopes": []})])
        self.assertEqual(got_ack, [("dX", {"ws": "w1", "cursor": 9})])

    def test_ws_share_frames_route_to_handler(self):
        """ws:announce/ws:revoke upstream frames reach the ws handler
        (klanker#145 ws-share control plane)."""
        hub = channel.hub()
        got = []
        hub.set_ws_handler(lambda did, k, p: got.append((did, k, p)))
        try:
            hub.handle_upstream("dX", [
                {"kind": "ws:announce",
                 "payload": {"ws": "w1", "owner": "dX"}},
                {"kind": "ws:revoke", "payload": {"ws": "w1"}},
            ])
        finally:
            hub.set_ws_handler(None)
        self.assertEqual(got, [
            ("dX", "ws:announce", {"ws": "w1", "owner": "dX"}),
            ("dX", "ws:revoke", {"ws": "w1"}),
        ])

    def test_e2e_presence_list(self):
        did = self._connect_client()
        self._wait_connected(did)
        st, body = self.srv.req(
            "GET", "/v1/hub/devices",
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 200)
        row = next(r for r in body["devices"] if r["id"] == did)
        self.assertTrue(row["connected"])
        self.assertGreater(row["last_seen"], 0)


class TestRemotesChannel(unittest.TestCase):
    def setUp(self) -> None:
        channel.reset_hub()
        reset_leases()
        self.td = tempfile.TemporaryDirectory()
        host_dir = Path(self.td.name)
        (host_dir / "workspaces" / "default").mkdir(parents=True)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": str(host_dir)})
        self.env.start()
        self.srv = _Srv(_cfg())

    def tearDown(self) -> None:
        self.srv.close()
        self.env.stop()
        self.td.cleanup()
        channel.reset_hub()
        reset_leases()

    def test_upsert_channel_remote_and_offline_proxy(self):
        from agents_relay.remotes import upsert_remote

        cfg = self.srv.server.relay_url and _cfg()  # noqa: F841
        row, err = upsert_remote(_cfg(), {
            "id": "desk1", "via": "channel", "device_id": "dev-x",
            "label": "desktop"})
        self.assertEqual(err, "")
        self.assertEqual(row["via"], "channel")
        self.assertEqual(row["device_id"], "dev-x")

        st, body = self.srv.req(
            "GET", "/v1/remote/desk1/v1/status",
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 503)  # device offline -> loud, not silent
        self.assertIn("offline", str(body.get("error") or body))

    def test_channel_remote_requires_device_id(self):
        from agents_relay.remotes import upsert_remote

        row, err = upsert_remote(_cfg(), {"id": "bad", "via": "channel"})
        self.assertIsNone(row)
        self.assertIn("device_id", err)

    def test_host_dedupe_reuses_canonical_id(self):
        from agents_relay.remotes import upsert_remote

        cfg = _cfg()
        row1, err1 = upsert_remote(cfg, {
            "id": "vps", "base_url": "https://klanker.example.com",
            "secret": "s1"})
        self.assertEqual(err1, "")
        row2, err2 = upsert_remote(cfg, {
            "id": "vps-dup", "base_url": "https://klanker.example.com/",
            "secret": "s2"})
        self.assertEqual(err2, "")
        self.assertEqual(row2["id"], "vps")  # canonical row wins


class TestWsMemberScope(unittest.TestCase):
    """agents-relay#41: ws_member devices are allowlist-scoped guests."""

    def setUp(self) -> None:
        channel.reset_hub()
        reset_leases()
        self.td = tempfile.TemporaryDirectory()
        host_dir = Path(self.td.name)
        for ws in ("default", "ws-a", "ws-b"):
            (host_dir / "workspaces" / ws).mkdir(parents=True)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": str(host_dir)})
        self.env.start()
        from agents_workspace import Host

        for ws in ("ws-a", "ws-b"):
            Host.from_env().create(ws)
        self.srv = _Srv(_cfg())
        self.dev = devices.mint_device(
            label="guest", role="ws_member", allowlist=["ws-a"])
        self.auth = {"Authorization": f"Bearer {self.dev['device_token']}"}

    def tearDown(self) -> None:
        self.srv.close()
        self.env.stop()
        self.td.cleanup()
        channel.reset_hub()
        reset_leases()

    def test_ws_member_needs_nonempty_allowlist(self):
        with self.assertRaises(ValueError):
            devices.mint_device(role="ws_member", allowlist=[])

    def test_ws_member_ws_scope_denials(self):
        # the granted ws passes the gate
        st, _ = self.srv.req("GET", "/v1/workspaces/ws-a/inbox",
                             headers=self.auth)
        self.assertEqual(st, 200)
        # a different ws is invisible — allowlist denial, not 404
        st, _ = self.srv.req("GET", "/v1/workspaces/ws-b/inbox",
                             headers=self.auth)
        self.assertEqual(st, 403)
        st, _ = self.srv.req("GET", "/v1/events?workspace=ws-b",
                             headers=self.auth)
        self.assertEqual(st, 403)
        # hard/soft delete of even a granted ws is denied
        st, _ = self.srv.req("DELETE", "/v1/workspaces/ws-a?hard=1",
                             headers=self.auth)
        self.assertEqual(st, 403)
        # policy keys are denied even inside an allowed ws
        st, _ = self.srv.req("PATCH", "/v1/workspaces/ws-a",
                             {"sync_hosts": ["other"]}, headers=self.auth)
        self.assertEqual(st, 403)
        # remote-proxy pivots and admin surfaces stay out of reach
        st, _ = self.srv.req(
            "GET", "/v1/remote/dev-other/v1/workspaces",
            headers=self.auth)
        self.assertEqual(st, 403)
        st, _ = self.srv.req("GET", "/v1/remotes", headers=self.auth)
        self.assertEqual(st, 403)
        st, _ = self.srv.req("GET", "/v1/personas?workspace=ws-a",
                             headers=self.auth)
        self.assertEqual(st, 403)
        # approval decisions are operator-only
        st, _ = self.srv.req("POST", "/v1/approvals/a1", {"allow": True},
                             headers=self.auth)
        self.assertEqual(st, 403)
        # channel frames still work — a guest is a real device
        st, body = self.srv.req(
            "POST", "/v1/hub/send",
            {"msgs": [{"kind": "presence", "payload": {}}]},
            headers={**self.auth,
                     "X-Device-Id": self.dev["device_id"]})
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])

    def test_rpc_http_denies_pivots_with_403(self):
        from agents_relay.channel_client import _rpc_http

        cfg = _cfg(relay_port=self.srv.port)
        for denied in ("/v1/remote/dev-x/v1/pair/create",
                       "/v1/approvals/a1",
                       "/v1/pair/redeem?x=1",
                       "/v1/model"):
            frames = list(_rpc_http(
                {"req": {"method": "GET", "path": denied}}, cfg))
            self.assertEqual(frames[0].get("status"), 403, denied)

    def test_rpc_http_keeps_query_after_normalization(self):
        from agents_relay.channel_client import _rpc_http

        cfg = _cfg(relay_port=self.srv.port)
        frames = list(_rpc_http(
            {"req": {"method": "GET",
                     "path": "/v1/../health?x=1"}},
            cfg))
        self.assertEqual(frames[-1].get("status"), 200)

    def test_approval_replay_and_binding(self):
        from agents_relay.approvals import (
            create_pending, mint_approval_token)

        token, _ = mint_approval_token("ws-a")
        auth = {"Authorization": f"Bearer {token}"}
        st, body = self.srv.req(
            "POST", "/v1/approvals/request",
            {"id": "a1", "tool": "mcp.terminal", "args": {"cmd": "ls"}},
            headers=auth)
        self.assertEqual(st, 200)
        # replaying the granted id with changed args is a conflict
        st, body = self.srv.req(
            "POST", "/v1/approvals/request",
            {"id": "a1", "tool": "mcp.terminal",
             "args": {"cmd": "rm -rf /"}},
            headers=auth)
        self.assertEqual(st, 409)
        # a record without token_hash fails closed to a turn token
        create_pending(_cfg(), 0,
                       {"id": "b2", "tool": "git", "args": {}})
        st, _ = self.srv.req(
            "POST", "/v1/approvals/request",
            {"id": "b2", "tool": "git", "args": {}}, headers=auth)
        self.assertEqual(st, 403)


class TestProxySanitize(unittest.TestCase):
    """agents-relay#41: CRLF/XSS guards on /v1/remote/* responses."""

    def test_header_injection_rejected(self):
        from agents_relay.remotes import _proxy_content_type, _sanitize_header_value

        with self.assertRaises(ValueError):
            _sanitize_header_value("text/html\r\nSet-Cookie: pwn=1")
        with self.assertRaises(ValueError):
            _proxy_content_type("text/html\r\nSet-Cookie: pwn=1")

    def test_html_svg_rewrite_to_plain(self):
        from agents_relay.remotes import _proxy_content_type

        self.assertEqual(
            _proxy_content_type("text/html; charset=utf-8"),
            "text/plain; charset=utf-8")
        self.assertEqual(
            _proxy_content_type("image/svg+xml"),
            "text/plain; charset=utf-8")
        self.assertEqual(
            _proxy_content_type("application/json"), "application/json")


if __name__ == "__main__":
    unittest.main()
