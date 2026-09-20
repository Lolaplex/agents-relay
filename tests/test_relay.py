"""Relay unit tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from http.client import HTTPConnection
from unittest.mock import patch

from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.loop_client import LOOP_TRAILER_MARKER, parse_loop_stdout, run_loop_turn
from agents_relay.turn import StreamEvent, iter_loop_stream, turn_params_from_body
from agents_relay.webhooks import handle_webhook


def _test_config(**overrides) -> RelayConfig:
    base = dict(
        loop_cmd=("python", "-c", "print('skip')"),
        loop_provider="echo",
        relay_secret="expected",
        telegram_bot_token="",
        telegram_allowed_chat_ids=(),
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
        webhook_secret="whsec",
        board_url="",
        board_project="",
        board_key_slug="",
    )
    base.update(overrides)
    return RelayConfig(**base)


class TestTrailerParse(unittest.TestCase):
    def test_parse_trailer(self):
        stdout = f"hello\n{LOOP_TRAILER_MARKER}\n" + json.dumps(
            {"session": "ses_x", "user_id": "u_y", "alias": "telegram:1"}
        )
        parsed = parse_loop_stdout(stdout)
        self.assertEqual(parsed["reply"], "hello")
        self.assertEqual(parsed["session"], "ses_x")


class TestTurnParams(unittest.TestCase):
    def test_turn_params_from_body(self):
        params = turn_params_from_body(
            {
                "channel": "overlay",
                "user": "fabian",
                "text": "hi",
                "provider": "echo",
                "persona": "default",
                "project": "demo",
            }
        )
        self.assertEqual(params.channel, "overlay")
        self.assertEqual(params.provider, "echo")
        self.assertEqual(params.persona, "default")
        self.assertEqual(params.project, "demo")


class TestLoopSubprocess(unittest.TestCase):
    def test_mock_loop_subprocess(self):
        trailer = json.dumps({"session": "s1", "user_id": "u1", "alias": "a1"})
        fake_stdout = f"pong\n{LOOP_TRAILER_MARKER}\n{trailer}\n"

        @dataclass
        class FakeProc:
            returncode: int = 0
            stdout: str = fake_stdout
            stderr: str = ""

        cfg = _test_config(gateway_secret="sekrit")
        with patch("agents_gateway.turn.subprocess.run", return_value=FakeProc()):
            result = run_loop_turn(
                channel="http",
                user="u",
                message="ping",
                config=cfg,
                provider="scripted.tool",
                persona="default",
            )
        self.assertEqual(result.reply, "pong")
        self.assertEqual(result.session, "s1")


class TestHttpTurn(unittest.TestCase):
    def test_v1_turn_auth_and_provider(self):
        cfg = _test_config()

        def fake_turn(**kwargs):
            from agents_relay.loop_client import LoopTurnResult

            self.assertEqual(kwargs.get("provider"), "echo")
            self.assertEqual(kwargs.get("persona"), "default")
            return LoopTurnResult(reply="ok", session="ses_1", user_id="u_1", alias="", returncode=0, stderr="")

        server = serve_http(cfg, on_turn=fake_turn)
        host, port = server.server_address  # type: ignore[misc]
        server_thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            body = json.dumps(
                {
                    "channel": "http",
                    "user": "t",
                    "text": "hi",
                    "provider": "echo",
                    "persona": "default",
                }
            )
            conn = HTTPConnection(host, port, timeout=5)
            conn.request("POST", "/v1/turn", body=body, headers={"Content-Type": "application/json"})
            self.assertEqual(conn.getresponse().status, 401)

            conn = HTTPConnection(host, port, timeout=5)
            conn.request(
                "POST",
                "/v1/turn",
                body=body,
                headers={"Content-Type": "application/json", "X-Relay-Secret": "expected"},
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertEqual(data["reply"], "ok")
            self.assertFalse(data["notified"])

            # Test /v1/alert with notification mocking
            with patch("agents_relay.telegram_adapter.send_message") as mock_send:
                cfg_with_tg = RelayConfig(
                    loop_cmd=("python", "-m", "runner.loop"),
                    loop_provider="echo",
                    relay_secret="expected",
                    telegram_bot_token="fake_bot_token",
                    telegram_allowed_chat_ids=(12345,),
                    relay_host="127.0.0.1",
                    relay_port=0,
                    telegram_poll_timeout=1,
                )
                tg_server = serve_http(cfg_with_tg, on_turn=fake_turn)
                tg_host, tg_port = tg_server.server_address
                tg_thread = __import__("threading").Thread(target=tg_server.serve_forever, daemon=True)
                tg_thread.start()
                try:
                    alert_conn = HTTPConnection(tg_host, tg_port, timeout=5)
                    alert_body = json.dumps({"text": "system alert"})
                    alert_conn.request(
                        "POST",
                        "/v1/alert",
                        body=alert_body,
                        headers={"Content-Type": "application/json", "X-Relay-Secret": "expected"},
                    )
                    alert_resp = alert_conn.getresponse()
                    self.assertEqual(alert_resp.status, 200)
                    alert_data = json.loads(alert_resp.read().decode())
                    self.assertTrue(alert_data["notified"])
                    mock_send.assert_called_once()
                finally:
                    tg_server.shutdown()
                    tg_thread.join(timeout=2)
        finally:
            server.shutdown()
            server_thread.join(timeout=2)


class TestWebhook(unittest.TestCase):
    def test_webhook_auth(self):
        cfg = _test_config()
        bad = handle_webhook("ci", {"status": "failed"}, raw_body=b"{}", secret_header="nope", config=cfg, notify_turn=False)
        self.assertFalse(bad["ok"])
        good = handle_webhook(
            "ci",
            {"repo": "agents-harness", "status": "failed"},
            raw_body=b"{}",
            secret_header="whsec",
            config=cfg,
            notify_turn=False,
        )
        self.assertTrue(good["ok"])
        self.assertIn("CI alert", good["alert"])


class TestStream(unittest.TestCase):
    def test_iter_loop_stream(self):
        trailer = json.dumps({"session": "s1", "user_id": "u1", "alias": "a1"})
        fake_stdout = f"hel{LOOP_TRAILER_MARKER}\n{trailer}\n"

        class FakeStdout:
            def __init__(self, data: str):
                self._data = data
                self._i = 0

            def read(self, n: int = 1) -> str:
                if self._i >= len(self._data):
                    return ""
                chunk = self._data[self._i : self._i + n]
                self._i += n
                return chunk

        class FakeProc:
            returncode = 0
            stdout = FakeStdout(fake_stdout)
            stderr = None

            def poll(self):
                return 0 if self.stdout._i >= len(fake_stdout) else None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        from agents_relay.turn import TurnParams

        params = TurnParams(channel="http", user="u", message="hi")
        with patch("agents_relay.turn.subprocess.Popen", return_value=FakeProc()):
            events = list(iter_loop_stream(params, config=_test_config()))
        self.assertTrue(any(e.type == "delta" for e in events))
        self.assertEqual(events[-1].type, "trailer")
        self.assertEqual(events[-1].session, "s1")


if __name__ == "__main__":
    unittest.main()
