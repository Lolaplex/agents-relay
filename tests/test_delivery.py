"""Cron/routine delivery matrix: notify fan-out to arbitrary channel+user."""

import json
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.loop_client import LoopTurnResult
from agents_relay.telegram_adapter import resolve_delivery


def _config(tmp: str, allowed=(7, 9)) -> RelayConfig:
    return RelayConfig(
        loop_cmd=("python", "-m", "runner.loop"),
        loop_provider="echo",
        relay_secret="expected",
        telegram_bot_token="tok",
        telegram_allowed_chat_ids=allowed,
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
        state_dir=str(Path(tmp) / "relay"),
    )


def _turn(**kw):
    return LoopTurnResult(reply="done", session="ses_1", user_id="u", alias="", returncode=0, stderr="")


class ResolveDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_specs(self):
        cfg = self.cfg
        cases = {
            "telegram:7": ([7], []),
            "7": ([7], []),
            "telegram:*": ([7, 9], []),
            "all": ([7, 9], []),
            "telegram": ([7, 9], []),
            "none": ([], []),
            "telegram:999": ([], ["telegram:999"]),
            "telegram:7,board:9,9": ([7, 9], ["board:9"]),
            "telegram:7,telegram:7": ([7], []),
        }
        for spec, want in cases.items():
            self.assertEqual(resolve_delivery(spec, cfg), want, spec)


class NotifyMatrixTests(unittest.TestCase):
    def _post_turn(self, server, body: dict) -> tuple[int, dict]:
        host, port = server.server_address  # type: ignore[misc]
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/v1/turn",
            body=json.dumps(body),
            headers={"Content-Type": "application/json", "X-Relay-Secret": "expected"},
        )
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode())

    def test_default_notify_first_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = serve_http(_config(tmp), on_turn=_turn)
            import threading

            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                with patch("agents_relay.telegram_adapter.deliver_html") as mock_del:
                    status, data = self._post_turn(server, {"text": "hi", "notify": True})
            finally:
                server.shutdown()
                t.join(timeout=2)
        self.assertEqual(status, 200)
        self.assertTrue(data["notified"])
        self.assertEqual([c.args[1] for c in mock_del.call_args_list], [7])

    def test_deliver_spec_fanout_and_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = serve_http(_config(tmp), on_turn=_turn)
            import threading

            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                with patch("agents_relay.telegram_adapter.deliver_html") as mock_del:
                    status, data = self._post_turn(
                        server,
                        {"text": "hi", "notify": True, "deliver": "telegram:9,board:9"},
                    )
            finally:
                server.shutdown()
                t.join(timeout=2)
        self.assertEqual(status, 200)
        self.assertEqual([c.args[1] for c in mock_del.call_args_list], [9])
        self.assertEqual(data["delivered"], [9])
        self.assertEqual(data["skipped"], ["board:9"])

    def test_deliver_none_suppresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = serve_http(_config(tmp), on_turn=_turn)
            import threading

            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                with patch("agents_relay.telegram_adapter.deliver_html") as mock_del:
                    status, data = self._post_turn(
                        server, {"text": "hi", "notify": True, "deliver": "none"}
                    )
            finally:
                server.shutdown()
                t.join(timeout=2)
        self.assertEqual(status, 200)
        self.assertFalse(data["notified"])
        mock_del.assert_not_called()


if __name__ == "__main__":
    unittest.main()
