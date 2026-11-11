"""Workspace registry, per-turn env, and HTTP routes."""

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
from agents_relay.turn import run_loop_turn, workspace_env
from agents_relay.workspaces import create_workspace, list_workspaces
from agents_workspace import Workspace


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
    def __init__(self, cfg: RelayConfig) -> None:
        self.server = serve_http(cfg)
        self.host, self.port = self.server.server_address  # type: ignore[misc]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, body: dict | None = None):
        conn = HTTPConnection(self.host, self.port, timeout=10)
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        headers["X-Relay-Secret"] = "expected"
        conn.request(method, path, body=payload, headers=headers)
        return conn.getresponse()


class TestWorkspaceEnv(unittest.TestCase):
    def test_empty_ref_inherits(self):
        self.assertIsNone(workspace_env(""))

    def test_unknown_ref_raises(self):
        with self.assertRaises(ValueError):
            workspace_env("does-not-exist")

    def test_env_sets_root_and_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENTS_WORKSPACES_DIR": tmp}, clear=False):
                ws = Workspace.create("demo")
                env = workspace_env("demo")
            self.assertIsNotNone(env)
            assert env is not None
            self.assertEqual(env["AGENTS_WORKSPACE"], "demo")
            self.assertEqual(env["AGENTS_HOST_DIR"], str(ws.host.dir))
            self.assertEqual(env["AGENTS_MEMORY_DIR"], str(ws.memory))
            self.assertEqual(env["AGENTS_MEMORY_PATH"], str(ws.memory))
            self.assertEqual(env["AGENTS_TRACES_DIR"], str(ws.traces))
            self.assertNotIn("AGENTS_WORKSPACE_ROOT", env)

    def test_turn_passes_workspace_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENTS_WORKSPACES_DIR": tmp}, clear=False):
                ws = Workspace.create("demo")
                captured: dict = {}

                def fake_run(argv, **kwargs):
                    captured["env"] = kwargs.get("env")

                    class Proc:
                        returncode = 0
                        stdout = ""
                        stderr = ""

                    return Proc()

                with patch("agents_relay.turn.subprocess.run", fake_run):
                    run_loop_turn(channel="http", user="u", message="hi", workspace="demo")

            self.assertEqual(captured["env"]["AGENTS_WORKSPACE"], "demo")
            self.assertEqual(captured["env"]["AGENTS_HOST_DIR"], str(ws.host.dir))


class TestTurnWorkspaceHttp(unittest.TestCase):
    """Per-turn workspace ids and attachment isolation."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {"AGENTS_WORKSPACES_DIR": self.dir.name},
            clear=False,
        )
        self.env.start()
        self.ws_a = Workspace.create("a")
        self.ws_b = Workspace.create("b")
        self.srv = _Server(_cfg())

    def tearDown(self):
        self.srv.close()
        self.env.stop()
        self.dir.cleanup()

    def _turn(self, **fields):
        return self.srv.request("POST", "/v1/turn", {"text": "hi", **fields})

    def test_traversal_workspace_400(self):
        resp = self._turn(workspace="../x")
        self.assertEqual(resp.status, 400)

    def test_path_workspace_400(self):
        resp = self._turn(workspace="/tmp/x")
        self.assertEqual(resp.status, 400)

    def test_label_workspace_400(self):
        resp = self._turn(workspace="My Workspace")
        self.assertEqual(resp.status, 400)

    def test_unknown_workspace_400(self):
        resp = self._turn(workspace="nope-nothere")
        self.assertEqual(resp.status, 400)

    def test_persona_path_400(self):
        resp = self._turn(persona="../evil")
        self.assertEqual(resp.status, 400)

    def test_stream_rejects_bad_workspace(self):
        resp = self.srv.request("POST", "/v1/stream", {"text": "hi", "workspace": "../x"})
        self.assertEqual(resp.status, 400)

    def test_attachment_lands_in_turn_workspace(self):
        from types import SimpleNamespace

        def fake_dl(url, dest_dir, *, mime=""):
            dest_dir.mkdir(parents=True, exist_ok=True)
            path = dest_dir / "dl.bin"
            path.write_bytes(b"x")
            return path

        result = SimpleNamespace(
            reply="ok", session="", user_id="", alias="", returncode=0, stderr=""
        )
        with patch("agents_relay.attachments.download_http", fake_dl), patch(
            "agents_relay.http_adapter.run_loop_turn", return_value=result
        ):
            resp = self._turn(
                workspace="b",
                attachments=[{"url": "https://example.test/x.bin"}],
            )
        self.assertEqual(resp.status, 200)
        b_inbox = self.ws_b.relay_state / "inbox"
        a_inbox = self.ws_a.relay_state / "inbox"
        self.assertTrue((b_inbox / "dl.bin").is_file())
        self.assertFalse((a_inbox / "dl.bin").exists())

    def test_workdir_field_sets_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENTS_WORKSPACES_DIR": tmp}, clear=False):
                Workspace.create("demo")
                captured: dict = {}

                def fake_run(argv, **kwargs):
                    captured["env"] = kwargs.get("env")

                    class Proc:
                        returncode = 0
                        stdout = ""
                        stderr = ""

                    return Proc()

                from agents_relay.turn import run_loop_turn

                with patch("agents_relay.turn.subprocess.run", fake_run):
                    run_loop_turn(
                        channel="http", user="u", message="hi",
                        workspace="demo", workdir="/work/repo",
                    )
            self.assertEqual(
                Path(captured["env"]["AGENTS_WORKDIR"]).resolve(),
                Path("/work/repo").resolve(),
            )


class TestWorkspaceRoutes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {"AGENTS_WORKSPACES_DIR": self.dir.name},
            clear=False,
        )
        self.env.start()
        self.srv = _Server(_cfg())

    def tearDown(self):
        self.srv.close()
        self.env.stop()
        self.dir.cleanup()

    def test_get_lists_default(self):
        resp = self.srv.request("GET", "/v1/workspaces")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode())
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(len(data["workspaces"]), 1)

    def test_post_creates_workspace(self):
        resp = self.srv.request("POST", "/v1/workspaces", {"label": "demo"})
        self.assertEqual(resp.status, 201)
        data = json.loads(resp.read().decode())
        self.assertEqual(data["workspace"]["label"], "demo")
        self.assertTrue((Path(data["workspace"]["root"]) / "workspace.json").is_file())

    def test_post_requires_label(self):
        resp = self.srv.request("POST", "/v1/workspaces", {})
        self.assertEqual(resp.status, 400)

    def test_requires_secret(self):
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("GET", "/v1/workspaces")
        self.assertEqual(conn.getresponse().status, 401)


if __name__ == "__main__":
    unittest.main()
