"""ACP adapter tests — both directions, via stub subprocesses."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

FAKE_LOOP = """\
import json, sys
msg = ""
for i, a in enumerate(sys.argv):
    if a == "--message":
        msg = sys.argv[i + 1]
sys.stdout.write("STUB-LOOP:" + msg)
sys.stdout.write("\\n---agents-loop-trailer---\\n" + json.dumps({"session": "ses_stub"}) + "\\n")
"""

FAKE_AGENT = """\
import json, os, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    m = json.loads(line)
    mid, method = m.get("id"), m.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}}), flush=True)
    elif method == "session/new":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "acp_stub_1"}}), flush=True)
    elif method == "session/load":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {}}), flush=True)
    elif method == "session/set_config_option":
        log = os.environ.get("AGENT_LOG")
        if log:
            with open(log, "a") as fh:
                fh.write(json.dumps(m.get("params") or {}) + "\\n")
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {}}), flush=True)
    elif method == "session/prompt":
        sid = (m.get("params") or {}).get("sessionId")
        for piece in ("STUB-AGENT:", "reply"):
            upd = {"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": piece}}}}
            print(json.dumps(upd), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}}), flush=True)
"""


def _env(tmp: str, **extra) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["AGENTS_RELAY_STATE"] = tmp
    env.update(extra)
    return env


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


class TestAcpServe(unittest.TestCase):
    """Zed-side: acp-serve must speak NDJSON JSON-RPC and stream a turn."""

    def test_initialize_and_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loop = _write(Path(tmp) / "fake_loop.py", FAKE_LOOP)
            env = _env(
                tmp, LOOP_CMD=f"{sys.executable} {loop}", LOOP_PROVIDER="echo"
            )
            proc = subprocess.Popen(
                [sys.executable, "-m", "agents_relay", "acp-serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=tmp,
            )
            assert proc.stdin and proc.stdout

            def rpc(msg: dict) -> dict:
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
                return json.loads(proc.stdout.readline())

            init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": 1, "clientCapabilities": {}}})
            self.assertEqual(init["result"]["protocolVersion"], 1)
            self.assertEqual(init["result"]["agentInfo"]["name"], "klanker")

            new = rpc({"jsonrpc": "2.0", "id": 2, "method": "session/new",
                       "params": {"cwd": tmp, "mcpServers": []}})
            sid = new["result"]["sessionId"]
            self.assertTrue(sid.startswith("ses_acp_"))

            # prompt: expect update notifications then the end_turn result
            proc.stdin.write(json.dumps({
                "jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                "params": {"sessionId": sid,
                           "prompt": [{"type": "text", "text": "hello"}]}}) + "\n")
            proc.stdin.flush()
            chunks: list[str] = []
            result = None
            for _ in range(50):
                msg = json.loads(proc.stdout.readline())
                if msg.get("method") == "session/update":
                    chunks.append(str(msg["params"]["update"]["content"].get("text") or ""))
                if msg.get("id") == 3:
                    result = msg
                    break
            self.assertIsNotNone(result)
            self.assertEqual(result["result"]["stopReason"], "end_turn")
            self.assertIn("STUB-LOOP:hello", "".join(chunks))
            proc.kill()
            proc.wait(timeout=10)


class TestAcpRun(unittest.TestCase):
    """Client-side: acp-run must drive a foreign agent and emit loop-shape output."""

    def test_turn_streams_and_trailers(self) -> None:
        from agents_relay.acp import run_acp_turn
        from agents_relay.config import RelayConfig

        with tempfile.TemporaryDirectory() as tmp:
            agent = _write(Path(tmp) / "fake_agent.py", FAKE_AGENT)
            import io, contextlib

            out = io.StringIO()
            env_save = {
                k: os.environ.get(k)
                for k in ("AGENTS_RELAY_DIR", "AGENTS_HOST_DIR", "AGENTS_WORKSPACE")
            }
            os.environ["AGENTS_RELAY_DIR"] = tmp
            os.environ["AGENTS_HOST_DIR"] = tmp
            os.environ.pop("AGENTS_WORKSPACE", None)
            config = RelayConfig.from_env()
            try:
                with contextlib.redirect_stdout(out):
                    rc = run_acp_turn(
                        agent_argv=[sys.executable, str(agent)],
                        message="ping",
                        session="ses_t1",
                        workdir=tmp,
                        config=config,
                    )
            finally:
                for k, v in env_save.items():
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("STUB-AGENT:reply", text)
            self.assertIn("---agents-loop-trailer---", text)
            self.assertIn('"session": "ses_t1"', text)
            # session id persisted in host cache for load on the next turn
            stored = json.loads(
                (Path(tmp) / "cache" / "acp_sessions" / "ses_t1.json").read_text()
            )
            self.assertEqual(stored["acpSessionId"], "acp_stub_1")

    def test_model_goes_to_set_config_option(self) -> None:
        from agents_relay.acp import run_acp_turn
        from agents_relay.config import RelayConfig

        with tempfile.TemporaryDirectory() as tmp:
            agent = _write(Path(tmp) / "fake_agent.py", FAKE_AGENT)
            log = str(Path(tmp) / "agent.log")
            import io, contextlib

            env_save = {
                k: os.environ.get(k)
                for k in (
                    "AGENTS_RELAY_DIR",
                    "AGENTS_HOST_DIR",
                    "AGENTS_WORKSPACE",
                    "AGENT_LOG",
                )
            }
            os.environ["AGENTS_RELAY_DIR"] = tmp
            os.environ["AGENTS_HOST_DIR"] = tmp
            os.environ.pop("AGENTS_WORKSPACE", None)
            os.environ["AGENT_LOG"] = log
            config = RelayConfig.from_env()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = run_acp_turn(
                        agent_argv=[sys.executable, str(agent)],
                        message="ping", session="ses_m1", workdir=tmp,
                        config=config, model="swe-2-high",
                    )
            finally:
                for k, v in env_save.items():
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            self.assertEqual(rc, 0)
            calls = [json.loads(x) for x in Path(log).read_text().splitlines()]
            self.assertEqual(calls, [{
                "sessionId": "acp_stub_1", "configId": "model",
                "value": "swe-2-high"}])

    def test_acp_run_strips_secrets_and_authenticate_chmods_0600(self) -> None:
        from unittest.mock import patch
        from agents_relay.acp import _acp_env, serve_acp
        from agents_relay.config import RelayConfig

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "AGENTS_HOST_DIR": tmp,
                "OPENAI_API_KEY": "oak",
                "LLM_API_KEY": "lak",
                "RELAY_SECRET": "rsec",
                "AGENTS_ACP_API_KEY": "acpsec",
                "GITHUB_TOKEN": "ght",
                "PATH": os.environ.get("PATH", ""),
            }
            with patch.dict(os.environ, env, clear=True):
                child = _acp_env(tmp)
            self.assertIsNotNone(child)
            assert child is not None
            for secret in (
                "OPENAI_API_KEY",
                "LLM_API_KEY",
                "RELAY_SECRET",
                "AGENTS_ACP_API_KEY",
                "GITHUB_TOKEN",
            ):
                self.assertNotIn(secret, child)

        import io

        with tempfile.TemporaryDirectory() as tmp:
            req = json.dumps({
                "jsonrpc": "2.0",
                "id": 9,
                "method": "authenticate",
                "params": {"methodId": "llm_api_key", "meta": {"api_key": "k-secret"}},
            })
            out = io.StringIO()
            chmod_calls: list[tuple[str, int]] = []
            with patch.dict(os.environ, {"AGENTS_HOST_DIR": tmp, "AGENTS_RELAY_DIR": tmp}, clear=False):
                with patch("sys.stdin", io.StringIO(req + "\n")), \
                     patch("sys.stdout", out), \
                     patch("agents_relay.state._is_posix", return_value=True), \
                     patch("agents_relay.state.os.open", wraps=os.open), \
                     patch("agents_relay.state.os.chmod", side_effect=lambda p, m: chmod_calls.append((str(p), m))), \
                     patch("agents_relay.acp.os.chmod", side_effect=lambda p, m: chmod_calls.append((str(p), m))):
                    rc = serve_acp(RelayConfig.from_env())
            self.assertEqual(rc, 0)
            saved = json.loads((Path(tmp) / "secrets.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["LLM_API_KEY"], "k-secret")
            self.assertTrue(any(m == 0o600 for _, m in chmod_calls))


class TestAcpProviderWiring(unittest.TestCase):
    """acp.* providers → acp-run argv, env cmd map, manifest, probe rows."""

    def _env_save(self, *keys):
        saved = {k: os.environ.get(k) for k in keys}
        self.addCleanup(
            lambda: [
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                for k, v in saved.items()])
        return saved

    def test_agent_cmd_suffix_and_fallback(self):
        from agents_relay.acp import acp_agent_cmd

        # Isolate the host providers dir: a real <host>/providers/acp.devin.json
        # with a `command` field would win over the env under test.
        self._env_save("AGENTS_HOST_DIR", "AGENTS_ACP_CMD", "AGENTS_ACP_CMD_DEVIN")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["AGENTS_HOST_DIR"] = tmp.name
        os.environ["AGENTS_ACP_CMD"] = "devin acp"
        os.environ["AGENTS_ACP_CMD_DEVIN"] = "devin acp --model x"
        self.assertEqual(acp_agent_cmd("acp.devin"), "devin acp --model x")
        self.assertEqual(acp_agent_cmd("acp.other"), "devin acp")
        self.assertEqual(acp_agent_cmd(), "devin acp")
        os.environ.pop("AGENTS_ACP_CMD_DEVIN")
        os.environ.pop("AGENTS_ACP_CMD")
        self.assertEqual(acp_agent_cmd("acp.devin"), "")

    def test_provider_name_env_and_basename(self):
        from agents_relay.acp import acp_provider_name

        self._env_save("AGENTS_ACP_CMD", "AGENTS_ACP_NAME")
        os.environ["AGENTS_ACP_CMD"] = "devin acp"
        self.assertEqual(acp_provider_name(), "acp.devin")
        os.environ["AGENTS_ACP_NAME"] = "zedfriend"
        self.assertEqual(acp_provider_name(), "acp.zedfriend")

    def test_ensure_manifest_registers_provider(self):
        from agents_relay.acp import ensure_acp_manifest

        self._env_save("AGENTS_ACP_CMD", "AGENTS_HOST_DIR")
        os.environ["AGENTS_ACP_CMD"] = "devin acp"
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AGENTS_HOST_DIR"] = tmp
            ensure_acp_manifest()
            path = Path(tmp) / "providers" / "acp.devin.json"
            self.assertTrue(path.is_file())
            from runner.providers import find_provider
            row = find_provider("acp.devin")
            self.assertIsNotNone(row)
            self.assertEqual(row["kind"], "acp-agent")
            # runner.model.use accepts the pick — no special casing needed
            from agents_workspace import Host
            from runner.model import use
            ws_dir = Path(tmp) / "workspaces" / "demo"
            ws_dir.mkdir(parents=True)
            (ws_dir / "workspace.json").write_text("{}")
            ws = Host.from_env().workspace("demo")
            out = use("acp.devin", "swe-2-high", workspace=ws)
            self.assertEqual(out["provider"], "acp.devin")

    def test_spawn_argv_wraps_cmd_shims_on_windows(self):
        from unittest.mock import patch

        from agents_relay.acp import _resolve_spawn_argv

        with patch("agents_relay.acp.os.name", "nt"), patch(
            "shutil.which", return_value="C:\\bin\\devin.cmd"
        ):
            argv = _resolve_spawn_argv(["devin", "acp"])
            self.assertEqual(argv, ["cmd.exe", "/c", "C:\\bin\\devin.cmd", "acp"])
        with patch("agents_relay.acp.os.name", "nt"), patch(
            "shutil.which", return_value="C:\\bin\\devin.exe"
        ):
            self.assertEqual(
                _resolve_spawn_argv(["devin", "acp"]),
                ["C:\\bin\\devin.exe", "acp"],
            )
        with patch("agents_relay.acp.os.name", "nt"), patch(
            "shutil.which", return_value=None
        ):
            self.assertRaises(FileNotFoundError, _resolve_spawn_argv, ["nope"])

    def test_acp_api_key_per_name_over_shared(self):
        from agents_relay.acp import acp_api_key

        self._env_save("AGENTS_ACP_API_KEY", "AGENTS_ACP_API_KEY_DEVIN")
        os.environ.pop("AGENTS_ACP_API_KEY", None)
        os.environ["AGENTS_ACP_API_KEY_DEVIN"] = "sk-devin"
        self.assertEqual(acp_api_key("acp.devin"), "sk-devin")
        os.environ["AGENTS_ACP_API_KEY"] = "sk-shared"
        self.assertEqual(acp_api_key("acp.devin"), "sk-devin")
        self.assertEqual(acp_api_key("acp.other"), "sk-shared")

    def test_build_loop_argv_acp_spawn(self):
        from agents_relay.config import RelayConfig
        from agents_relay.turn import TurnParams, build_loop_argv

        self._env_save("AGENTS_ACP_CMD_DEVIN")
        os.environ["AGENTS_ACP_CMD_DEVIN"] = "devin acp"
        cfg = RelayConfig.from_env()
        params = TurnParams(provider="acp.devin", model="swe-2-high")
        argv = build_loop_argv(params, cfg)
        self.assertEqual(argv[:4], [sys.executable, "-m", "agents_relay", "acp-run"])
        self.assertIn("--provider", argv)
        self.assertEqual(argv[argv.index("--provider") + 1], "acp.devin")
        self.assertEqual(argv[argv.index("--model") + 1], "swe-2-high")

    def test_workspace_pick_injects_acp(self):
        from agents_relay.config import RelayConfig
        from agents_relay.turn import TurnParams, build_loop_argv

        self._env_save("AGENTS_HOST_DIR", "AGENTS_ACP_CMD_DEVIN")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AGENTS_HOST_DIR"] = tmp
            os.environ["AGENTS_ACP_CMD_DEVIN"] = "devin acp"
            ws_dir = Path(tmp) / "workspaces" / "demo"
            ws_dir.mkdir(parents=True)
            (ws_dir / "workspace.json").write_text("{}")
            (ws_dir / "config.json").write_text(json.dumps(
                {"model": {"provider": "acp.devin", "model": "swe-2-high"}}))
            cfg = RelayConfig.from_env()
            params = TurnParams(workspace="demo")
            argv = build_loop_argv(params, cfg)
            self.assertEqual(argv[:4],
                             [sys.executable, "-m", "agents_relay", "acp-run"])
            self.assertEqual(argv[argv.index("--provider") + 1], "acp.devin")
            self.assertEqual(argv[argv.index("--model") + 1], "swe-2-high")
            # explicit flag beats the pick
            params2 = TurnParams(workspace="demo", provider="echo", model="m2")
            argv2 = build_loop_argv(params2, cfg)
            self.assertEqual(argv2[:3], ["python", "-m", "runner.loop"])
            self.assertEqual(argv2[argv2.index("--model") + 1], "m2")

    def test_provider_rows_enriched(self):
        from unittest.mock import patch
        from agents_relay import acp
        from agents_relay.config import RelayConfig

        self._env_save("AGENTS_ACP_CMD", "AGENTS_HOST_DIR")
        os.environ["AGENTS_ACP_CMD"] = "devin acp"
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AGENTS_HOST_DIR"] = tmp
            acp.ensure_acp_manifest()
            fake = {"model": "swe-2-high", "models": ["a", "b"],
                    "mode": "accept-edits", "modes": []}
            self._env_save("AGENTS_RELAY_DIR")
            os.environ["AGENTS_RELAY_DIR"] = tmp
            with patch.object(acp, "acp_agent_info", return_value=fake):
                # Plain listing reads the probe cache only (no spawn) —
                # refresh=1 is the path that calls acp_agent_info.
                rows = acp.acp_provider_rows(RelayConfig.from_env(), refresh=True)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["name"], "acp.devin")
            self.assertEqual(row["models"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
