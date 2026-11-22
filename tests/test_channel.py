"""Hub channel v0 (agents-relay#40): device creds, SSE+POST, seq/replay, rpc."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from agents_relay import channel, devices
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

    def test_seq_replay_and_kick(self):
        hub = channel.hub()
        link = hub.connect("d1")
        self.assertEqual(hub.publish("d1", "op", {"a": 1}), 1)
        self.assertEqual(hub.publish("d1", "op", {"a": 2}), 2)
        # reconnect: same seq space, replay ring survives
        link2 = hub.connect("d1")
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
        self.assertEqual(out, {"acked": 0})

    def test_upstream_broadcast_and_ack(self):
        hub = channel.hub()
        hub.connect("d1")
        hub.connect("d2")
        out = hub.handle_upstream("d1", [
            {"kind": "op", "payload": {"n": 1}},
            {"kind": "presence", "payload": {"ok": True}},
            {"kind": "bogus", "payload": {}},
        ])
        self.assertEqual(out, {"acked": 1})
        msg = hub.link("d2").queue.get_nowait()
        self.assertEqual(msg["kind"], "op")
        self.assertEqual(msg["payload"]["n"], 1)
        # sender must not get its own op back
        self.assertTrue(hub.link("d1").queue.empty())

    def test_rpc_call_roundtrip(self):
        hub = channel.hub()
        link = hub.connect("d1")
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
        # device token authorizes the API like a scoped credential
        st, body = self.srv.req(
            "GET", "/v1/remotes",
            headers={"Authorization": f"Bearer {res['token']}"})
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])

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


class TestChannelE2E(unittest.TestCase):
    """Full path: pair -> ChannelClient stream -> hub rpc_call over the wire."""

    def setUp(self) -> None:
        channel.reset_hub()
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


if __name__ == "__main__":
    unittest.main()
