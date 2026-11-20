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
import json, sys
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
            env = _env(tmp, LOOP_CMD=f"{sys.executable} {loop}")
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
            env_save = os.environ.get("AGENTS_RELAY_STATE")
            os.environ["AGENTS_RELAY_STATE"] = tmp
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
                if env_save is None:
                    os.environ.pop("AGENTS_RELAY_STATE", None)
                else:
                    os.environ["AGENTS_RELAY_STATE"] = env_save
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("STUB-AGENT:reply", text)
            self.assertIn("---agents-loop-trailer---", text)
            self.assertIn('"session": "ses_t1"', text)
            # session id persisted for load on the next turn
            stored = json.loads((Path(tmp) / "acp_sessions" / "ses_t1.json").read_text())
            self.assertEqual(stored["acpSessionId"], "acp_stub_1")


if __name__ == "__main__":
    unittest.main()
