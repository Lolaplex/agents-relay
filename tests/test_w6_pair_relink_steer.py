"""W6 regression tests: pair-link advertising, relink reuse, web steering."""
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

import agents_relay.http_adapter as ha
from agents_relay import channel_client as cc
from agents_relay import devices, pairing
from agents_relay import web as web_mod
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.loop_client import LoopTurnResult
from agents_relay.turn import StreamEvent


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
        web_token="tok",
        web_user="web",
    )
    base.update(overrides)
    return RelayConfig(**base)


class _Srv:
    def __init__(self, cfg: RelayConfig, *, on_turn=None) -> None:
        self.server = serve_http(cfg, on_turn=on_turn)
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
            parsed = json.loads(data)
        except json.JSONDecodeError:
            parsed = data
        return status, parsed


class _W6Base(unittest.TestCase):
    def setUp(self) -> None:
        pairing.clear_pairs()
        self.td = tempfile.TemporaryDirectory()
        self.host_dir = Path(self.td.name)
        (self.host_dir / "workspaces" / "default").mkdir(parents=True)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": str(self.host_dir)})
        self.env.start()
        web_mod.steer_record_hook = None
        self._clear_active()

    def tearDown(self) -> None:
        self._clear_active()
        web_mod.steer_record_hook = None
        pairing.clear_pairs()
        self.env.stop()
        self.td.cleanup()

    @staticmethod
    def _clear_active() -> None:
        with web_mod._ACTIVE_LOCK:
            for entry in web_mod._ACTIVE.values():
                entry["cancel"].set()
            web_mod._ACTIVE.clear()

    def _workspace(self, ws_id: str) -> None:
        root = self.host_dir / "workspaces" / ws_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "workspace.json").write_text(
            json.dumps({"id": ws_id}), encoding="utf-8")

    @staticmethod
    def _auth(token: str = "expected") -> dict:
        return {"Authorization": f"Bearer {token}"}


class TestLoopbackPairLinks(_W6Base):
    def _create(self, srv: _Srv, mode: str = "remote") -> dict:
        st, body = srv.req(
            "POST", "/v1/pair/create", {"mode": mode},
            headers=self._auth())
        self.assertEqual(st, 200, body)
        return body

    def test_explicit_public_and_relay_urls_win_without_probe(self):
        srv = _Srv(_cfg())
        calls: list[str] = []

        def probe() -> str:
            calls.append("called")
            return "100.64.2.3"

        try:
            with patch.object(ha, "detect_non_loopback_ipv4", probe):
                env = {
                    "AGENTS_RELAY_PUBLIC_URL": "",
                    "AGENTS_RELAY_URL": "https://relay.example:9443",
                }
                with patch.dict(os.environ, env):
                    body = self._create(srv)
                public_env = {
                    "AGENTS_RELAY_PUBLIC_URL": "https://public.example:9443",
                    "AGENTS_RELAY_URL": "https://relay.example:9443",
                }
                with patch.dict(os.environ, public_env):
                    public_body = self._create(srv)
            self.assertEqual(calls, [])
            self.assertTrue(
                body["pair_url"].startswith(
                    "https://relay.example:9443/#kind=remote&pair="),
                body["pair_url"],
            )
            self.assertEqual(body["advertised_url"], "https://relay.example:9443")
            self.assertTrue(
                public_body["pair_url"].startswith(
                    "https://public.example:9443/#kind=remote&pair="),
                public_body["pair_url"],
            )
            self.assertEqual(
                public_body["advertised_url"], "https://public.example:9443")
            self.assertFalse(body["loopback_warning"])
            self.assertFalse(body["loopback_substituted"])
        finally:
            srv.close()

    def test_loopback_remote_pair_prefers_tailscale_ip(self):
        srv = _Srv(_cfg())
        try:
            env = {"AGENTS_RELAY_PUBLIC_URL": "", "AGENTS_RELAY_URL": ""}
            with patch.dict(os.environ, env), \
                    patch.object(ha, "_tailscale_ipv4", return_value="100.64.2.3"), \
                    patch.object(ha, "_default_route_ipv4", return_value="192.168.1.20"):
                body = self._create(srv)
            expected = f"http://100.64.2.3:{srv.port}"
            self.assertTrue(
                body["pair_url"].startswith(expected + "/#kind=remote&pair="),
                body["pair_url"],
            )
            self.assertEqual(body["advertised_url"], expected)
            self.assertFalse(body["loopback_warning"])
            self.assertTrue(body["loopback_substituted"])
        finally:
            srv.close()

    def test_cli_auto_bound_relay_env_does_not_block_substitution(self):
        srv = _Srv(_cfg())
        try:
            auto_url = f"http://{srv.host}:{srv.port}"
            env = {"AGENTS_RELAY_PUBLIC_URL": "", "AGENTS_RELAY_URL": auto_url}
            with patch.dict(os.environ, env), \
                    patch.object(ha, "_tailscale_ipv4", return_value="100.64.2.3"), \
                    patch.object(ha, "_default_route_ipv4", return_value="192.168.1.20"):
                body = self._create(srv)
            self.assertTrue(
                body["pair_url"].startswith(
                    f"http://100.64.2.3:{srv.port}/#kind=remote&pair="),
                body["pair_url"],
            )
            self.assertTrue(body["loopback_substituted"])
        finally:
            srv.close()

    def test_loopback_remote_pair_falls_back_to_lan_ip(self):
        srv = _Srv(_cfg())
        try:
            env = {"AGENTS_RELAY_PUBLIC_URL": "", "AGENTS_RELAY_URL": ""}
            with patch.dict(os.environ, env), \
                    patch.object(ha, "_tailscale_ipv4", return_value=""), \
                    patch.object(ha, "_default_route_ipv4", return_value="192.168.1.20"):
                body = self._create(srv)
            expected = f"http://192.168.1.20:{srv.port}"
            self.assertTrue(
                body["pair_url"].startswith(expected + "/#kind=remote&pair="),
                body["pair_url"],
            )
            self.assertEqual(body["advertised_url"], expected)
            self.assertFalse(body["loopback_warning"])
            self.assertTrue(body["loopback_substituted"])
        finally:
            srv.close()

    def test_loopback_only_host_sets_warning_without_crashing(self):
        srv = _Srv(_cfg())
        try:
            env = {"AGENTS_RELAY_PUBLIC_URL": "", "AGENTS_RELAY_URL": ""}
            with patch.dict(os.environ, env), \
                    patch.object(ha, "_tailscale_ipv4", return_value=""), \
                    patch.object(ha, "_default_route_ipv4", return_value=""):
                body = self._create(srv)
            expected = f"http://{srv.host}:{srv.port}"
            self.assertTrue(
                body["pair_url"].startswith(expected + "/#kind=remote&pair="),
                body["pair_url"],
            )
            self.assertEqual(body["advertised_url"], expected)
            self.assertTrue(body["loopback_warning"])
            self.assertFalse(body["loopback_substituted"])
        finally:
            srv.close()

    def test_web_pair_may_stay_loopback(self):
        srv = _Srv(_cfg())
        try:
            env = {"AGENTS_RELAY_PUBLIC_URL": "", "AGENTS_RELAY_URL": ""}
            with patch.dict(os.environ, env), \
                    patch.object(ha, "_tailscale_ipv4", return_value="100.64.2.3"), \
                    patch.object(ha, "_default_route_ipv4", return_value="192.168.1.20"):
                body = self._create(srv, mode="web")
            self.assertTrue(
                body["pair_url"].startswith(
                    f"http://{srv.host}:{srv.port}/#pair="),
                body["pair_url"],
            )
            self.assertNotIn("loopback_warning", body)
        finally:
            srv.close()

    def test_ws_invite_uses_the_same_loopback_substitution(self):
        ha.ws_invite_auth = lambda did, ws: did == "__admin__" and ws == "w1"
        ha.ws_grant_hook = lambda did, ws, by: None
        srv = _Srv(_cfg())
        try:
            env = {"AGENTS_RELAY_PUBLIC_URL": "", "AGENTS_RELAY_URL": ""}
            with patch.dict(os.environ, env), \
                    patch.object(ha, "_tailscale_ipv4", return_value="100.64.2.3"), \
                    patch.object(ha, "_default_route_ipv4", return_value="192.168.1.20"):
                st, body = srv.req(
                    "POST", "/v1/ws/invite", {"ws": "w1"},
                    headers=self._auth())
            self.assertEqual(st, 200, body)
            expected = f"http://100.64.2.3:{srv.port}"
            self.assertTrue(
                body["pair_url"].startswith(
                    expected + "/#kind=ws&ws=w1&pair="),
                body["pair_url"],
            )
            self.assertTrue(body["loopback_substituted"])
        finally:
            ha.ws_invite_auth = None
            ha.ws_grant_hook = None
            srv.close()

    def test_detector_injection_prefers_tailscale_then_lan(self):
        self.assertEqual(
            ha.detect_non_loopback_ipv4(
                tailscale_probe=lambda: "100.64.9.8",
                route_probe=lambda: "192.168.1.9",
            ),
            "100.64.9.8",
        )
        self.assertEqual(
            ha.detect_non_loopback_ipv4(
                tailscale_probe=lambda: "",
                route_probe=lambda: "192.168.1.9",
            ),
            "192.168.1.9",
        )
        self.assertEqual(
            ha.detect_non_loopback_ipv4(
                tailscale_probe=lambda: "not-an-ip",
                route_probe=lambda: "127.0.0.1",
            ),
            "",
        )


class TestRemoteRelinkReuse(_W6Base):
    def _pair_create(self, srv: _Srv) -> dict:
        st, body = srv.req(
            "POST", "/v1/pair/create", {"mode": "remote"},
            headers=self._auth())
        self.assertEqual(st, 200, body)
        return body

    def _redeem(self, srv: _Srv, credential: str, *, token: str = "",
                label: str = "") -> tuple[int, dict]:
        headers = self._auth(token) if token else {}
        return srv.req(
            "POST", "/v1/pair/redeem",
            {"token": credential, "label": label}, headers=headers)

    def _mint_remote_device(self, srv: _Srv, label: str = "first") -> dict:
        pair = self._pair_create(srv)
        st, body = self._redeem(srv, pair["token"], label=label)
        self.assertEqual(st, 200, body)
        self.assertTrue(body["token"])
        return body

    def test_remote_redeem_reuses_existing_device_without_rotation(self):
        srv = _Srv(_cfg())
        try:
            first = self._mint_remote_device(srv)
            pair = self._pair_create(srv)
            st, body = self._redeem(
                srv, pair["token"], token=first["token"], label="relinked")
            self.assertEqual(st, 200, body)
            self.assertTrue(body["reused"])
            self.assertEqual(body["device_id"], first["device_id"])
            self.assertEqual(body["token"], "")
            self.assertEqual(len(devices.load_devices()), 1)
            self.assertEqual(
                devices.device_id_for_token(first["token"]), first["device_id"])
            meta = devices.load_devices()[first["device_id"]]
            self.assertEqual(meta["role"], "host")
            self.assertEqual(meta["label"], "relinked")
        finally:
            srv.close()

    def test_remote_reuse_preserves_client_role(self):
        srv = _Srv(_cfg())
        try:
            client = devices.mint_device(
                label="operator", role="client", allowlist=[])
            pair = self._pair_create(srv)
            st, body = self._redeem(
                srv, pair["token"], token=client["device_token"])
            self.assertEqual(st, 200, body)
            self.assertTrue(body["reused"])
            self.assertEqual(body["device_id"], client["device_id"])
            self.assertEqual(body["role"], "client")
            self.assertEqual(
                devices.load_devices()[client["device_id"]]["role"], "client")
        finally:
            srv.close()

    def test_admin_remote_pair_can_upgrade_ws_member_in_place(self):
        srv = _Srv(_cfg())
        try:
            member = devices.mint_device(
                label="guest", role="ws_member", allowlist=["w1"])
            pair = self._pair_create(srv)
            st, body = self._redeem(
                srv, pair["token"], token=member["device_token"])
            self.assertEqual(st, 200, body)
            self.assertTrue(body["reused"])
            self.assertEqual(body["device_id"], member["device_id"])
            self.assertEqual(body["role"], "host")
            meta = devices.load_devices()[member["device_id"]]
            self.assertEqual(meta["role"], "host")
            self.assertEqual(meta["allowlist"], [])
            self.assertEqual(len(devices.load_devices()), 1)
        finally:
            srv.close()

    def test_non_admin_remote_pair_cannot_upgrade_ws_member(self):
        srv = _Srv(_cfg())
        try:
            member = devices.mint_device(
                label="guest", role="ws_member", allowlist=["w1"])
            foreign = pairing.create_pair(mode="remote", granted_by="peer-device")
            st, body = self._redeem(
                srv, foreign["token"], token=member["device_token"])
            self.assertEqual(st, 403)
            self.assertIn("cannot upgrade", body["error"])
            meta = devices.load_devices()[member["device_id"]]
            self.assertEqual(meta["role"], "ws_member")
            self.assertEqual(meta["allowlist"], ["w1"])
            self.assertEqual(len(devices.load_devices()), 1)
        finally:
            srv.close()


class TestHubUrlNormalization(_W6Base):
    def test_normalize_hub_url_variants(self):
        cases = {
            "HTTP://LOCALHOST:80/": "http://127.0.0.1",
            "http://127.0.0.1/": "http://127.0.0.1",
            "https://EXAMPLE.com:443/": "https://example.com",
            "http://Hub:8787/": "http://hub:8787",
            "https://example.com:444/": "https://example.com:444",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(cc._normalize_hub_url(raw), expected)

    def test_connect_hub_matches_normalized_existing_link(self):
        captured = {}

        def fake_redeem(hub_url, cred, *, label="", bearer=""):
            captured["hub_url"] = hub_url
            captured["bearer"] = bearer
            return {"ok": True, "mode": "remote", "host_id": "h1",
                    "device_id": "d123", "token": ""}

        cc._store_hubs({"h1": {
            "hub_url": "HTTP://LOCALHOST:80/",
            "device_id": "d123",
            "label": "stored",
            "last_seq": 7,
            "connected_at": 1,
        }})
        with patch.object(cc, "redeem_device", fake_redeem), \
                patch.object(cc, "hub_token", lambda hid: "OLDTOK"):
            res = cc.connect_hub("http://127.0.0.1/", "PAIRCODE", label="lab")
        self.assertEqual(captured["hub_url"], "http://127.0.0.1")
        self.assertEqual(captured["bearer"], "OLDTOK")
        self.assertTrue(res["reused"])
        self.assertEqual(res["device_id"], "d123")
        rows = cc._load_hubs()
        self.assertEqual(list(rows), ["h1"])
        self.assertEqual(rows["h1"]["hub_url"], "http://127.0.0.1")


class TestWebSessionSteer(_W6Base):
    def _start_run(self, srv: _Srv, *, run: str, session: str,
                   workspace: str = "", persona: str = ""):
        gate = threading.Event()
        result: dict = {}

        def fake_stream(params, config, *, cancel=None, on_pid=None):
            gate.wait(timeout=5)
            yield StreamEvent(
                type="trailer", session=session, user_id="u",
                alias="", returncode=0)

        # Parsing the SSE body needs one raw streaming connection; _Srv.req
        # is still enough for the ordinary JSON inject assertions.
        def raw_worker() -> None:
            conn = HTTPConnection(srv.host, srv.port, timeout=5)
            conn.request(
                "POST", "/v1/chat/stream",
                body=json.dumps({"text": "start", "run": run,
                                 "session": session, "workspace": workspace,
                                 "persona": persona}),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer expected"},
            )
            resp = conn.getresponse()
            result["status"] = resp.status
            result["body"] = resp.read().decode()
            conn.close()

        def patched_worker() -> None:
            with patch("agents_relay.web.iter_loop_stream_live", fake_stream):
                raw_worker()

        thread = threading.Thread(target=patched_worker, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and run not in web_mod.active_runs():
            time.sleep(0.02)
        if run not in web_mod.active_runs():
            raise AssertionError(
                f"{run} never became active; response={result!r}")
        return thread, result, gate

    def test_active_web_session_steer_publishes_sse_and_queues(self):
        self._workspace("w1")
        srv = _Srv(_cfg())
        gate = threading.Event()
        try:
            thread, result, gate = self._start_run(
                srv, run="r-steer", session="ses-steer",
                workspace="w1", persona="p1")

            class ClosedPipe:
                def writable(self):
                    return True

                def write(self, data):
                    raise BrokenPipeError("closed")

                def flush(self):
                    raise BrokenPipeError("closed")

            with web_mod._ACTIVE_LOCK:
                entry = web_mod._ACTIVE["r-steer"]
                entry["stdin"] = ClosedPipe()

            st, body = srv.req(
                "POST", "/v1/inject",
                {"session_id": "ses-steer", "session": "ses-steer",
                 "text": "focus on the failing test", "workspace": "w1",
                 "persona": "p1", "user": "web"},
                headers=self._auth())
            self.assertEqual(st, 200, body)
            self.assertEqual(
                body,
                {"ok": True, "steered": True,
                 "session": "ses-steer", "active": True},
            )
            self.assertEqual(entry["steer_queue"][0]["text"],
                             "focus on the failing test")
            gate.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result["status"], 200)
            frames = [
                json.loads(line[6:])
                for line in result["body"].splitlines()
                if line.startswith("data: ")
            ]
            steer = [f for f in frames if f.get("type") == "steer"]
            self.assertEqual(
                steer,
                [{"type": "steer", "text": "focus on the failing test",
                  "session": "ses-steer"}],
            )
        finally:
            gate.set()
            srv.close()

    def test_idle_web_session_steer_returns_409_loudly(self):
        srv = _Srv(_cfg())
        try:
            st, body = srv.req(
                "POST", "/v1/inject",
                {"session_id": "ses-idle", "text": "hello", "user": "web"},
                headers=self._auth())
            self.assertEqual(st, 409)
            self.assertEqual(
                body,
                {"error": "no active run for session",
                 "session": "ses-idle", "active": False},
            )
        finally:
            srv.close()

    def test_web_session_steer_requires_auth(self):
        srv = _Srv(_cfg())
        try:
            st, _ = srv.req(
                "POST", "/v1/inject",
                {"session_id": "ses-x", "text": "hello", "user": "web"})
            self.assertEqual(st, 401)
        finally:
            srv.close()

    def test_ws_member_wrong_workspace_gets_403(self):
        member = devices.mint_device(
            label="guest", role="ws_member", allowlist=["w1"])
        srv = _Srv(_cfg())
        try:
            st, _ = srv.req(
                "POST", "/v1/inject",
                {"session_id": "ses-x", "text": "hello",
                 "workspace": "w2", "user": "web"},
                headers=self._auth(member["device_token"]))
            self.assertEqual(st, 403)
        finally:
            srv.close()

    def test_numeric_telegram_inject_still_wins_and_envelope_stays_clean(self):
        seen = {}

        def fake_turn(**kwargs):
            seen.update(kwargs)
            return LoopTurnResult(
                reply="telegram ok", session="ses-tg", user_id="u",
                alias="telegram:12345", returncode=0, stderr="")

        cfg = _cfg(
            telegram_bot_token="fake_bot_token",
            telegram_allowed_chat_ids=(12345,),
        )
        srv = _Srv(cfg, on_turn=fake_turn)
        try:
            with patch("agents_relay.telegram_adapter.send_message",
                       return_value={"result": {"message_id": 1}}), \
                    patch("agents_relay.telegram_adapter.edit_message"):
                st, body = srv.req(
                    "POST", "/v1/inject",
                    {"chat_id": 12345, "text": "hi",
                     "session": "ses-tg", "session_id": "ses-tg"},
                    headers=self._auth())
            self.assertEqual(st, 200, body)
            self.assertEqual(body["reply"], "telegram ok")
            self.assertEqual(seen["channel"], "telegram")
            self.assertEqual(seen["message"], "hi")
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
