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

        cfg = RelayConfig(
            loop_cmd=("python", "-c", "print('skip')"),
            loop_provider="echo",
            relay_secret="sekrit",
            telegram_bot_token="",
            telegram_allowed_chat_ids=(),
            relay_host="127.0.0.1",
            relay_port=0,
            telegram_poll_timeout=1,
        )
        with patch("agents_relay.loop_client.subprocess.run", return_value=FakeProc()):
            result = run_loop_turn(channel="http", user="u", message="ping", config=cfg)
        self.assertEqual(result.reply, "pong")
        self.assertEqual(result.session, "s1")


class TestHttpTurn(unittest.TestCase):
    def test_v1_turn_auth(self):
        cfg = RelayConfig(
            loop_cmd=("python", "-m", "runner.loop"),
            loop_provider="echo",
            relay_secret="expected",
            telegram_bot_token="",
            telegram_allowed_chat_ids=(),
            relay_host="127.0.0.1",
            relay_port=0,
            telegram_poll_timeout=1,
        )

        def fake_turn(**kwargs):
            from agents_relay.loop_client import LoopTurnResult

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


class TestTelegramFormat(unittest.TestCase):
    def test_format_with_traces_and_links(self):
        from agents_relay.telegram_format import format_telegram_html

        raw = (
            "thinking...\n"
            "[*] Running 'mcp.memory.search' (rests on: memory)...\n"
            "    CMD: python -m agents_memory search ['Lolax']\n"
            "[+] 'mcp.memory.search' OK (exit 0) in 0.28s\n"
            "<ts>2026-09-05T01:24:15+00:00</ts>\n"
            "Hier ist dein Status zu [Lolax](https://lolax.dev):\n"
            "• Alles läuft auf **dev**!\n"
        )
        formatted = format_telegram_html(raw)
        self.assertIn('<a href="https://lolax.dev">Lolax</a>', formatted)
        self.assertIn("<b>dev</b>", formatted)
        self.assertIn("<blockquote expandable><b>tools</b>", formatted)
        self.assertNotIn("(leere Antwort)", formatted)

    def test_bracket_lists_not_stripped(self):
        from agents_relay.telegram_format import format_telegram_html

        text = (
            "Hier sind die Optionen:\n"
            "[1] Option eins mit ein bisschen Text drumherum\n"
            "[2] Option zwei mit noch mehr Erklärung"
        )
        formatted = format_telegram_html(text)
        self.assertIn("[1] Option eins", formatted)
        self.assertIn("[2] Option zwei", formatted)
        self.assertNotIn("(leere Antwort)", formatted)

    def test_urls_with_query_params(self):
        from agents_relay.telegram_format import format_telegram_html

        text = "Check [API Docs](https://api.lolax.dev/v1/search?q=test&lang=de#intro) now!"
        formatted = format_telegram_html(text)
        self.assertIn('<a href="https://api.lolax.dev/v1/search?q=test&amp;lang=de#intro">API Docs</a>', formatted)

    def test_model_json_dump_stripped(self):
        from agents_relay.telegram_format import visible_reply

        tool_json = '{"name": "call_job", "arguments": {"catalog": "mcp.memory.search", "query": "test"}}'
        self.assertEqual(visible_reply(tool_json), "(leere Antwort)")

        tool_fenced = '```json\n{"tool_call": "search", "arguments": {"q": "test"}}\n```'
        self.assertEqual(visible_reply(tool_fenced), "(leere Antwort)")

    def test_normal_json_in_text_preserved(self):
        from agents_relay.telegram_format import visible_reply

        user_code = 'In Python kannst du ein Dict definieren: `{"user": "Felix", "score": 10}`.'
        self.assertIn('{"user": "Felix", "score": 10}', visible_reply(user_code))

    def test_empty_with_traces_returns_fertig(self):
        from agents_relay.telegram_format import visible_reply

        self.assertEqual(visible_reply("", traces=("mcp.memory.search",)), "Fertig.")
        self.assertEqual(visible_reply("", ()), "(leere Antwort)")

    def test_long_message_budget(self):
        from agents_relay.telegram_format import format_telegram_html

        long_text = "A" * 5000
        formatted = format_telegram_html(long_text)
        self.assertLessEqual(len(formatted), 4096)
        self.assertTrue(formatted.endswith("..."))

    def test_html_entities_escaped_with_formatting(self):
        from agents_relay.telegram_format import format_telegram_html

        raw = "Condition: `x < 10 && y > 5` is **true**! Details: [Guide](https://example.com/docs?a=1&b=2)"
        formatted = format_telegram_html(raw)
        self.assertIn("<code>x &lt; 10 &amp;&amp; y &gt; 5</code>", formatted)
        self.assertIn("<b>true</b>", formatted)
        self.assertIn('<a href="https://example.com/docs?a=1&amp;b=2">Guide</a>', formatted)

    def test_code_fence_preserved_if_not_tool_blob(self):
        from agents_relay.telegram_format import visible_reply

        code = "Hier ist die Funktion:\n```python\ndef hello():\n    return 'world'\n```\nFertig."
        cleaned = visible_reply(code)
        self.assertIn("def hello():", cleaned)
        self.assertIn("return 'world'", cleaned)

    def test_traces_escaping_in_blockquote(self):
        from agents_relay.telegram_format import format_telegram_html

        raw = (
            "[*] Running 'tool' (check <stdin> & <stdout>)...\n"
            "[+] OK in 0.1s\n"
            "Done!"
        )
        formatted = format_telegram_html(raw)
        self.assertIn("<blockquote expandable><b>tools</b>", formatted)
        self.assertIn("&lt;stdin&gt; &amp; &lt;stdout&gt;", formatted)
        self.assertIn("Done!", formatted)


class TestSendOutbound(unittest.TestCase):
    def test_allowlist_reject(self):
        from agents_relay.telegram_adapter import send_to_user

        with self.assertRaises(PermissionError):
            send_to_user(
                token="fake-token",
                chat_id=99,
                text="hi",
                allowed=(12345,),
            )

    def test_missing_token(self):
        from agents_relay.telegram_adapter import send_to_user

        with self.assertRaises(ValueError):
            send_to_user(token="", chat_id=1, text="hi", allowed=(1,))

    def test_send_calls_api_without_leaking_token(self):
        from agents_relay.telegram_adapter import send_to_user

        with patch("agents_relay.telegram_adapter.send_message", return_value={"ok": True}) as mock_send:
            send_to_user(token="secret-token-value", chat_id=12345, text="hello", allowed=(12345,))
            mock_send.assert_called_once()
            args, kwargs = mock_send.call_args
            self.assertEqual(args[0], "secret-token-value")
            self.assertEqual(args[1], 12345)

        from agents_relay.__main__ import main as relay_main

        with patch("agents_relay.telegram_adapter.send_message", return_value={"ok": True}):
            with patch(
                "agents_relay.__main__.RelayConfig.from_env",
                return_value=RelayConfig(
                    loop_cmd=("python", "-m", "runner.loop"),
                    loop_provider="echo",
                    relay_secret="",
                    telegram_bot_token="secret-token-value",
                    telegram_allowed_chat_ids=(12345,),
                    relay_host="127.0.0.1",
                    relay_port=0,
                    telegram_poll_timeout=1,
                ),
            ):
                rc = relay_main(["send", "--user", "99", "--text", "nope"])
        self.assertEqual(rc, 1)

    def test_help_json_lists_send(self):
        from agents_relay.__main__ import _help_json

        blob = json.dumps(_help_json())
        self.assertIn("send", blob)
        self.assertNotIn("secret-token-value", blob)


if __name__ == "__main__":
    unittest.main()




