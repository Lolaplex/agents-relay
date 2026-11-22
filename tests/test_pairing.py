"""Single-use 60s in-memory QR/code pairing tests."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from agents_relay import pairing
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import _redact_log_text, serve_http


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

    def req(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        hdrs = dict(headers or {})
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        conn = HTTPConnection(self.host, self.port, timeout=3)
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


class TestPairing(unittest.TestCase):
    def setUp(self) -> None:
        pairing.clear_pairs()

    def tearDown(self) -> None:
        pairing.clear_pairs()

    def test_create_requires_auth_and_clamps_ttl(self):
        srv = _Srv(_cfg())
        try:
            st, _, _ = srv.req("POST", "/v1/pair/create", {"mode": "web"})
            self.assertEqual(st, 401)

            st, _, res = srv.req(
                "POST",
                "/v1/pair/create",
                {"mode": "web", "ttl_sec": 999},
                headers={"Authorization": "Bearer expected"},
            )
            self.assertEqual(st, 200)
            self.assertTrue(res["ok"])
            self.assertEqual(res["expires_in"], 120)
            self.assertIn("/#pair=", res["pair_url"])
            self.assertNotIn("?token=", res["pair_url"])
        finally:
            srv.close()

    def test_web_redeem_issues_cookie_once_hides_master_token_and_never_touches_disk(self):
        with tempfile.TemporaryDirectory() as td:
            host_dir = Path(td)
            (host_dir / "workspaces" / "default").mkdir(parents=True)
            with patch.dict(os.environ, {"AGENTS_HOST_DIR": str(host_dir)}):
                srv = _Srv(_cfg())
                try:
                    st, _, created = srv.req(
                        "POST",
                        "/v1/pair/create",
                        {"mode": "web", "ttl_sec": 60},
                        headers={"Authorization": "Bearer expected"},
                    )
                    self.assertEqual(st, 200)
                    code = created["code"]
                    token = created["token"]

                    # Verify zero pairing secrets written to host directory on disk.
                    for p in host_dir.rglob("*"):
                        if p.is_file():
                            text = p.read_text(encoding="utf-8", errors="replace")
                            self.assertNotIn(code, text)
                            self.assertNotIn(token, text)

                    # Unauthed redeem with short display code works cleanly once.
                    st1, hdrs1, res1 = srv.req("POST", "/v1/pair/redeem", {"code": code.lower()})
                    self.assertEqual(st1, 200)
                    self.assertEqual(res1["mode"], "web")
                    self.assertNotIn("token", res1)
                    self.assertNotIn("expected", json.dumps(res1))
                    cookies = [v for k, v in hdrs1 if k.lower() == "set-cookie"]
                    self.assertTrue(any("relay_web=" in c and "HttpOnly" in c for c in cookies))

                    # Issued cookie grants authenticated access to /v1/workspaces.
                    cookie_kv = cookies[0].split(";", 1)[0]
                    st_ws, _, ws_body = srv.req(
                        "GET", "/v1/workspaces", headers={"Cookie": cookie_kv}
                    )
                    self.assertEqual(st_ws, 200)
                    self.assertTrue(ws_body["ok"])

                    # Falsifying replay: second redeem MUST fail 401.
                    st2, _, _ = srv.req("POST", "/v1/pair/redeem", {"code": code})
                    self.assertEqual(st2, 401)
                    st3, _, _ = srv.req("POST", "/v1/pair/redeem", {"token": token})
                    self.assertEqual(st3, 401)
                finally:
                    srv.close()

    def test_remote_redeem_returns_token_once(self):
        srv = _Srv(_cfg())
        try:
            st, _, created = srv.req(
                "POST",
                "/v1/pair/create",
                {"mode": "remote", "ttl_sec": 5},
                headers={"Authorization": "Bearer expected"},
            )
            self.assertEqual(st, 200)
            self.assertEqual(created["expires_in"], 15)  # clamped minimum

            st1, _, res1 = srv.req("POST", "/v1/pair/redeem", {"token": created["token"]})
            self.assertEqual(st1, 200)
            self.assertEqual(res1["mode"], "remote")
            self.assertEqual(res1["token"], "expected")

            st2, _, _ = srv.req("POST", "/v1/pair/redeem", {"token": created["token"]})
            self.assertEqual(st2, 401)
        finally:
            srv.close()

    def test_rate_limit_and_expiry_and_log_redaction(self):
        now = [100.0]
        store = pairing.PairingStore(clock=lambda: now[0])
        created = store.create_pair(mode="web", ttl_sec=30)
        now[0] = 131.0  # past 30s expiry
        status_exp, _ = store.redeem_pair(created["token"], client_ip="10.0.0.1")
        self.assertEqual(status_exp, "invalid")

        # 4 more bad guesses from same IP -> total 5 failures -> 6th hits rate limit.
        fresh = store.create_pair(mode="web", ttl_sec=60)
        for _ in range(4):
            s_bad, _ = store.redeem_pair("WRONG-CODE", client_ip="10.0.0.1")
            self.assertEqual(s_bad, "invalid")
        s_rl, _ = store.redeem_pair(fresh["code"], client_ip="10.0.0.1")
        self.assertEqual(s_rl, "rate_limited")

        # Different IP still succeeds once.
        s_ok, _ = store.redeem_pair(fresh["code"], client_ip="10.0.0.2")
        self.assertEqual(s_ok, "ok")

        # Log URL secret redaction covers code= and pair=.
        red = _redact_log_text("GET /?code=ABCD-EFGH&pair=secretTok HTTP/1.1")
        self.assertNotIn("ABCD-EFGH", red)
        self.assertNotIn("secretTok", red)
        self.assertIn("[REDACTED]", red)

    def test_concurrent_redeem_succeeds_at_most_once(self):
        created = pairing.create_pair(mode="web", ttl_sec=60)
        results: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def worker(i: int) -> None:
            barrier.wait()
            st, _ = pairing.redeem_pair(created["token"], client_ip=f"10.0.1.{i}")
            with lock:
                results.append(st)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3)
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("invalid"), 5)
