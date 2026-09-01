"""Gateway unit tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from http.client import HTTPConnection
from unittest.mock import patch

from agents_gateway.config import GatewayConfig
from agents_gateway.http_adapter import serve_http
from agents_gateway.loop_client import LOOP_TRAILER_MARKER, parse_loop_stdout, run_loop_turn


class TestTrailerParse(unittest.TestCase):
    def test_parse_trailer(self):
        stdout = f"hello\n{LOOP_TRAILER_MARKER}\n" + json.dumps(
            {"session": "ses_x", "user_id": "u_y", "alias": "telegram:1"}
        )
        parsed = parse_loop_stdout(stdout)
        self.assertEqual(parsed["reply"], "hello")
        self.assertEqual(parsed["session"], "ses_x")
        self.assertEqual(parsed["user_id"], "u_y")
        self.assertEqual(parsed["alias"], "telegram:1")


class TestLoopSubprocess(unittest.TestCase):
    def test_mock_loop_subprocess(self):
        trailer = json.dumps({"session": "s1", "user_id": "u1", "alias": "a1"})
        fake_stdout = f"pong\n{LOOP_TRAILER_MARKER}\n{trailer}\n"

        @dataclass
        class FakeProc:
            returncode: int = 0
            stdout: str = fake_stdout
            stderr: str = ""

        cfg = GatewayConfig(
            loop_cmd=("python", "-c", "print('skip')"),
            loop_provider="echo",
            gateway_secret="sekrit",
            telegram_bot_token="",
            telegram_allowed_chat_ids=(),
            gateway_host="127.0.0.1",
            gateway_port=0,
            telegram_poll_timeout=1,
        )
        with patch("agents_gateway.loop_client.subprocess.run", return_value=FakeProc()):
            result = run_loop_turn(channel="http", user="u", message="ping", config=cfg)
        self.assertEqual(result.reply, "pong")
        self.assertEqual(result.session, "s1")


class TestHttpTurn(unittest.TestCase):
    def test_v1_turn_auth(self):
        cfg = GatewayConfig(
            loop_cmd=("python", "-m", "runner.loop"),
            loop_provider="echo",
            gateway_secret="expected",
            telegram_bot_token="",
            telegram_allowed_chat_ids=(),
            gateway_host="127.0.0.1",
            gateway_port=0,
            telegram_poll_timeout=1,
        )

        def fake_turn(**kwargs):
            from agents_gateway.loop_client import LoopTurnResult

            return LoopTurnResult(reply="ok", session="", user_id="", alias="", returncode=0, stderr="")

        server = serve_http(cfg, on_turn=fake_turn)
        server.server_port  # type: ignore[attr-defined]
        host, port = server.server_address  # type: ignore[misc]
        server_thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            conn = HTTPConnection(host, port, timeout=5)
            body = json.dumps({"channel": "http", "user": "t", "text": "hi"})
            conn.request("POST", "/v1/turn", body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 401)

            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/v1/turn",
                body=body,
                headers={"Content-Type": "application/json", "X-Gateway-Secret": "expected"},
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertEqual(data["reply"], "ok")
        finally:
            server.shutdown()
            server_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
