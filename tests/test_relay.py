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

    def test_timeout_reply_hides_command(self):
        cfg = RelayConfig(
            loop_cmd=("python", "-c", "import time; time.sleep(30)"),
            loop_provider="echo",
            relay_secret="",
            telegram_bot_token="",
            telegram_allowed_chat_ids=(),
            relay_host="127.0.0.1",
            relay_port=0,
            telegram_poll_timeout=1,
        )
        result = run_loop_turn(
            channel="telegram",
            user="u",
            message="Hi",
            config=cfg,
            timeout_sec=1,
            on_status=lambda _status: None,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.reply, "Turn stopped before a final answer.")
        self.assertNotIn("runner.loop", result.reply)
        self.assertNotIn("sleep", result.reply)


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

    def test_process_update_edits_message(self):
        from agents_relay.telegram_adapter import process_update
        from agents_relay.loop_client import LoopTurnResult

        cfg = RelayConfig(
            loop_cmd=("python", "-m", "runner.loop"),
            loop_provider="echo",
            relay_secret="",
            telegram_bot_token="fake_bot_token",
            telegram_allowed_chat_ids=(12345,),
            relay_host="127.0.0.1",
            relay_port=8787,
            telegram_poll_timeout=1,
        )
        fake_update = {
            "message": {
                "chat": {"id": 12345},
                "text": "test message",
                "from": {"username": "felix"},
            }
        }
        with patch("agents_relay.telegram_adapter.send_message", return_value={"result": {"message_id": 99}}) as mock_send:
            with patch("agents_relay.telegram_adapter.edit_message") as mock_edit:
                def fake_turn(*args, **kwargs):
                    if "on_status" in kwargs and kwargs["on_status"]:
                        kwargs["on_status"]("running mcp.terminal...")
                    return LoopTurnResult(
                        reply="Echo: test message",
                        session="ses_123",
                        user_id="u_123",
                        alias="telegram:felix",
                        returncode=0,
                        stderr="[*] Running 'mcp.terminal'...\n[+] 'mcp.terminal' OK (exit 0)",
                    )

                process_update(fake_update, config=cfg, on_turn=fake_turn)
                mock_send.assert_called_once()
                self.assertGreaterEqual(mock_edit.call_count, 1)
                self.assertIn("Echo: test message", mock_edit.call_args[0][3])



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
        self.assertNotIn("<blockquote expandable><b>tools</b>", formatted)
        self.assertNotIn("[*] Running", formatted)
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

    def test_empty_with_traces_synthesizes_progress(self):
        from agents_relay.telegram_format import visible_reply

        traces = (
            "[*] Running 'mcp.memory.search' (rests on: memory)...",
            "[+] 'mcp.memory.search' OK (exit 0) in 0.28s",
        )
        out = visible_reply("", traces=traces)
        self.assertIn("Memory", out)
        self.assertNotEqual(out.strip(), "")
        self.assertNotEqual(out, "Fertig.")
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

    def test_failure_notes_humanized_not_cli(self):
        from agents_relay.telegram_format import format_telegram_html

        raw = (
            "[*] Running 'tool' (check <stdin> & <stdout>)...\n"
            "[-] 'mcp.memory.add' FAILED: expected exit 0, got 2\n"
            "Done!"
        )
        formatted = format_telegram_html(raw)
        self.assertNotIn("<blockquote expandable><b>tools</b>", formatted)
        self.assertNotIn("[*] Running", formatted)
        self.assertIn("Memory fehlgeschlagen (exit 2).", formatted)
        self.assertIn("Done!", formatted)

    def test_humanize_status_calendar_progress(self):
        from agents_relay.telegram_format import humanize_status, status_html

        running = (
            "[*] Running 'mcp.calendar.list' (rests on: agents-calendar CLI list "
            "VEVENTs; extra argv is --from/--to ISO; exit 0 = listed)..."
        )
        self.assertEqual(humanize_status(running), "Kalender (mcp.calendar.list) …")
        self.assertEqual(
            humanize_status("[+] 'mcp.calendar.list' OK (exit 0) in 2.21s"),
            "Kalender fertig.",
        )
        self.assertEqual(
            humanize_status("[-] 'mcp.memory.add' FAILED: expected exit 0, got 2"),
            "Memory fehlgeschlagen (exit 2).",
        )
        self.assertEqual(humanize_status("CMD: python -m agents_calendar list"), "")
        self.assertEqual(humanize_status("thinking..."), "Einen Moment …")
        self.assertIn("Einen Moment", status_html("thinking..."))
        self.assertNotIn("[*]", status_html(running))

    def test_empty_final_never_whitespace(self):
        from agents_relay.telegram_format import format_telegram_html

        formatted = format_telegram_html(
            "thinking...\n[*] Running 'mcp.calendar.list'...\n",
            (
                "[*] Running 'mcp.calendar.list'...",
                "[+] 'mcp.calendar.list' OK (exit 0) in 2.21s",
            ),
        )
        self.assertTrue(formatted.strip())
        self.assertNotIn("(leere Antwort)", formatted)
        self.assertIn("Kalender", formatted)

    def test_long_body_not_truncated_for_traces(self):
        from agents_relay.telegram_format import format_telegram_html, TG_LIMIT

        body = "A" * 4000
        traces = ("[-] 'mcp.memory.add' FAILED: expected exit 0, got 2",) * 20
        formatted = format_telegram_html(body, traces)
        self.assertLessEqual(len(formatted), TG_LIMIT)
        self.assertIn("A" * 80, formatted)

    def test_markdown_table_transformation(self):
        from agents_relay.telegram_format import format_telegram_html

        table = (
            "Hier ist der Status:\n"
            "| Repo | Letzter Push | Was drin ist |\n"
            "|---|---|---|\n"
            '| klanker (dev) | heute 13:28 | echter Content: *"workspace grounding"* |\n'
            "| agents-harness | heute 13:28 | 1 Commit seit 04.09 |\n"
            "| agents-terminal | heute 13:27 | 1 Commit seit 05.09 |\n"
            "Fertig."
        )
        formatted = format_telegram_html(table)
        self.assertNotIn("|---|---|---|", formatted)
        self.assertIn('• <b>klanker (dev)</b> (<i>heute 13:28</i>) — echter Content: <i>"workspace grounding"</i>', formatted)
        self.assertIn("• <b>agents-harness</b> (<i>heute 13:28</i>) — 1 Commit seit 04.09", formatted)
        self.assertIn("• <b>agents-terminal</b> (<i>heute 13:27</i>) — 1 Commit seit 05.09", formatted)




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


class TestTelegramAdapterResilience(unittest.TestCase):
    def test_edit_message_ignores_message_not_modified(self):
        import io
        import urllib.error
        from agents_relay.telegram_adapter import edit_message

        err_fp = io.BytesIO(b'{"ok": false, "error_code": 400, "description": "Bad Request: message is not modified"}')
        http_err = urllib.error.HTTPError("http://example.com", 400, "Bad Request", {}, err_fp)

        with patch("agents_relay.telegram_adapter._post_json", side_effect=http_err):
            res = edit_message("token", 12345, 99, "<i>thinking...</i>", parse_mode="HTML")
            self.assertTrue(res.get("ok"))
            self.assertEqual(res["result"]["message_id"], 99)

    def test_edit_message_strips_html_on_entity_error(self):
        import io
        import urllib.error
        from agents_relay.telegram_adapter import edit_message

        err_fp = io.BytesIO(b'{"ok": false, "error_code": 400, "description": "Bad Request: can\'t parse entities"}')
        http_err = urllib.error.HTTPError("http://example.com", 400, "Bad Request", {}, err_fp)

        calls = []

        def fake_post(url, body):
            calls.append(body)
            if len(calls) == 1:
                raise http_err
            return {"ok": True, "result": {"message_id": 99}}

        with patch("agents_relay.telegram_adapter._post_json", side_effect=fake_post):
            res = edit_message("token", 12345, 99, "<b>hello</b> <i>world</i>", parse_mode="HTML")
            self.assertTrue(res.get("ok"))
            self.assertEqual(len(calls), 2)
            self.assertNotIn("parse_mode", calls[1])
            self.assertEqual(calls[1]["text"], "hello world")

    def test_process_update_accepts_caption_without_text(self):
        from agents_relay.telegram_adapter import process_update
        from agents_relay.loop_client import LoopTurnResult

        cfg = RelayConfig(
            loop_cmd=("python", "-m", "runner.loop"),
            loop_provider="echo",
            relay_secret="",
            telegram_bot_token="fake_bot_token",
            telegram_allowed_chat_ids=(12345,),
            relay_host="127.0.0.1",
            relay_port=8787,
            telegram_poll_timeout=1,
        )
        fake_update = {
            "message": {
                "chat": {"id": 12345},
                "caption": "Photo caption question",
                "from": {"username": "felix"},
            }
        }
        received_msg = []

        def fake_turn(*args, **kwargs):
            received_msg.append(kwargs.get("message"))
            return LoopTurnResult(reply="Answer", session="s", user_id="u", alias="a", returncode=0, stderr="")

        with patch("agents_relay.telegram_adapter.send_message", return_value={"result": {"message_id": 99}}):
            with patch("agents_relay.telegram_adapter.edit_message"):
                process_update(fake_update, config=cfg, on_turn=fake_turn)

        self.assertEqual(received_msg, ["Photo caption question"])


class TestInject(unittest.TestCase):
    def _cfg(self, *, allowed=(12345,), secret="expected"):
        return RelayConfig(
            loop_cmd=("python", "-m", "runner.loop"),
            loop_provider="echo",
            relay_secret=secret,
            telegram_bot_token="fake_bot_token",
            telegram_allowed_chat_ids=allowed,
            relay_host="127.0.0.1",
            relay_port=0,
            telegram_poll_timeout=1,
        )

    def test_inject_and_poll_call_same_handler(self):
        from agents_relay.loop_client import LoopTurnResult
        from agents_relay.telegram_adapter import inject_text, process_update

        cfg = self._cfg()
        fake_result = LoopTurnResult(
            reply="pong", session="s", user_id="u", alias="a", returncode=0, stderr=""
        )
        fake_update = {
            "message": {
                "chat": {"id": 12345},
                "text": "hello from poll",
                "from": {"username": "felix"},
            }
        }
        with patch(
            "agents_relay.telegram_adapter.handle_inbound_text",
            return_value=fake_result,
        ) as mock_h:
            process_update(fake_update, config=cfg)
            inject_text(chat_id=12345, text="hello from inject", config=cfg)

        self.assertEqual(mock_h.call_count, 2)
        poll_kw = mock_h.call_args_list[0].kwargs
        inj_kw = mock_h.call_args_list[1].kwargs
        self.assertEqual(poll_kw["chat_id"], 12345)
        self.assertEqual(poll_kw["text"], "hello from poll")
        self.assertEqual(inj_kw["chat_id"], 12345)
        self.assertEqual(inj_kw["text"], "hello from inject")

    def test_inject_edits_thinking_then_final(self):
        from agents_relay.loop_client import LoopTurnResult
        from agents_relay.telegram_adapter import inject_text

        cfg = self._cfg()
        seen = []

        def fake_turn(*args, **kwargs):
            seen.append(kwargs)
            if kwargs.get("on_status"):
                kwargs["on_status"]("running mcp.calendar.list...")
            return LoopTurnResult(
                reply="Kalender leer.",
                session="ses_1",
                user_id="u_1",
                alias="telegram:12345",
                returncode=0,
                stderr="[*] Running 'mcp.calendar.list'...\n[+] 'mcp.calendar.list' OK (exit 0)",
            )

        with patch(
            "agents_relay.telegram_adapter.send_message",
            return_value={"result": {"message_id": 77}},
        ) as mock_send:
            with patch("agents_relay.telegram_adapter.edit_message") as mock_edit:
                result = inject_text(
                    chat_id=12345, text="was steht diese woche an?", config=cfg, on_turn=fake_turn
                )

        self.assertEqual(result.reply, "Kalender leer.")
        self.assertEqual(seen[0]["channel"], "telegram")
        self.assertEqual(seen[0]["user"], "12345")
        mock_send.assert_called_once()
        self.assertGreaterEqual(mock_edit.call_count, 1)
        self.assertIn("Kalender leer.", mock_edit.call_args[0][3])

    def test_inject_denylist(self):
        from agents_relay.telegram_adapter import inject_text

        with self.assertRaises(PermissionError):
            inject_text(chat_id=99, text="hi", config=self._cfg())

    def test_inject_empty_text(self):
        from agents_relay.telegram_adapter import inject_text

        with self.assertRaises(ValueError):
            inject_text(chat_id=12345, text="   ", config=self._cfg())

    def test_v1_inject_http(self):
        from agents_relay.loop_client import LoopTurnResult

        cfg = self._cfg()

        def fake_turn(**kwargs):
            return LoopTurnResult(
                reply="ok from inject",
                session="ses_i",
                user_id="u_i",
                alias="telegram:12345",
                returncode=0,
                stderr="[+] 'mcp.memory.search' OK (exit 0)",
            )

        server = serve_http(cfg, on_turn=fake_turn)
        host, port = server.server_address
        thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch(
                "agents_relay.telegram_adapter.send_message",
                return_value={"result": {"message_id": 1}},
            ):
                with patch("agents_relay.telegram_adapter.edit_message"):
                    conn = HTTPConnection(host, port, timeout=5)
                    body = json.dumps({"user": 12345, "text": "hi"})
                    conn.request("POST", "/v1/inject", body=body, headers={"Content-Type": "application/json"})
                    self.assertEqual(conn.getresponse().status, 401)

                    conn = HTTPConnection(host, port, timeout=5)
                    conn.request(
                        "POST",
                        "/v1/inject",
                        body=json.dumps({"user": 99, "text": "hi"}),
                        headers={"Content-Type": "application/json", "X-Relay-Secret": "expected"},
                    )
                    self.assertEqual(conn.getresponse().status, 403)

                    conn = HTTPConnection(host, port, timeout=5)
                    conn.request(
                        "POST",
                        "/v1/inject",
                        body=json.dumps({"user": 12345, "text": "  "}),
                        headers={"Content-Type": "application/json", "X-Relay-Secret": "expected"},
                    )
                    self.assertEqual(conn.getresponse().status, 400)

                    conn = HTTPConnection(host, port, timeout=5)
                    conn.request(
                        "POST",
                        "/v1/inject",
                        body=json.dumps({"chat_id": 12345, "text": "hi"}),
                        headers={"Content-Type": "application/json", "X-Relay-Secret": "expected"},
                    )
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    data = json.loads(resp.read().decode())
                    self.assertEqual(data["reply"], "ok from inject")
                    self.assertEqual(data["session"], "ses_i")
                    self.assertTrue(any("Memory" in t for t in data["traces"]))
        finally:
            server.shutdown()
            thread.join(timeout=2)

    def test_cli_inject_and_help_json(self):
        import io
        from agents_relay.__main__ import _help_json
        from agents_relay.__main__ import main as relay_main
        from agents_relay.loop_client import LoopTurnResult

        blob = json.dumps(_help_json())
        self.assertIn("inject", blob)
        self.assertIn("/v1/inject", blob)

        fake = LoopTurnResult(
            reply="cli pong", session="s", user_id="u", alias="a", returncode=0, stderr=""
        )
        buf = io.StringIO()
        with patch("agents_relay.__main__.inject_text", return_value=fake) as mock_inj:
            with patch(
                "agents_relay.__main__.RelayConfig.from_env",
                return_value=self._cfg(),
            ):
                with patch("sys.stdout", buf):
                    rc = relay_main(["inject", "--user", "12345", "--text", "hi"])
        self.assertEqual(rc, 0)
        mock_inj.assert_called_once()
        self.assertEqual(mock_inj.call_args.kwargs["chat_id"], 12345)
        self.assertEqual(mock_inj.call_args.kwargs["text"], "hi")
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["reply"], "cli pong")


if __name__ == "__main__":
    unittest.main()





