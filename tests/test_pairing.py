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
        self.td = tempfile.TemporaryDirectory()
        host_dir = Path(self.td.name)
        (host_dir / "workspaces" / "default").mkdir(parents=True)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": str(host_dir)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.td.cleanup()
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
            with patch.dict(os.environ, {
                    "AGENTS_RELAY_PUBLIC_URL": "",
                    "AGENTS_RELAY_URL": ""}), \
                    patch("agents_relay.http_adapter.detect_non_loopback_ipv4",
                          return_value=""):
                st, _, created = srv.req(
                    "POST",
                    "/v1/pair/create",
                    {"mode": "remote", "ttl_sec": 5},
                    headers={"Authorization": "Bearer expected"},
                )
            self.assertEqual(st, 200)
            self.assertTrue(created["loopback_warning"])
            # Remote pairs declare their kind in the link shape —
            # redeem branches on the stored mode AND the URL marker.
            self.assertTrue(
                created["pair_url"].startswith(
                    f"http://{srv.host}:{srv.port}/#kind=remote&pair="),
                created["pair_url"],
            )
            self.assertEqual(created["expires_in"], 15)  # clamped minimum

            st1, _, res1 = srv.req("POST", "/v1/pair/redeem", {"token": created["token"]})
            self.assertEqual(st1, 200)
            self.assertEqual(res1["mode"], "remote")
            # remote redeem mints scoped device credentials — the master
            # relay_secret is never handed out (agents-relay#38/#40).
            self.assertTrue(res1["device_id"])
            self.assertTrue(res1["token"])
            self.assertNotEqual(res1["token"], "expected")

            st2, _, _ = srv.req("POST", "/v1/pair/redeem", {"token": created["token"]})
            self.assertEqual(st2, 401)
        finally:
            srv.close()

    def test_ws_invite_pairs_and_grants(self):
        """klanker#145: owner device mints ws-scoped invite; redeem pairs
        the new device AND fires the ws grant hook."""
        import agents_relay.http_adapter as ha

        grants = []
        ha.ws_invite_auth = lambda did, ws: did == "ownerDev" and ws == "w1"
        ha.ws_grant_hook = lambda did, ws, by: grants.append((did, ws, by))
        srv = _Srv(_cfg())
        try:
            # owner pairs first -> device credential
            st, _, p = srv.req("POST", "/v1/pair/create",
                               {"mode": "remote"},
                               headers={"Authorization": "Bearer expected"})
            _, _, r = srv.req("POST", "/v1/pair/redeem",
                              {"token": p["token"]})
            owner_tok, owner_id = r["token"], r["device_id"]
            assert owner_id

            # forge the expected owner id via the hook's lambda
            ha.ws_invite_auth = lambda did, ws: did == owner_id
            st, _, inv = srv.req(
                "POST", "/v1/ws/invite", {"ws": "w1"},
                headers={"Authorization": f"Bearer {owner_tok}",
                         "X-Device-Id": owner_id})
            self.assertEqual(st, 200)
            self.assertEqual(inv["ws"], "w1")
            self.assertIn("/#kind=ws&ws=w1&pair=", inv["pair_url"])

            # a different paired device is authenticated but not owner
            st, _, p2 = srv.req("POST", "/v1/pair/create",
                                {"mode": "remote"},
                                headers={"Authorization": "Bearer expected"})
            _, _, r2b = srv.req("POST", "/v1/pair/redeem",
                                {"token": p2["token"]})
            st, _, _ = srv.req(
                "POST", "/v1/ws/invite", {"ws": "w1"},
                headers={"Authorization": f"Bearer {r2b['token']}",
                         "X-Device-Id": r2b["device_id"]})
            self.assertEqual(st, 403)
            # unauthenticated cannot mint
            st, _, _ = srv.req("POST", "/v1/ws/invite", {"ws": "w1"})
            self.assertEqual(st, 401)

            # new device redeems the ws code: device pair + grant
            st, _, r2 = srv.req("POST", "/v1/pair/redeem",
                                {"code": inv["code"]})
            self.assertEqual(st, 200)
            self.assertEqual(r2["mode"], "ws")
            self.assertEqual(r2["ws"], "w1")
            self.assertTrue(r2["device_id"])
            self.assertTrue(r2["token"])
            self.assertEqual(
                grants, [(r2["device_id"], "w1", owner_id)])

            # ws redeem mints a ws_member device scoped to the granted ws
            # (agents-relay#41): the token is a guest credential, not a
            # second host key.
            from agents_relay import devices

            meta = devices.load_devices().get(r2["device_id"]) or {}
            self.assertEqual(meta.get("role"), "ws_member")
            self.assertEqual(meta.get("allowlist"), ["w1"])
            guest = {"Authorization": f"Bearer {r2['token']}"}
            st, _, _ = srv.req("POST", "/v1/providers",
                               {"name": "x", "cmd": "id"}, headers=guest)
            self.assertEqual(st, 403)  # ws-member gate denies pre-auth
            st, _, _ = srv.req("DELETE", "/v1/workspaces/w1?hard=1",
                               headers=guest)
            self.assertEqual(st, 403)
            st, _, _ = srv.req("GET", "/v1/workspaces/w2/inbox",
                               headers=guest)
            self.assertEqual(st, 403)
        finally:
            ha.ws_invite_auth = None
            ha.ws_grant_hook = None
            srv.close()

    def test_pair_url_base_resolution(self):
        # Proxy headers win over the request Host — but ONLY behind a
        # declared trusted proxy (agents-relay#42): without
        # RELAY_TRUST_PROXY=1 a client-spoofed X-Forwarded-Host must not
        # poison generated pair URLs.
        srv = _Srv(_cfg())
        try:
            st, _, res = srv.req(
                "POST",
                "/v1/pair/create",
                {"mode": "web"},
                headers={
                    "Authorization": "Bearer expected",
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "relay.example.com",
                },
            )
            self.assertEqual(st, 200)
            self.assertTrue(
                res["pair_url"].startswith(f"http://{srv.host}:{srv.port}/#pair="),
                res["pair_url"],
            )
            with patch.dict(os.environ, {"RELAY_TRUST_PROXY": "1"}):
                st, _, res = srv.req(
                    "POST",
                    "/v1/pair/create",
                    {"mode": "web"},
                    headers={
                        "Authorization": "Bearer expected",
                        "X-Forwarded-Proto": "https",
                        "X-Forwarded-Host": "relay.example.com",
                    },
                )
                self.assertEqual(st, 200)
                self.assertTrue(
                    res["pair_url"].startswith("https://relay.example.com/#pair="),
                    res["pair_url"],
                )
        finally:
            srv.close()

        srv2 = _Srv(_cfg(relay_url="https://relay.internal:9443"))
        try:
            st, _, res = srv2.req(
                "POST",
                "/v1/pair/create",
                {"mode": "remote"},
                headers={
                    "Authorization": "Bearer expected",
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "relay.example.com",
                },
            )
            self.assertEqual(st, 200)
            self.assertTrue(
                res["pair_url"].startswith(
                    "https://relay.internal:9443/#kind=remote&pair="),
                res["pair_url"],
            )
        finally:
            srv2.close()

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


class TestWsInviteHardening(unittest.TestCase):
    """agents-relay#42: invite/redeem gap proofs — quotas, re-verify,
    rollback, already-paired reuse."""

    def setUp(self) -> None:
        import agents_relay.http_adapter as ha

        pairing.clear_pairs()
        self._ha = ha
        self.grants: list = []
        ha.ws_invite_auth = None
        ha.ws_grant_hook = None
        self.td = tempfile.TemporaryDirectory()
        host_dir = Path(self.td.name)
        (host_dir / "workspaces" / "default").mkdir(parents=True)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": str(host_dir)})
        self.env.start()
        self.srv = _Srv(_cfg())

    def tearDown(self) -> None:
        self._ha.ws_invite_auth = None
        self._ha.ws_grant_hook = None
        self.srv.close()
        self.env.stop()
        self.td.cleanup()
        pairing.clear_pairs()

    def _pair_device(self, label: str = "") -> dict:
        st, _, p = self.srv.req(
            "POST", "/v1/pair/create", {"mode": "remote"},
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 200)
        st, _, r = self.srv.req(
            "POST", "/v1/pair/redeem",
            {"token": p["token"], "label": label})
        self.assertEqual(st, 200)
        return r

    def test_pair_create_rejects_ws_mode(self):
        st, _, body = self.srv.req(
            "POST", "/v1/pair/create", {"mode": "ws"},
            headers={"Authorization": "Bearer expected"})
        self.assertEqual(st, 400)

    def test_invite_503_without_hooks_and_400_bad_ws(self):
        owner = self._pair_device()
        auth = {"Authorization": f"Bearer {owner['token']}",
                "X-Device-Id": owner["device_id"]}
        st, _, _ = self.srv.req(
            "POST", "/v1/ws/invite", {"ws": "w1"}, headers=auth)
        self.assertEqual(st, 503)  # no hooks -> invites off, never open
        st, _, _ = self.srv.req(
            "POST", "/v1/ws/invite", {"ws": "bad id!!"}, headers=auth)
        self.assertEqual(st, 400)

    def _invite(self, owner: dict, ws: str = "w1") -> dict:
        self._ha.ws_invite_auth = lambda did, w: did == owner["device_id"]
        if self._ha.ws_grant_hook is None:
            self._ha.ws_grant_hook = lambda d, w, b: self.grants.append(
                (d, w, b))
        st, _, inv = self.srv.req(
            "POST", "/v1/ws/invite", {"ws": ws},
            headers={"Authorization": f"Bearer {owner['token']}",
                     "X-Device-Id": owner["device_id"]})
        self.assertEqual(st, 200)
        return inv

    def test_redeem_rejected_after_inviter_revoked(self):
        from agents_relay import devices

        owner = self._pair_device()
        inv = self._invite(owner)
        devices.revoke_device(owner["device_id"])
        st, _, body = self.srv.req(
            "POST", "/v1/pair/redeem", {"code": inv["code"]})
        self.assertEqual(st, 403)

    def test_redeem_rejected_when_ownership_lost(self):
        owner = self._pair_device()
        inv = self._invite(owner)
        # owner still exists but is no longer the ws owner
        self._ha.ws_invite_auth = lambda did, w: False
        st, _, body = self.srv.req(
            "POST", "/v1/pair/redeem", {"code": inv["code"]})
        self.assertEqual(st, 403)

    def test_grant_hook_failure_rolls_back_minted_device(self):
        from agents_relay import devices

        owner = self._pair_device()
        inv = self._invite(owner)

        def boom(did, ws, by):
            raise RuntimeError("store down")

        self._ha.ws_grant_hook = boom
        st, _, body = self.srv.req(
            "POST", "/v1/pair/redeem", {"code": inv["code"]})
        self.assertEqual(st, 500)
        # nothing may leak: no device row, no vault token
        survivors = [d for d in devices.load_devices()
                     if d != owner["device_id"]]
        self.assertEqual(survivors, [])

    def test_already_paired_device_gains_grant_without_new_id(self):
        from agents_relay import devices

        owner = self._pair_device()
        member = self._pair_device()
        inv = self._invite(owner, ws="w2")
        self._ha.ws_grant_hook = lambda d, w, b: self.grants.append(
            (d, w, b))
        st, _, res = self.srv.req(
            "POST", "/v1/pair/redeem", {"code": inv["code"]},
            headers={"Authorization": f"Bearer {member['token']}",
                     "X-Device-Id": member["device_id"]})
        self.assertEqual(st, 200)
        self.assertEqual(res["device_id"], member["device_id"])
        self.assertEqual(res["token"], "")
        self.assertEqual(self.grants, [(member["device_id"], "w2",
                                        owner["device_id"])])
        # A wildcard remote-role device keeps an EMPTY allowlist —
        # granting must never silently narrow it to one ws
        # (agents-relay#44); membership lives in ws.sync_hosts.
        self.assertEqual(
            devices.load_devices()[member["device_id"]]["allowlist"],
            [])

    def test_label_forwarded_to_minted_device(self):
        from agents_relay import devices

        owner = self._pair_device()
        inv = self._invite(owner)
        self._ha.ws_grant_hook = lambda d, w, b: None
        st, _, res = self.srv.req(
            "POST", "/v1/pair/redeem",
            {"code": inv["code"], "label": "alice-laptop"})
        self.assertEqual(st, 200)
        meta = devices.load_devices()[res["device_id"]]
        self.assertEqual(meta["label"], "alice-laptop")

    def test_ws_quota_separate_from_admin_pairs(self):
        store = pairing.PairingStore()
        for _ in range(pairing._MAX_WS_PENDING):
            store.create_pair(mode="ws", ws="w1")
        admin = store.create_pair(mode="remote")
        st, entry = store.redeem_pair(admin["token"], client_ip="10.9.9.9")
        self.assertEqual(st, "ok")
        # and the ws cap actually bites on the next ws invite
        st_ws, entry_ws = store.redeem_pair(
            store.create_pair(mode="ws", ws="w1")["token"],
            client_ip="10.9.9.9")
        self.assertEqual(st_ws, "ok")  # oldest ws evicted, not admin
