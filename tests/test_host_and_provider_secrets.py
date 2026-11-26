"""Tests for friendly host_label surfaces and POST /v1/providers/secret."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from agents_relay import devices
from agents_relay.channel_client import _rpc_denied
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_workspace import Host, resolve_secret


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
        status = resp.status
        conn.close()
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            parsed = data
        return status, parsed


class TestHostLabelAndProviderSecrets(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.host_dir = Path(self.td.name)
        Host(self.host_dir).create("default", "default")
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": str(self.host_dir)})
        self.env.start()
        self.srv = _Srv(_cfg())

    def tearDown(self) -> None:
        self.srv.close()
        self.env.stop()
        self.td.cleanup()

    def test_health_host_routes_and_ws_hosts_use_friendly_label(self) -> None:
        st, health = self.srv.req("GET", "/health")
        self.assertEqual(st, 200)
        self.assertTrue(health.get("host_label"))
        self.assertNotEqual(health["host_label"], health["host_id"])

        admin = {"Authorization": "Bearer expected"}
        dev = devices.mint_device(label="peer", role="host")
        dev_auth = {"Authorization": f"Bearer {dev['device_token']}", "X-Device-Id": dev["device_id"]}

        # Device token cannot read/write /v1/host.
        st_d1, _ = self.srv.req("GET", "/v1/host", headers=dev_auth)
        self.assertEqual(st_d1, 401)
        st_d2, _ = self.srv.req("PATCH", "/v1/host", {"label": "hijack"}, headers=dev_auth)
        self.assertEqual(st_d2, 401)

        # Admin sets label cleanly.
        st_p, res_p = self.srv.req("PATCH", "/v1/host", {"label": "Studio Desktop"}, headers=admin)
        self.assertEqual(st_p, 200)
        self.assertEqual(res_p["host_label"], "Studio Desktop")

        st_g, res_g = self.srv.req("GET", "/v1/host", headers=admin)
        self.assertEqual(st_g, 200)
        self.assertEqual(res_g["host_label"], "Studio Desktop")

        st_w, res_w = self.srv.req("GET", "/v1/workspaces/default/hosts", headers=admin)
        self.assertEqual(st_w, 200)
        self_rows = [h for h in res_w["hosts"] if h.get("self")]
        self.assertEqual(len(self_rows), 1)
        self.assertEqual(self_rows[0]["label"], "Studio Desktop")

    def test_provider_secret_admin_only_blocks_infra_and_never_echoes(self) -> None:
        admin = {"Authorization": "Bearer expected"}
        dev = devices.mint_device(label="peer", role="host")
        dev_auth = {"Authorization": f"Bearer {dev['device_token']}", "X-Device-Id": dev["device_id"]}

        st_dev, _ = self.srv.req(
            "POST", "/v1/providers/secret",
            {"key_env": "OPENROUTER_API_KEY", "value": "sk-dev"},
            headers=dev_auth,
        )
        self.assertEqual(st_dev, 401)

        for forbidden in ("RELAY_SECRET", "TELEGRAM_BOT_TOKEN", "DEVICE_ABC_TOKEN", "GITHUB_TOKEN"):
            st_f, _ = self.srv.req(
                "POST", "/v1/providers/secret",
                {"key_env": forbidden, "value": "nope"},
                headers=admin,
            )
            self.assertEqual(st_f, 403, forbidden)

        st_bad, _ = self.srv.req(
            "POST", "/v1/providers/secret",
            {"key_env": "RANDOM_UNRELATED_KEY", "value": "x"},
            headers=admin,
        )
        self.assertEqual(st_bad, 400)

        st_ok, res_ok = self.srv.req(
            "POST", "/v1/providers/secret",
            {"key_env": "OPENROUTER_API_KEY", "value": "sk-or-secret-xyz"},
            headers=admin,
        )
        self.assertEqual(st_ok, 200)
        self.assertEqual(res_ok, {"ok": True, "key_env": "OPENROUTER_API_KEY", "configured": True})
        self.assertNotIn("sk-or-secret-xyz", json.dumps(res_ok))
        self.assertEqual(resolve_secret("OPENROUTER_API_KEY", scope="host"), "sk-or-secret-xyz")

        # RPC channel denylist blocks both routes.
        self.assertTrue(_rpc_denied("GET", "/v1/host"))
        self.assertTrue(_rpc_denied("PATCH", "/v1/host"))
        self.assertTrue(_rpc_denied("POST", "/v1/providers/secret"))

        # Custom provider with custom key_env can have its secret configured and shows key_configured=True.
        st_cp, _ = self.srv.req(
            "POST", "/v1/providers",
            {"name": "custom.local", "url": "http://127.0.0.1:9999/v1", "key_env": "CUSTOM_LOCAL_KEY", "model": "m1"},
            headers=admin,
        )
        self.assertEqual(st_cp, 200)
        st_ls1, ls1 = self.srv.req("GET", "/v1/providers", headers=admin)
        self.assertEqual(st_ls1, 200)
        row1 = next(r for r in ls1["providers"] if r["name"] == "custom.local")
        self.assertFalse(row1["key_configured"])

        st_cs, _ = self.srv.req(
            "POST", "/v1/providers/secret",
            {"key_env": "CUSTOM_LOCAL_KEY", "value": "custom-tok-123"},
            headers=admin,
        )
        self.assertEqual(st_cs, 200)
        st_ls2, ls2 = self.srv.req("GET", "/v1/providers", headers=admin)
        self.assertEqual(st_ls2, 200)
        row2 = next(r for r in ls2["providers"] if r["name"] == "custom.local")
        self.assertTrue(row2["key_configured"])
        self.assertNotIn("custom-tok-123", json.dumps(ls2))
