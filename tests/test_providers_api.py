"""Provider write API: POST/DELETE/probe /v1/providers, session picks, favorites."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_workspace import Host


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
    )
    base.update(overrides)
    return RelayConfig(**base)


class _Server:
    def __init__(self, cfg: RelayConfig, **kw) -> None:
        self.server = serve_http(cfg, **kw)
        self.host, self.port = self.server.server_address  # type: ignore[misc]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, body: dict | None = None):
        conn = HTTPConnection(self.host, self.port, timeout=10)
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json", "X-Relay-Secret": "expected"}
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        raw = res.read() or b"{}"
        try:
            return res.status, json.loads(raw)
        except json.JSONDecodeError:
            return res.status, {"error": raw.decode("utf-8", "replace")}


class TestProviderWriteApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.host = Host(dir=Path(self.tmp.name))
        self.ws = self.host.create("qa", env=os.environ)
        self.srv = _Server(_cfg(state_dir=str(Path(self.tmp.name) / "relay")))

    def tearDown(self) -> None:
        self.srv.close()

    def _row(self, rows, name):
        return next((r for r in rows if r.get("name") == name), None)

    def test_list_rows_carry_source(self):
        status, data = self.srv.request("GET", "/v1/providers")
        self.assertEqual(status, 200)
        echo = self._row(data["providers"], "echo")
        self.assertIsNotNone(echo)
        self.assertEqual(echo["source"], "env")
        self.assertFalse(echo["configurable"])

    def test_create_file_provider_then_delete(self):
        status, data = self.srv.request(
            "POST",
            "/v1/providers",
            {"name": "lms.local", "url": "http://127.0.0.1:1234/v1", "model": "qwen"},
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["provider"]["source"], "file")
        self.assertTrue(
            (Path(self.tmp.name) / "providers" / "lms.local.json").is_file()
        )
        status, data = self.srv.request("GET", "/v1/providers")
        row = self._row(data["providers"], "lms.local")
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "file")
        self.assertTrue(row["configurable"])
        status, data = self.srv.request("DELETE", "/v1/providers/lms.local")
        self.assertEqual(status, 200, data)
        status, data = self.srv.request("GET", "/v1/providers")
        self.assertIsNone(self._row(data["providers"], "lms.local"))

    def test_create_acp_agent_with_command(self):
        status, data = self.srv.request(
            "POST",
            "/v1/providers",
            {"name": "acp.devin", "kind": "acp-agent", "command": "devin acp"},
        )
        self.assertEqual(status, 200, data)
        status, data = self.srv.request("GET", "/v1/providers")
        row = self._row(data["providers"], "acp.devin")
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "acp-agent")
        self.assertEqual(row["command"], "devin acp")
        self.assertEqual(row["source"], "file")
        # Manifest command wins over env.
        from agents_relay.acp import acp_agent_cmd

        self.assertEqual(acp_agent_cmd("acp.devin"), "devin acp")

    def test_acp_agent_requires_command(self):
        status, data = self.srv.request(
            "POST", "/v1/providers", {"name": "acp.nada", "kind": "acp-agent"}
        )
        self.assertEqual(status, 400)

    def test_acp_agent_requires_acp_prefix(self):
        status, data = self.srv.request(
            "POST",
            "/v1/providers",
            {"name": "notacp.x", "kind": "acp-agent", "command": "devin acp"},
        )
        self.assertEqual(status, 400)

    def test_env_provider_shadowable_but_not_deletable(self):
        # POST shadows the package row with a file manifest (edit path);
        # DELETE still refuses — env rows return once the file is gone.
        status, _ = self.srv.request(
            "POST", "/v1/providers", {"name": "echo", "kind": "echo", "url_env": "ECHO_URL_X"}
        )
        self.assertEqual(status, 200)
        status, data = self.srv.request("GET", "/v1/providers")
        self.assertEqual(self._row(data["providers"], "echo")["source"], "file")
        status, _ = self.srv.request("DELETE", "/v1/providers/echo")
        self.assertEqual(status, 200)
        status, data = self.srv.request("GET", "/v1/providers")
        self.assertEqual(self._row(data["providers"], "echo")["source"], "env")
        status, _ = self.srv.request("DELETE", "/v1/providers/echo")
        self.assertEqual(status, 409)

    def test_env_acp_row_overridable_by_file_manifest(self):
        with patch.dict(
            os.environ, {"AGENTS_ACP_CMD_CURSOR": "cursor agent --acp"}, clear=False
        ):
            from agents_relay.acp import ensure_acp_manifest

            ensure_acp_manifest()  # env drop — read-only source=env row
            status, data = self.srv.request("GET", "/v1/providers")
            row = self._row(data["providers"], "acp.cursor")
            self.assertEqual(row["source"], "env")
            status, _ = self.srv.request(
                "POST",
                "/v1/providers",
                {"name": "acp.cursor", "kind": "acp-agent", "command": "ssh box cursor --acp"},
            )
            self.assertEqual(status, 200)
            from agents_relay.acp import acp_agent_cmd

            self.assertEqual(acp_agent_cmd("acp.cursor"), "ssh box cursor --acp")
            status, data = self.srv.request("GET", "/v1/providers")
            self.assertEqual(self._row(data["providers"], "acp.cursor")["source"], "file")
            # Deleting the override restores the env row.
            status, _ = self.srv.request("DELETE", "/v1/providers/acp.cursor")
            self.assertEqual(status, 200)
            self.assertEqual(acp_agent_cmd("acp.cursor"), "cursor agent --acp")

    def test_delete_unknown_404(self):
        status, _ = self.srv.request("DELETE", "/v1/providers/nope.nothing")
        self.assertEqual(status, 404)

    def test_probe_echo_and_missing_url(self):
        status, data = self.srv.request("GET", "/v1/providers/echo/probe")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        self.srv.request(
            "POST",
            "/v1/providers",
            {"name": "unreach.local", "url_env": "NOPE_URL_UNSET", "key_env": ""},
        )
        status, data = self.srv.request("GET", "/v1/providers/unreach.local/probe")
        self.assertEqual(data["status"], "missing-url")
        status, _ = self.srv.request("GET", "/v1/providers/nope.nothing/probe")
        self.assertEqual(status, 404)


class TestSessionPicksAndFavorites(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.host = Host(dir=Path(self.tmp.name))
        self.ws = self.host.create("qa", env=os.environ)
        self.srv = _Server(_cfg(state_dir=str(Path(self.tmp.name) / "relay")))

    def tearDown(self) -> None:
        self.srv.close()

    def test_session_pick_wins_and_ws_keeps_last_used(self):
        status, data = self.srv.request(
            "POST",
            "/v1/model",
            {"provider": "echo", "model": "m1", "workspace": "qa", "session": "ses_a"},
        )
        self.assertEqual(status, 200, data)
        # Session sees its own pick; bare ws GET sees the ws last-used.
        status, data = self.srv.request("GET", "/v1/model?workspace=qa&session=ses_a")
        self.assertEqual(data["provider"], "echo")
        self.assertEqual(data["model"], "m1")
        status, data = self.srv.request("GET", "/v1/model?workspace=qa")
        self.assertEqual(data["provider"], "echo")
        # A different session still inherits the ws last-used pick.
        status, data = self.srv.request("GET", "/v1/model?workspace=qa&session=ses_b")
        self.assertEqual(data["provider"], "echo")
        self.assertEqual(data["model"], "m1")
        # Pick file lives in the host clients dir.
        picks = json.loads(
            (Path(self.tmp.name) / "clients" / "sessions.json").read_text("utf-8")
        )
        self.assertEqual(picks["ses_a"]["provider"], "echo")
        # Clear with a session drops only the session override — the ws
        # last-used pick is untouched and shines through again.
        status, data = self.srv.request(
            "POST", "/v1/model", {"clear": True, "session": "ses_a"}
        )
        self.assertEqual(status, 200)
        status, data = self.srv.request("GET", "/v1/model?workspace=qa&session=ses_a")
        self.assertEqual(data["model"], "m1")
        status, data = self.srv.request("GET", "/v1/model?workspace=qa")
        self.assertEqual(data["provider"], "echo")

    def test_session_pick_reaches_turn_resolution(self):
        from agents_relay.providers_api import set_session_pick
        from agents_relay.turn import TurnParams, _resolve_provider_and_model

        set_session_pick("ses_x", "echo", "picked-model")
        params = TurnParams(
            channel="web", user="u", message="hi", session="ses_x",
            provider="", model="", persona="", project="", workspace="qa",
        )
        provider, model = _resolve_provider_and_model(params, _cfg())
        self.assertEqual((provider, model), ("echo", "picked-model"))

    def test_favorites_toggle_and_replace(self):
        status, data = self.srv.request("GET", "/v1/favorites")
        self.assertEqual(data["favorites"], [])
        status, data = self.srv.request(
            "POST",
            "/v1/favorites",
            {"provider": "acp.devin", "model": "opus", "star": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["favorites"], [{"provider": "acp.devin", "model": "opus"}])
        status, data = self.srv.request(
            "POST",
            "/v1/favorites",
            {"favorites": [{"provider": "echo", "model": "m1"}]},
        )
        self.assertEqual(data["favorites"], [{"provider": "echo", "model": "m1"}])
        status, data = self.srv.request("GET", "/v1/favorites")
        self.assertEqual(data["favorites"], [{"provider": "echo", "model": "m1"}])
        status, data = self.srv.request(
            "POST", "/v1/favorites", {"provider": "echo", "model": "m1", "star": False}
        )
        self.assertEqual(data["favorites"], [])


if __name__ == "__main__":
    unittest.main()
