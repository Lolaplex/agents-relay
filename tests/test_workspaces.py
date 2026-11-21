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
    def __init__(self, cfg: RelayConfig, on_turn=None) -> None:
        self.server = serve_http(cfg, on_turn=on_turn)
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

    def test_select_prefers_agents_workspace_env(self):
        from agents_relay.workspaces import select_workspace

        with tempfile.TemporaryDirectory() as tmp:
            env = {"AGENTS_WORKSPACES_DIR": tmp}
            with patch.dict(os.environ, env, clear=False):
                Workspace.create("a")
                Workspace.create("b")
                with patch.dict(os.environ, {"AGENTS_WORKSPACE": "b"}, clear=False):
                    self.assertEqual(select_workspace().id, "b")

    def test_delete_route_archives_and_guards(self):
        import urllib.error

        with tempfile.TemporaryDirectory() as tmp:
            env = {"AGENTS_WORKSPACES_DIR": tmp,
                   "AGENTS_WORKSPACE": "demo"}
            with patch.dict(os.environ, env, clear=False):
                Workspace.create("demo")
                Workspace.create("lab")
                srv = _Server(_cfg())
                self.addCleanup(srv.close)
                # secret required
                try:
                    conn = HTTPConnection(srv.host, srv.port, timeout=10)
                    conn.request("DELETE", "/v1/workspaces/lab")
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 401)
                    resp.read()
                finally:
                    conn.close()
                # selected workspace refused
                resp = srv.request("DELETE", "/v1/workspaces/demo")
                self.assertEqual(resp.status, 409)
                resp.read()
                # archive lab
                resp = srv.request("DELETE", "/v1/workspaces/lab")
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                self.assertTrue(data["ok"])
                self.assertIn(".trash", data["archived"])
                self.assertFalse((Path(tmp) / "lab").exists())
                # unknown -> 400
                resp = srv.request("DELETE", "/v1/workspaces/lab")
                self.assertEqual(resp.status, 400)
                resp.read()
                # traversal -> 400
                resp = srv.request("DELETE", "/v1/workspaces/..%2Fevil")
                self.assertEqual(resp.status, 400)
                resp.read()

    def test_select_ambiguous_without_env(self):
        from agents_relay.workspaces import select_workspace

        with tempfile.TemporaryDirectory() as tmp:
            env = {"AGENTS_WORKSPACES_DIR": tmp}
            with patch.dict(os.environ, env, clear=False):
                os.environ.pop("AGENTS_WORKSPACE", None)
                Workspace.create("a")
                Workspace.create("b")
                with self.assertRaises(ValueError):
                    select_workspace()


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

    def test_post_creates_workspace_by_id(self):
        resp = self.srv.request(
            "POST", "/v1/workspaces", {"id": "proj-b", "label": "Proj B"})
        self.assertEqual(resp.status, 201)
        data = json.loads(resp.read().decode())
        self.assertEqual(data["workspace"]["id"], "proj-b")
        # first persona 'default' created with the workspace
        self.assertTrue((Path(data["workspace"]["root"]) / "personas"
                         / "default" / "persona.json").is_file())

    def test_get_personas_workspace_scoped(self):
        create_workspace("demo")
        resp = self.srv.request("GET", "/v1/personas?workspace=demo")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode())
        self.assertEqual(data["workspace"], "demo")
        ids = {p["id"] for p in data["personas"]}
        self.assertIn("default", ids)

    def test_get_personas_bad_workspace_400(self):
        resp = self.srv.request("GET", "/v1/personas?workspace=nope")
        self.assertEqual(resp.status, 400)

    def test_session_roundtrip(self):
        create_workspace("demo")
        base = "/v1/workspaces/demo/personas/cos"
        # create the persona manifest explicitly (equal persona, not default)
        resp = self.srv.request(
            "POST", "/v1/workspaces/demo/personas", {"id": "cos"})
        self.assertEqual(resp.status, 201)
        # mint a session under it
        resp = self.srv.request(
            "POST", base + "/sessions", {"user": "me", "channel": "http"})
        self.assertEqual(resp.status, 201)
        data = json.loads(resp.read().decode())
        ses = data["session"]
        self.assertEqual(ses["persona"], "cos")
        self.assertEqual(ses["workspace"], "demo")
        self.assertTrue(ses["id"].startswith("ses_"))
        sid = ses["id"]
        # list sessions under the persona
        resp = self.srv.request("GET", base + "/sessions")
        self.assertEqual(resp.status, 200)
        rows = json.loads(resp.read().decode())["sessions"]
        self.assertEqual([r["id"] for r in rows], [sid])
        # fetch the single session (record + events key)
        resp = self.srv.request("GET", base + f"/sessions/{sid}")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode())
        self.assertEqual(data["session"]["id"], sid)
        self.assertIn("events", data)
        # a second persona's session list stays separate
        resp = self.srv.request("GET", "/v1/workspaces/demo/personas/ops/sessions")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.read().decode())["sessions"], [])

    def test_session_routes_reject_traversal(self):
        create_workspace("demo")
        resp = self.srv.request(
            "GET", "/v1/workspaces/demo/personas/..%2Fevil/sessions")
        self.assertIn(resp.status, (400, 404))

    def test_inbox_handoff_roundtrip(self):
        create_workspace("demo")
        resp = self.srv.request(
            "POST", "/v1/workspaces/demo/personas", {"id": "cos"})
        self.assertEqual(resp.status, 201)
        # task lands in default's inbox
        resp = self.srv.request(
            "POST", "/v1/workspaces/demo/personas/default/inbox",
            {"title": "write report", "user": "me"})
        self.assertEqual(resp.status, 201)
        task = json.loads(resp.read().decode())["task"]
        self.assertEqual(task["owner"], "default")
        tid = task["id"]
        # handoff default -> cos: owner moves, cos inbox lists it
        resp = self.srv.request(
            "POST", f"/v1/workspaces/demo/personas/default/inbox/{tid}/handoff",
            {"to": "cos", "note": "yours"})
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode())
        self.assertEqual(data["task"]["owner"], "cos")
        resp = self.srv.request(
            "GET", "/v1/workspaces/demo/personas/cos/inbox")
        self.assertEqual(resp.status, 200)
        rows = json.loads(resp.read().decode())["inbox"]
        self.assertEqual([t["id"] for t in rows], [tid])
        # default inbox now empty; replaying the same handoff 404s
        resp = self.srv.request(
            "GET", "/v1/workspaces/demo/personas/default/inbox")
        self.assertEqual(json.loads(resp.read().decode())["inbox"], [])
        resp = self.srv.request(
            "POST", f"/v1/workspaces/demo/personas/default/inbox/{tid}/handoff",
            {"to": "ops"})
        self.assertEqual(resp.status, 400)

    def test_inbox_requires_title(self):
        create_workspace("demo")
        resp = self.srv.request(
            "POST", "/v1/workspaces/demo/personas/default/inbox", {})
        self.assertEqual(resp.status, 400)


class TestHandoffWake(unittest.TestCase):
    """POST .../handoff?wake=1 runs the task as a detached turn (#18)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {"AGENTS_WORKSPACES_DIR": self.dir.name},
            clear=False,
        )
        self.env.start()
        self.calls: list[dict] = []

        def hook(**kwargs):
            from agents_relay.turn import LoopTurnResult

            self.calls.append(kwargs)
            return LoopTurnResult(
                reply="wake reply", session=str(kwargs.get("session") or ""),
                user_id="", alias="", returncode=0, stderr="",
            )

        self.srv = _Server(_cfg(), on_turn=hook)

    def tearDown(self):
        from agents_relay import wake

        wake.drain()
        self.srv.close()
        self.env.stop()
        self.dir.cleanup()

    def _mk_task(self, persona: str = "default") -> str:
        resp = self.srv.request(
            "POST", f"/v1/workspaces/demo/personas/{persona}/inbox",
            {"title": "summarize logs", "note": "do it", "user": "me"})
        self.assertEqual(resp.status, 201)
        return json.loads(resp.read().decode())["task"]["id"]

    def _wait_task(self, persona: str, tid: str, status: str = "done"):
        import time as _time

        from agents_workspace.inbox import find_task
        from agents_workspace import Host

        deadline = _time.monotonic() + 5
        ws = Host.from_env().workspace("demo")
        while _time.monotonic() < deadline:
            hit = find_task(ws, tid)
            if hit and hit[1].get("status") == status:
                return hit[0], hit[1]
            _time.sleep(0.05)
        self.fail(f"task {tid} never reached {status}")

    def test_wake_runs_detached_turn(self):
        create_workspace("demo")
        self.srv.request(
            "POST", "/v1/workspaces/demo/personas", {"id": "cos"})
        tid = self._mk_task()
        resp = self.srv.request(
            "POST",
            f"/v1/workspaces/demo/personas/default/inbox/{tid}/handoff?wake=1",
            {"to": "cos"})
        self.assertEqual(resp.status, 202)
        data = json.loads(resp.read().decode())
        self.assertTrue(data["wake"])
        self.assertEqual(data["task"]["owner"], "cos")
        owner, task = self._wait_task("cos", tid)
        self.assertEqual(owner, "cos")
        self.assertEqual(task["result"], "wake reply")
        self.assertEqual(task["reply_summary"], "wake reply")
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["persona"], "cos")
        self.assertEqual(call["workspace"], "demo")
        self.assertEqual(call["session"], f"routine:handoff:{tid}")
        self.assertEqual(call["channel"], "handoff")
        env = call["extra_env"]
        self.assertEqual(env["AGENTS_PERSONA"], "cos")
        self.assertEqual(env["AGENTS_PERSONA_HOP"], "1")

    def test_wake_honors_target_peer_policy(self):
        create_workspace("demo")
        self.srv.request(
            "POST", "/v1/workspaces/demo/personas",
            {"id": "cos", "accepts_messages": False})
        tid = self._mk_task()
        resp = self.srv.request(
            "POST",
            f"/v1/workspaces/demo/personas/default/inbox/{tid}/handoff?wake=1",
            {"to": "cos"})
        self.assertEqual(resp.status, 400)
        resp.read()
        self.assertEqual(self.calls, [])

    def test_wake_marks_failed_on_turn_error(self):
        create_workspace("demo")
        self.srv.request(
            "POST", "/v1/workspaces/demo/personas", {"id": "cos"})
        self.srv.close()

        def bad(**kwargs):
            from agents_relay.turn import LoopTurnResult

            return LoopTurnResult(
                reply="", session="", user_id="", alias="",
                returncode=1, stderr="boom",
            )

        self.srv = _Server(_cfg(), on_turn=bad)
        tid = self._mk_task()
        resp = self.srv.request(
            "POST",
            f"/v1/workspaces/demo/personas/default/inbox/{tid}/handoff?wake=1",
            {"to": "cos"})
        self.assertEqual(resp.status, 202)
        resp.read()
        owner, task = self._wait_task("cos", tid, status="failed")
        self.assertEqual(owner, "cos")
        self.assertIn("exit 1", task["result"])

    def _handoff_no_wake(self, tid: str, target: str = "cos") -> None:
        resp = self.srv.request(
            "POST",
            f"/v1/workspaces/demo/personas/default/inbox/{tid}/handoff",
            {"to": target})
        self.assertEqual(resp.status, 200)
        resp.read()

    def _stranded(self, tid: str):
        """Crash state: queue entry + task stuck at running (#93)."""
        from agents_relay import wake
        from agents_workspace import Host
        from agents_workspace.inbox import find_task

        ws = Host.from_env().workspace("demo")
        _owner, task = find_task(ws, tid)
        wake._enqueue(ws, task)
        wake._write_status(ws, tid, "running", note="crash")
        return ws

    def _restart(self, calls: list) -> None:
        from agents_relay.turn import LoopTurnResult

        def hook(**kwargs):
            calls.append(kwargs)
            return LoopTurnResult(
                reply="replayed", session=str(kwargs.get("session") or ""),
                user_id="", alias="", returncode=0, stderr="")

        self.srv.close()
        self.srv = _Server(_cfg(), on_turn=hook)

    def test_wake_queue_cleared_on_finish(self):
        create_workspace("demo")
        self.srv.request(
            "POST", "/v1/workspaces/demo/personas", {"id": "cos"})
        tid = self._mk_task()
        resp = self.srv.request(
            "POST",
            f"/v1/workspaces/demo/personas/default/inbox/{tid}/handoff?wake=1",
            {"to": "cos"})
        self.assertEqual(resp.status, 202)
        resp.read()
        self._wait_task("cos", tid)
        from agents_workspace import Host

        qdir = Host.from_env().workspace("demo").relay_state / "handoffs"
        self.assertEqual(list(qdir.glob("*.json")), [])

    def test_boot_replays_stranded_handoff(self):
        create_workspace("demo")
        self.srv.request(
            "POST", "/v1/workspaces/demo/personas", {"id": "cos"})
        tid = self._mk_task()
        self._handoff_no_wake(tid)
        ws = self._stranded(tid)
        calls: list[dict] = []
        self._restart(calls)
        owner, task = self._wait_task("cos", tid)
        self.assertEqual(task["result"], "replayed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["session"], f"routine:handoff:{tid}")
        self.assertEqual(calls[0]["persona"], "cos")
        self.assertEqual(
            list((ws.relay_state / "handoffs").glob("*.json")), [])

    def test_replay_honors_post_move_policy(self):
        create_workspace("demo")
        self.srv.request(
            "POST", "/v1/workspaces/demo/personas", {"id": "cos"})
        tid = self._mk_task()
        self._handoff_no_wake(tid)
        ws = self._stranded(tid)
        # Policy tightened after the move: replay must refuse, not run.
        manifest = ws.personas / "cos" / "persona.json"
        meta = json.loads(manifest.read_text(encoding="utf-8"))
        meta["accepts_messages"] = False
        manifest.write_text(json.dumps(meta), encoding="utf-8")
        calls: list[dict] = []
        self._restart(calls)
        _owner, task = self._wait_task("cos", tid, status="failed")
        self.assertIn("does not accept", str(task.get("events")[-1].get("note")))
        self.assertEqual(calls, [])
        self.assertEqual(
            list((ws.relay_state / "handoffs").glob("*.json")), [])

    def test_replay_drops_terminal_and_missing_tasks(self):
        create_workspace("demo")
        self.srv.request(
            "POST", "/v1/workspaces/demo/personas", {"id": "cos"})
        tid = self._mk_task()
        ws = self._stranded(tid)
        # terminal: finished before the crash window closed -> drop, no wake
        from agents_relay import wake

        wake._write_status(ws, tid, "done", result="x")
        # missing: dangling entry for a task that no longer exists
        wake._enqueue(ws, {"id": "ghost1", "owner": "cos"})
        calls: list[dict] = []
        self._restart(calls)
        self.assertEqual(calls, [])
        self.assertEqual(
            list((ws.relay_state / "handoffs").glob("*.json")), [])

    def test_handler_error_returns_500_not_disconnect(self):
        """A crashing handler must answer 500 JSON, not drop the socket."""
        create_workspace("demo")
        self.srv.request(
            "POST", "/v1/workspaces/demo/personas", {"id": "cos"})
        tid = self._mk_task()
        with patch("agents_relay.wake.wake_task",
                   side_effect=RuntimeError("boom")):
            resp = self.srv.request(
                "POST",
                f"/v1/workspaces/demo/personas/default/inbox/{tid}/handoff?wake=1",
                {"to": "cos"})
        self.assertEqual(resp.status, 500)
        data = json.loads(resp.read().decode())
        self.assertFalse(data["ok"])

    def test_team_inbox_feed(self):
        create_workspace("demo")
        self.srv.request(
            "POST", "/v1/workspaces/demo/personas", {"id": "cos"})
        tid = self._mk_task()
        resp = self.srv.request(
            "POST",
            f"/v1/workspaces/demo/personas/default/inbox/{tid}/handoff",
            {"to": "cos"})
        self.assertEqual(resp.status, 200)
        resp.read()
        resp = self.srv.request("GET", "/v1/workspaces/demo/inbox")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode())
        self.assertEqual(data["workspace"], "demo")
        self.assertEqual(data["personas"]["cos"]["open"], 1)
        self.assertEqual(data["personas"]["cos"]["unread"], 1)
        self.assertEqual(data["personas"]["default"]["total"], 0)
        hand = [r for r in data["recent"] if r["task"] == tid]
        self.assertEqual(len(hand), 1)
        self.assertEqual((hand[0]["from"], hand[0]["to"]), ("default", "cos"))


if __name__ == "__main__":
    unittest.main()


class StateDirTests(unittest.TestCase):
    def test_legacy_shared_dir_never_used(self) -> None:
        """A present ~/.agents-relay must not steal state from a workspace."""
        import warnings

        from agents_relay.state import resolve_state_dir

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        home = root / "home"
        home.mkdir()
        (home / ".agents-relay").mkdir()
        host = root / "host"
        host.mkdir()

        env = {"AGENTS_HOST_DIR": str(host), "AGENTS_WORKSPACE": ""}
        import agents_relay.state as state_mod

        state_mod._legacy_warned = False
        with patch.dict(os.environ, env, clear=False), patch(
            "pathlib.Path.home", return_value=home
        ), warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            out = resolve_state_dir()
        self.assertTrue(str(out).startswith(str(host)), out)
        self.assertTrue(out.is_dir())
        self.assertTrue(any("agents-workspace migrate" in str(w.message) for w in seen))

    def test_turn_env_scoped_by_role_and_provider_key(self):
        from agents_workspace import Host

        with tempfile.TemporaryDirectory() as tmp:
            host_dir = Path(tmp) / "host"
            prov_dir = host_dir / "providers"
            prov_dir.mkdir(parents=True)
            (prov_dir / "custom.openai.json").write_text(
                json.dumps({
                    "name": "custom.openai",
                    "kind": "openai_compat",
                    "url": "https://api.example/v1",
                    "model": "gpt-test",
                    "key_env": "OPENAI_API_KEY",
                    "verb": "chat.completions",
                    "cadence": "on_request",
                    "rests_on": "custom openai provider",
                }),
                encoding="utf-8",
            )
            env_patch = {
                "AGENTS_HOST_DIR": str(host_dir),
                "RELAY_SECRET": "rsec",
                "RELAY_WEB_TOKEN": "wtok",
                "WEBHOOK_SECRET": "wsec",
                "TELEGRAM_BOT_TOKEN": "ttok",
                "AGENTS_SYNC_TOKEN": "stok",
                "AGENTS_APPROVAL_TOKEN": "atok",
                "AGENTS_RELAY_URL": "http://127.0.0.1:8787",
                "OPENAI_API_KEY": "oak",
                "ANTHROPIC_API_KEY": "aak",
                "LLM_API_KEY": "lak",
            }
            with patch.dict(os.environ, env_patch, clear=False):
                Host.from_env().create("demo")
                Host.from_env().create("cws", isolation="container")
                turn_env = workspace_env("demo", provider="custom.openai")
                self.assertIsNotNone(turn_env)
                assert turn_env is not None
                self.assertNotIn("RELAY_SECRET", turn_env)
                self.assertNotIn("RELAY_WEB_TOKEN", turn_env)
                self.assertNotIn("WEBHOOK_SECRET", turn_env)
                self.assertNotIn("TELEGRAM_BOT_TOKEN", turn_env)
                self.assertNotIn("AGENTS_SYNC_TOKEN", turn_env)
                self.assertEqual(turn_env["AGENTS_APPROVAL_TOKEN"], "atok")
                self.assertEqual(turn_env["AGENTS_RELAY_URL"], "http://127.0.0.1:8787")
                self.assertEqual(turn_env["OPENAI_API_KEY"], "oak")
                self.assertNotIn("ANTHROPIC_API_KEY", turn_env)
                self.assertNotIn("LLM_API_KEY", turn_env)

                tg_env = workspace_env("demo", provider="custom.openai", approver="telegram")
                assert tg_env is not None
                self.assertNotIn("TELEGRAM_BOT_TOKEN", tg_env)
                self.assertEqual(tg_env.get("AGENTS_APPROVAL_TOKEN"), "atok")
                self.assertEqual(tg_env.get("AGENTS_RELAY_URL"), "http://127.0.0.1:8787")

                c_env = workspace_env("cws", provider="openai.default")
                assert c_env is not None
                self.assertEqual(c_env["LLM_API_KEY"], "lak")
                self.assertNotIn("OPENAI_API_KEY", c_env)
                self.assertNotIn("RELAY_SECRET", c_env)

    def test_explicit_config_state_dir_wins(self) -> None:
        from agents_relay.state import resolve_state_dir

        cfg = _cfg(state_dir="C:/tmp/custom-relay-state")
        self.assertEqual(
            resolve_state_dir(cfg), Path("C:/tmp/custom-relay-state")
        )
