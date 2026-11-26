"""Tests for U02 rich approval preview carriage & remember option propagation."""
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
from agents_relay.approvals import (
    create_pending,
    decide_approval,
    mint_approval_token,
    read_approval,
    record_decision,
    run_http_approve,
    run_local_approve,
    sanitize_preview,
)
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
        self.host, self.port = self.server.server_address  # type: ignore[misc]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        from agents_relay import events

        events.reset_bus()

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


class TestApprovalPreviewAndRemember(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.host_dir = Path(self.td.name)
        Host(self.host_dir).create("default", "default")
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": str(self.host_dir)})
        self.env.start()
        self.cfg = _cfg()
        self.srv = _Srv(self.cfg)

    def tearDown(self) -> None:
        self.srv.close()
        self.env.stop()
        self.td.cleanup()

    def test_sanitize_preview_caps_oversized_and_rejects_bad_kinds(self) -> None:
        self.assertIsNone(sanitize_preview("bad"))
        self.assertIsNone(sanitize_preview({"kind": "script", "html": "<script>"}))
        huge = sanitize_preview({
            "kind": "diff",
            "path": "a.py",
            "old_text": "x" * 100_000,
            "new_text": "y" * 100_000,
            "unified": "z" * 100_000,
        })
        self.assertIsNotNone(huge)
        assert huge is not None
        self.assertLessEqual(len(json.dumps(huge).encode("utf-8")), 65_536)

    def test_preview_roundtrip_hash_guard_and_remember_via_http(self) -> None:
        admin = {"Authorization": "Bearer expected"}
        tok, _ = mint_approval_token("default")
        turn_auth = {"Authorization": f"Bearer {tok}"}

        req_body = {
            "id": "ap_u02_1",
            "workspace": "default",
            "tool": "mcp.terminal",
            "args": {"argv": ["write", "app.py", "--content", "print(1)"]},
            "summary": "write app.py",
            "preview": {
                "kind": "diff",
                "path": "app.py",
                "old_text": "",
                "new_text": "print(1)",
                "unified": "+print(1)\n",
                "truncated": False,
            },
        }
        st, res = self.srv.req("POST", "/v1/approvals/request", req_body, headers=turn_auth)
        self.assertEqual(st, 200)
        self.assertEqual(res["status"], "pending")

        # Changing args still fails closed with 409 (hash stays strict on tool + args).
        bad_replay = dict(req_body, args={"argv": ["run", "--", "whoami"]})
        st_409, _ = self.srv.req("POST", "/v1/approvals/request", bad_replay, headers=turn_auth)
        self.assertEqual(st_409, 409)

        # GET /v1/approvals includes sanitized preview.
        st_g, res_g = self.srv.req("GET", "/v1/approvals?workspace=default", headers=admin)
        self.assertEqual(st_g, 200)
        self.assertEqual(len(res_g["approvals"]), 1)
        self.assertEqual(res_g["approvals"][0]["preview"]["path"], "app.py")

        # role=host device token cannot decide approvals.
        host_dev = devices.mint_device(label="peer-host", role="host")
        st_f, _ = self.srv.req(
            "POST",
            "/v1/approvals/ap_u02_1",
            {"allow": True, "remember": True},
            headers={"Authorization": f"Bearer {host_dev['device_token']}", "X-Device-Id": host_dev["device_id"]},
        )
        self.assertEqual(st_f, 403)

        # role=client decides with allow=True, remember=True.
        client_dev = devices.mint_device(label="mobile-client", role="client")
        st_d, res_d = self.srv.req(
            "POST",
            "/v1/approvals/ap_u02_1",
            {"allow": True, "remember": True},
            headers={"Authorization": f"Bearer {client_dev['device_token']}", "X-Device-Id": client_dev["device_id"]},
        )
        self.assertEqual(st_d, 200)
        self.assertTrue(res_d["remember"])

        st_w, res_w = self.srv.req("GET", "/v1/approvals/ap_u02_1/wait?timeout=0", headers=turn_auth)
        self.assertEqual(st_w, 200)
        self.assertEqual(res_w["status"], "approved")
        self.assertTrue(res_w.get("remember"))

        # run_http_approve retrieves remember option on re-wait.
        opts: dict = {}
        code, _ = run_http_approve(
            f"http://{self.srv.host}:{self.srv.port}",
            tok,
            req_body,
            timeout=1.0,
            workspace="default",
            out_opts=opts,
        )
        self.assertEqual(code, 0)
        self.assertTrue(opts.get("remember"))

    def test_deny_and_mirror_never_persist_remember(self) -> None:
        admin = {"Authorization": "Bearer expected"}
        tok, _ = mint_approval_token("default")
        turn_auth = {"Authorization": f"Bearer {tok}"}

        self.srv.req(
            "POST",
            "/v1/approvals/request",
            {"id": "ap_deny_1", "workspace": "default", "tool": "mcp.terminal", "args": {"a": 1}, "summary": "x"},
            headers=turn_auth,
        )
        st, res = self.srv.req(
            "POST",
            "/v1/approvals/ap_deny_1",
            {"allow": False, "remember": True},
            headers=admin,
        )
        self.assertEqual(st, 200)
        self.assertFalse(res["remember"])
        rec = read_approval(self.cfg, "ap_deny_1") or {}
        self.assertNotIn("remember", rec)

        create_pending(self.cfg, 0, {"id": "ap_mir_1", "tool": "t", "args": {}}, workspace="default", from_mirror=True)
        record_decision(
            self.cfg,
            "ap_mir_1",
            status="approved",
            note="approved",
            decided_by=None,
            from_mirror=True,
            remember=True,
            allow_pattern="mcp.terminal *",
        )
        mir_rec = read_approval(self.cfg, "ap_mir_1") or {}
        self.assertNotIn("remember", mir_rec)
        self.assertNotIn("allow_pattern", mir_rec)

    def test_local_approve_out_opts_collects_remember(self) -> None:
        create_pending(self.cfg, 0, {"id": "ap_loc_1", "tool": "mcp.terminal", "args": {"a": 1}}, workspace="default")
        decide_approval(self.cfg, "ap_loc_1", allow=True, remember=True)
        opts: dict = {}
        code, _ = run_local_approve(self.cfg, {"id": "ap_loc_1", "tool": "mcp.terminal", "args": {"a": 1}}, 1.0, out_opts=opts)
        self.assertEqual(code, 0)
        self.assertTrue(opts.get("remember"))
