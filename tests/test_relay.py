"""Relay unit tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, replace
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.loop_client import LOOP_TRAILER_MARKER, parse_loop_stdout, run_loop_turn
from agents_relay.turn import StreamEvent, iter_loop_stream, turn_params_from_body
from agents_relay.webhooks import handle_webhook

_ISOLATED_TMP = tempfile.TemporaryDirectory()


def setUpModule():
    tmp = _ISOLATED_TMP.name
    os.environ["AGENTS_HOST_DIR"] = os.path.join(tmp, "host")
    os.environ["AGENTS_TRACES_DIR"] = os.path.join(tmp, "traces")


def tearDownModule():
    for key in ("AGENTS_HOST_DIR", "AGENTS_TRACES_DIR"):
        os.environ.pop(key, None)
    _ISOLATED_TMP.cleanup()


def _test_config(**overrides) -> RelayConfig:
    base = dict(
        loop_cmd=(sys.executable, "-c", "print('skip')"),
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


class TestLoopCmdSplit(unittest.TestCase):
    def test_plain_command_splits(self):
        with patch.dict("os.environ", {"LOOP_CMD": "python -m runner.loop"}, clear=False):
            self.assertEqual(
                RelayConfig.from_env().loop_cmd, ("python", "-m", "runner.loop")
            )

    def test_quoted_program_path_survives(self):
        raw = "'C:/Program Files/Python/python.exe' -m klanker.turn --system 'C:/a b/s.txt'"
        with patch.dict("os.environ", {"LOOP_CMD": raw}, clear=False):
            self.assertEqual(
                RelayConfig.from_env().loop_cmd,
                (
                    "C:/Program Files/Python/python.exe",
                    "-m",
                    "klanker.turn",
                    "--system",
                    "C:/a b/s.txt",
                ),
            )

    def test_empty_command_falls_back(self):
        with patch.dict("os.environ", {"LOOP_CMD": "   "}, clear=False):
            self.assertEqual(
                RelayConfig.from_env().loop_cmd, ("python", "-m", "runner.loop")
            )


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

        cfg = _test_config(relay_secret="sekrit")
        with patch("agents_relay.loop_client.subprocess.run", return_value=FakeProc()):
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

    def test_timeout_reply_hides_command(self):
        cfg = RelayConfig(
            loop_cmd=(sys.executable, "-c", "import time; time.sleep(30)"),
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
    def test_v1_turn_auth_and_provider(self):
        cfg = _test_config()
        turn_calls = []

        def fake_turn(**kwargs):
            from agents_relay.loop_client import LoopTurnResult

            turn_calls.append(kwargs)
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
            self.assertEqual(turn_calls[0].get("provider"), "echo")
            self.assertEqual(turn_calls[0].get("persona"), "default")

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

    def test_process_update_replaces_thinking_with_reply(self):
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
                "from": {"username": "alice"},
            }
        }
        with patch("agents_relay.telegram_adapter.send_message", return_value={"result": {"message_id": 99}}) as mock_send:
            with patch("agents_relay.telegram_adapter.edit_message"), \
                    patch("agents_relay.telegram_adapter.delete_message") as mock_delete:
                def fake_turn(*args, **kwargs):
                    if "on_status" in kwargs and kwargs["on_status"]:
                        kwargs["on_status"]("running mcp.terminal...")
                    return LoopTurnResult(
                        reply="Echo: test message",
                        session="ses_123",
                        user_id="u_123",
                        alias="telegram:alice",
                        returncode=0,
                        stderr="[*] Running 'mcp.terminal'...\n[+] 'mcp.terminal' OK (exit 0)",
                    )

                process_update(fake_update, config=cfg, on_turn=fake_turn)
                mock_delete.assert_called_once_with("fake_bot_token", 12345, 99)
                self.assertIn("Echo: test message", mock_send.call_args[0][2])



class TestTelegramFormat(unittest.TestCase):
    def test_digest_refs_never_reach_telegram(self):
        from agents_relay.telegram_format import (
            format_telegram_html,
            split_telegram_html,
            visible_reply,
        )

        reply = visible_reply('analysis…\n<ref digest="deadbeef0123" chars="9000"/>')
        self.assertNotIn("digest", reply)
        self.assertIn("analysis", reply)
        reply = visible_reply(
            'only <stdio evicted="1" truncated="1" digest="cafe" chars="5000"/> here'
        )
        self.assertNotIn("digest", reply)
        long_html = format_telegram_html(
            "para one.\n\n" + "x" * 9000 + '\n<ref digest="d1" chars="8000"/>'
        )
        parts = split_telegram_html(long_html)
        self.assertGreater(len(parts), 1)
        for part in parts:
            from agents_relay.telegram_format import utf16_len

            self.assertLessEqual(utf16_len(part), 4096)
            self.assertNotIn("digest", part)

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
        self.assertNotIn("(empty reply)", formatted)

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
        self.assertNotIn("(empty reply)", formatted)

    def test_urls_with_query_params(self):
        from agents_relay.telegram_format import format_telegram_html

        text = "Check [API Docs](https://api.lolax.dev/v1/search?q=test&lang=de#intro) now!"
        formatted = format_telegram_html(text)
        self.assertIn('<a href="https://api.lolax.dev/v1/search?q=test&amp;lang=de#intro">API Docs</a>', formatted)

    def test_model_json_dump_stripped(self):
        from agents_relay.telegram_format import visible_reply

        tool_json = '{"name": "call_job", "arguments": {"catalog": "mcp.memory.search", "query": "test"}}'
        self.assertEqual(visible_reply(tool_json), "(empty reply)")

        tool_fenced = '```json\n{"tool_call": "search", "arguments": {"q": "test"}}\n```'
        self.assertEqual(visible_reply(tool_fenced), "(empty reply)")

    def test_normal_json_in_text_preserved(self):
        from agents_relay.telegram_format import visible_reply

        user_code = 'In Python kannst du ein Dict definieren: `{"user": "Alice", "score": 10}`.'
        self.assertIn('{"user": "Alice", "score": 10}', visible_reply(user_code))

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
        self.assertEqual(visible_reply("", ()), "(empty reply)")

    def test_long_message_budget(self):
        import re

        from agents_relay.telegram_format import format_telegram_html, split_telegram_html, utf16_len

        long_text = "A" * 5000
        formatted = format_telegram_html(long_text)
        self.assertEqual(formatted.count("A"), 5000)
        self.assertFalse(formatted.endswith("..."))
        parts = split_telegram_html(formatted)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(utf16_len(part), 4096)
        joined = "".join(re.sub(r"<[^>]+>", "", part) for part in parts)
        self.assertEqual(joined.count("A"), 5000)

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

        # When body is present, intermediate failures are suppressed from the final message
        raw_with_body = (
            "[*] Running 'tool' (check <stdin> & <stdout>)...\n"
            "[-] 'mcp.memory.add' FAILED: expected exit 0, got 2\n"
            "Done!"
        )
        formatted = format_telegram_html(raw_with_body)
        self.assertNotIn("Memory failed", formatted)
        self.assertIn("Done!", formatted)

        # When NO body is present, fallback humanized failure is preserved
        raw_no_body = (
            "[*] Running 'tool' (check <stdin> & <stdout>)...\n"
            "[-] 'mcp.memory.add' FAILED: expected exit 0, got 2\n"
        )
        formatted_no_body = format_telegram_html(raw_no_body)
        self.assertIn("Memory failed (exit 2).", formatted_no_body)

    def test_new_and_reset_command_forwards_new_session(self):
        from agents_relay.loop_client import LoopTurnResult
        from agents_relay.telegram_adapter import handle_inbound_text

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
        recorded = []

        def fake_turn(*args, **kwargs):
            recorded.append(kwargs)
            return LoopTurnResult(
                reply="Neu gestartet.",
                session="ses_new",
                user_id="u_1",
                alias="telegram:12345",
                returncode=0,
                stderr="",
            )

        with patch("agents_relay.telegram_adapter.send_message", return_value={"ok": True}):
            handle_inbound_text(chat_id=12345, text="/new", config=cfg, on_turn=fake_turn)
            handle_inbound_text(chat_id=12345, text="/reset Start working", config=cfg, on_turn=fake_turn)

        self.assertEqual(len(recorded), 2)
        self.assertTrue(recorded[0]["new_session"])
        self.assertEqual(recorded[0]["message"], "Hallo")
        self.assertTrue(recorded[1]["new_session"])
        self.assertEqual(recorded[1]["message"], "Start working")


    def test_humanize_status_calendar_progress(self):
        from agents_relay.telegram_format import humanize_status, status_html

        running = (
            "[*] Running 'mcp.calendar.list' (rests on: agents-calendar CLI list "
            "VEVENTs; extra argv is --from/--to ISO; exit 0 = listed)..."
        )
        self.assertEqual(humanize_status(running), "Calendar (mcp.calendar.list) …")
        self.assertEqual(
            humanize_status("[+] 'mcp.calendar.list' OK (exit 0) in 2.21s"),
            "Calendar done.",
        )
        self.assertEqual(
            humanize_status("[-] 'mcp.memory.add' FAILED: expected exit 0, got 2"),
            "Memory failed (exit 2).",
        )
        self.assertEqual(humanize_status("CMD: python -m agents_calendar list"), "")
        with patch.dict("os.environ", {"AGENTS_RELAY_WAIT_TEXT": ""}):
            self.assertEqual(humanize_status("thinking..."), "One moment …")
            self.assertIn("One moment", status_html("thinking..."))
        with patch.dict("os.environ", {"AGENTS_RELAY_WAIT_TEXT": "Einen Moment …"}):
            self.assertEqual(humanize_status("thinking..."), "Einen Moment …")
            self.assertEqual(status_html(""), "<i>Einen Moment …</i>")
        self.assertNotIn("[*]", status_html(running))

    def test_humanize_status_drops_machine_noise(self):
        from agents_relay.telegram_format import humanize_status

        self.assertEqual(
            humanize_status("/usr/local/lib/python3.12/site-packages/klanker/turn.py:44: "
                            "UserWarning: domain env overrides are test/debug only"),
            "",
        )
        self.assertEqual(humanize_status('  File "/x/runner/loop.py", line 10, in run'), "")
        self.assertEqual(humanize_status("Traceback (most recent call last):"), "")
        self.assertEqual(humanize_status("ValueError: unknown workspace"), "")

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
        self.assertNotIn("(empty reply)", formatted)
        self.assertIn("Calendar", formatted)

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
                "from": {"username": "alice"},
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
                "from": {"username": "alice"},
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

    def test_inject_deletes_thinking_then_final(self):
        from agents_relay.loop_client import LoopTurnResult
        from agents_relay.telegram_adapter import inject_text

        cfg = self._cfg()
        seen = []

        def fake_turn(*args, **kwargs):
            seen.append(kwargs)
            if kwargs.get("on_status"):
                kwargs["on_status"]("running mcp.calendar.list...")
            return LoopTurnResult(
                reply="Calendar empty.",
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
            with patch("agents_relay.telegram_adapter.edit_message"), \
                    patch("agents_relay.telegram_adapter.delete_message") as mock_delete:
                result = inject_text(
                    chat_id=12345, text="was steht diese woche an?", config=cfg, on_turn=fake_turn
                )

        self.assertEqual(result.reply, "Calendar empty.")
        self.assertEqual(seen[0]["channel"], "telegram")
        self.assertEqual(seen[0]["user"], "12345")
        mock_delete.assert_called_once_with("fake_bot_token", 12345, 77)
        self.assertIn("Calendar empty.", mock_send.call_args[0][2])

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


    def test_v1_inject_webhook_query_auth_and_async(self):
        import time
        from agents_relay.loop_client import LoopTurnResult

        cfg = self._cfg()
        seen_messages = []

        def fake_turn(**kwargs):
            seen_messages.append(kwargs.get("message", ""))
            return LoopTurnResult(
                reply="diagnosed",
                session="ses_w",
                user_id="u_w",
                alias="telegram:12345",
                returncode=0,
                stderr="",
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
                    # 1. Query secret + omitted chat_id (falls back to primary allowlisted chat) + structured webhook JSON
                    conn = HTTPConnection(host, port, timeout=5)
                    webhook_body = json.dumps({
                        "message": "Deployment failed",
                        "event": "deployment_failed",
                        "application_name": "demo-app",
                        "url": "https://panel.example.com/deploy/1",
                    })
                    conn.request(
                        "POST",
                        "/v1/inject?prefix=Ops+Alert",
                        body=webhook_body,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": "Bearer expected",
                        },
                    )
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    data = json.loads(resp.read().decode())
                    self.assertEqual(data["reply"], "diagnosed")
                    self.assertEqual(len(seen_messages), 1)
                    self.assertIn("Ops Alert", seen_messages[0])
                    self.assertIn("Deployment failed", seen_messages[0])
                    self.assertIn("application_name: demo-app", seen_messages[0])

                    # 2. Async mode (?async=1) returns 202 immediately and runs on worker thread
                    conn = HTTPConnection(host, port, timeout=5)
                    conn.request(
                        "POST",
                        "/v1/inject?async=1",
                        body=json.dumps({"message": "Container restarted", "service": "api"}),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": "Bearer expected",
                        },
                    )
                    resp2 = conn.getresponse()
                    self.assertEqual(resp2.status, 202)
                    data2 = json.loads(resp2.read().decode())
                    self.assertTrue(data2["queued"])
                    self.assertEqual(data2["chat_id"], 12345)
                    for _ in range(20):
                        if len(seen_messages) >= 2:
                            break
                        time.sleep(0.05)
                    self.assertEqual(len(seen_messages), 2)
                    self.assertIn("service: api", seen_messages[1])
        finally:
            server.shutdown()
            thread.join(timeout=2)


class TestWebhook(unittest.TestCase):
    def test_webhook_auth(self):
        import hashlib, hmac
        cfg = _test_config()
        bad = handle_webhook("ci", {"status": "failed"}, raw_body=b"{}", secret_header="nope", config=cfg, notify_turn=False)
        self.assertFalse(bad["ok"])
        missing_hdr = handle_webhook("ci", {"status": "failed"}, raw_body=b"{}", secret_header="", config=cfg, notify_turn=False)
        self.assertFalse(missing_hdr["ok"])
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
        sig = hmac.new(b"whsec", b"{}", hashlib.sha256).hexdigest()
        signed = handle_webhook(
            "ci", {"status": "failed"}, raw_body=b"{}", secret_header=f"sha256={sig}", config=cfg, notify_turn=False
        )
        self.assertTrue(signed["ok"])

    def test_webhook_fails_closed_when_secret_unset(self):
        for empty in ("", "   "):
            cfg = _test_config(webhook_secret=empty)
            res = handle_webhook("ci", {"status": "failed"}, raw_body=b"{}", secret_header="", config=cfg, notify_turn=False)
            self.assertEqual(res, {"ok": False, "error": "unauthorized"})
            res2 = handle_webhook("ci", {"status": "failed"}, raw_body=b"{}", secret_header="anything", config=cfg, notify_turn=False)
            self.assertEqual(res2, {"ok": False, "error": "unauthorized"})


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


class TestDashboardApi(unittest.TestCase):
    """Board agents-relay#12: /v1/traces, /v1/approvals, RELAY_CORS_ORIGINS."""

    def _serve(self, cfg: RelayConfig) -> tuple[str, int]:
        server = serve_http(cfg)
        host, port = server.server_address  # type: ignore[misc]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(thread.join, 2)
        return host, port

    def _req(self, host: str, port: int, method: str, path: str,
             body: dict | None = None, headers: dict | None = None,
             secret: bool = True):
        conn = HTTPConnection(host, port, timeout=10)
        h = dict(headers or {})
        if body is not None:
            h.setdefault("Content-Type", "application/json")
        if secret:
            h.setdefault("X-Relay-Secret", "expected")
        conn.request(
            method, path,
            body=json.dumps(body) if body is not None else None,
            headers=h,
        )
        return conn.getresponse()

    def _workspace(self, ws_id: str = "demo"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        host_dir = Path(tmp.name) / "host"
        self._host_dir = host_dir
        env = patch.dict(
            os.environ,
            {
                "AGENTS_HOST_DIR": str(host_dir),
                "AGENTS_WORKSPACES_DIR": str(host_dir / "workspaces"),
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        from agents_workspace import Workspace

        return Workspace.create(ws_id)

    def test_traces_tail_and_session_listing(self):
        ws = self._workspace()
        events = [
            {"ts": "2026-01-01T00:00:01Z", "session": "ses_a", "type": "message"},
            {"ts": "2026-01-01T00:00:02Z", "session": "ses_b", "type": "message"},
            {"ts": "2026-01-01T00:00:03Z", "session": "ses_a", "type": "tool_call",
             "tool": "shell"},
        ]
        ws.traces.mkdir(parents=True, exist_ok=True)
        (ws.traces / "2026-01-01.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events) + "not-json\n",
            encoding="utf-8")
        host, port = self._serve(_test_config())

        resp = self._req(
            host, port, "GET",
            "/v1/traces?workspace=demo&persona=default&session=ses_a&limit=10")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode())
        self.assertEqual(len(data["lines"]), 2)
        self.assertTrue(
            all(json.loads(line)["session"] == "ses_a" for line in data["lines"]))
        self.assertEqual(data["note"], "")

        resp = self._req(
            host, port, "GET", "/v1/traces?workspace=demo&session=ses_a&limit=1")
        data = json.loads(resp.read().decode())
        self.assertEqual(len(data["lines"]), 1)
        self.assertEqual(json.loads(data["lines"][0])["type"], "tool_call")

        # no session -> list that persona's on-disk session ids
        sdir = ws.personas / "default" / "sessions"
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "ses_a.json").write_text("{}", encoding="utf-8")
        resp = self._req(
            host, port, "GET", "/v1/traces?workspace=demo&persona=default")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode())
        self.assertEqual(data["sessions"], ["ses_a"])
        self.assertEqual(data["lines"], [])

    def test_traces_rejects_traversal(self):
        self._workspace()
        host, port = self._serve(_test_config())
        for path in (
            "/v1/traces?workspace=..%2Fevil",
            "/v1/traces?workspace=demo&persona=..%2Fevil",
            "/v1/traces?workspace=demo&session=..%2Fevil",
        ):
            resp = self._req(host, port, "GET", path)
            self.assertEqual(resp.status, 400, path)
            resp.read()

    def test_dashboard_routes_require_secret(self):
        host, port = self._serve(_test_config())
        for method, path in (
            ("GET", "/v1/traces"),
            ("GET", "/v1/approvals"),
            ("POST", "/v1/approvals/ap1"),
        ):
            resp = self._req(host, port, method, path, secret=False)
            self.assertEqual(resp.status, 401, path)
            resp.read()

    def test_approvals_list_and_resolve(self):
        ws = self._workspace()
        cfg = _test_config()
        from agents_relay.approvals import create_pending
        from agents_relay.state import read_json

        ws_cfg = replace(cfg, state_dir=str(ws.relay_state))
        create_pending(ws_cfg, 0, {"id": "ap1", "tool": "shell",
                                   "summary": "run tests"}, workspace="demo")
        create_pending(ws_cfg, 0, {"id": "ap2", "tool": "write",
                                   "summary": "edit file"}, workspace="demo")
        host, port = self._serve(cfg)

        resp = self._req(host, port, "GET", "/v1/approvals?workspace=demo")
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode())
        self.assertEqual([a["id"] for a in data["approvals"]], ["ap1", "ap2"])
        self.assertEqual(data["approvals"][0]["tool"], "shell")
        self.assertEqual(data["approvals"][0]["summary"], "run tests")
        self.assertFalse((ws.relay_state / "approvals" / "ap1.json").exists())

        resp = self._req(
            host, port, "POST", "/v1/approvals/ap1?workspace=demo",
            {"allow": "yes"})
        self.assertEqual(resp.status, 400)
        resp.read()

        resp = self._req(
            host, port, "POST", "/v1/approvals/ap1?workspace=demo",
            {"allow": True})
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode())
        self.assertEqual(data["status"], "approved")
        rec = read_json(self._host_dir / "run" / "approvals" / "ap1.json")
        self.assertEqual(rec["status"], "approved")

        resp = self._req(host, port, "GET", "/v1/approvals?workspace=demo")
        data = json.loads(resp.read().decode())
        self.assertEqual([a["id"] for a in data["approvals"]], ["ap2"])

        # already decided -> 404; deny path writes the same decision file
        resp = self._req(
            host, port, "POST", "/v1/approvals/ap1?workspace=demo",
            {"allow": False})
        self.assertEqual(resp.status, 404)
        resp.read()
        resp = self._req(
            host, port, "POST", "/v1/approvals/ap2?workspace=demo",
            {"allow": False})
        self.assertEqual(resp.status, 200)
        resp.read()
        rec = read_json(self._host_dir / "run" / "approvals" / "ap2.json")
        self.assertEqual(rec["status"], "denied")

    def test_http_turn_approval_request_wait_and_token_lifecycle(self):
        from agents_relay.approvals import (
            hash_approval_token,
            mint_approval_token,
            revoke_approval_token,
            run_http_approve,
        )
        from agents_relay.state import read_json
        from agents_workspace import Host

        ws = self._workspace("demo")
        Host.from_env().create("other")
        cfg = _test_config(
            telegram_bot_token="bot-tok",
            telegram_allowed_chat_ids=(12345,),
            approver="12345",
        )
        host, port = self._serve(cfg)
        token, thash = mint_approval_token("demo")
        other_token, _ = mint_approval_token("other")
        self.assertTrue((self._host_dir / "run" / "approvals" / "tokens" / f"{thash}.json").is_file())

        # Missing or invalid token -> 401
        r = self._req(host, port, "POST", "/v1/approvals/request",
                      {"id": "req-unauth", "tool": "shell", "summary": "ls"}, secret=False)
        self.assertEqual(r.status, 401)
        r.read()
        r = self._req(host, port, "POST", "/v1/approvals/request",
                      {"id": "req-bad", "tool": "shell", "summary": "ls"},
                      headers={"Authorization": "Bearer wrong-token"}, secret=False)
        self.assertEqual(r.status, 401)
        r.read()

        # Cross-workspace request -> 403
        r = self._req(host, port, "POST", "/v1/approvals/request",
                      {"id": "req-xws", "workspace": "other", "tool": "shell", "summary": "ls"},
                      headers={"Authorization": f"Bearer {token}"}, secret=False)
        self.assertEqual(r.status, 403)
        r.read()

        # Full round-trip with Telegram dispatch + operator WebUI decision
        req_payload = {
            "id": "req-http-1",
            "session": "ses_1",
            "user": "12345",
            "workspace": "demo",
            "tool": "mcp.terminal.run",
            "args": {"cmd": "ls"},
            "summary": "run ls",
            "timeout": 5,
        }
        with patch("agents_relay.telegram_adapter.send_message", return_value={"ok": True}) as tg_sent:
            holder: dict[str, object] = {}

            def _waiter() -> None:
                code, note = run_http_approve(
                    f"http://{host}:{port}", token, req_payload, 5.0,
                    workspace="demo", poll_slice=0.5,
                )
                holder["code"] = code
                holder["note"] = note

            th = threading.Thread(target=_waiter, daemon=True)
            th.start()

            ap_file = self._host_dir / "run" / "approvals" / "req-http-1.json"
            deadline = time.time() + 3
            while time.time() < deadline and not ap_file.is_file():
                time.sleep(0.02)
            self.assertTrue(ap_file.is_file())
            self.assertFalse((ws.relay_state / "approvals" / "req-http-1.json").exists())
            rec = read_json(ap_file)
            self.assertEqual(rec["workspace"], "demo")
            self.assertEqual(rec["token_hash"], hash_approval_token(token))
            deadline = time.time() + 3
            while time.time() < deadline and not tg_sent.called:
                time.sleep(0.02)
            tg_sent.assert_called()

            # Different turn's token cannot wait on this approval id -> 403
            r_other = self._req(
                host, port, "GET", "/v1/approvals/req-http-1/wait?timeout=1",
                headers={"Authorization": f"Bearer {other_token}"}, secret=False,
            )
            self.assertEqual(r_other.status, 403)
            r_other.read()

            # Wrong workspace on operator POST -> 403
            r_wrong_ws = self._req(
                host, port, "POST", "/v1/approvals/req-http-1?workspace=other",
                {"allow": True},
            )
            self.assertEqual(r_wrong_ws.status, 403)
            r_wrong_ws.read()

            # Operator resolves on right workspace -> wait thread unblocks
            r_ok = self._req(
                host, port, "POST", "/v1/approvals/req-http-1?workspace=demo",
                {"allow": True},
            )
            self.assertEqual(r_ok.status, 200)
            r_ok.read()
            th.join(timeout=3)
            self.assertEqual(holder.get("code"), 0)
            self.assertEqual(holder.get("note"), "approved")

        # Timeout finalization + token revocation
        code_to, note_to = run_http_approve(
            f"http://{host}:{port}", token,
            {"id": "req-http-timeout", "tool": "shell", "summary": "slow", "user": "local"},
            0.25, workspace="demo", poll_slice=0.25,
        )
        self.assertEqual((code_to, note_to), (2, "timeout"))
        self.assertEqual(
            read_json(self._host_dir / "run" / "approvals" / "req-http-timeout.json")["status"],
            "timeout",
        )

        revoke_approval_token(token)
        self.assertFalse((self._host_dir / "run" / "approvals" / "tokens" / f"{thash}.json").exists())
        r_revoked = self._req(
            host, port, "POST", "/v1/approvals/request",
            {"id": "req-after-revoke", "workspace": "demo", "tool": "shell", "summary": "x"},
            headers={"Authorization": f"Bearer {token}"}, secret=False,
        )
        self.assertEqual(r_revoked.status, 401)
        r_revoked.read()

    def test_turn_approval_env_lifecycle_and_telegram_isolation(self):
        from agents_relay.approvals import hash_approval_token, verify_approval_token
        from agents_relay.turn import _container_rewrite, turn_approval_env

        self._workspace("demo")
        cfg = replace(_test_config(relay_port=8787), relay_url="http://127.0.0.1:8787")
        seen_tok = ""
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "top-secret-tg"}, clear=False):
            os.environ.pop("AGENTS_RELAY_URL", None)
            os.environ.pop("AGENTS_APPROVAL_TOKEN", None)
            with turn_approval_env("demo", config=cfg) as extra:
                seen_tok = extra["AGENTS_APPROVAL_TOKEN"]
                self.assertEqual(extra["AGENTS_RELAY_URL"], "http://127.0.0.1:8787")
                self.assertTrue(len(seen_tok) >= 32)
                self.assertIsNotNone(verify_approval_token(seen_tok, workspace="demo"))
                tok_file = self._host_dir / "run" / "approvals" / "tokens" / f"{hash_approval_token(seen_tok)}.json"
                self.assertTrue(tok_file.is_file())
                _, child_env, _ = _container_rewrite(["echo"], "demo", "", extra_env=extra)
                assert child_env is not None
                self.assertNotIn("TELEGRAM_BOT_TOKEN", child_env)
                self.assertEqual(child_env["AGENTS_APPROVAL_TOKEN"], seen_tok)
                self.assertEqual(child_env["AGENTS_RELAY_URL"], "http://127.0.0.1:8787")
            self.assertIsNone(verify_approval_token(seen_tok, workspace="demo"))
            self.assertFalse((self._host_dir / "run" / "approvals" / "tokens" / f"{hash_approval_token(seen_tok)}.json").exists())

    def test_cors_off_by_default(self):
        self._workspace()
        host, port = self._serve(_test_config())
        origin = {"Origin": "https://dash.example.com"}
        resp = self._req(
            host, port, "GET", "/v1/traces?workspace=demo&session=ses_a",
            headers=origin)
        self.assertEqual(resp.status, 200)
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        resp.read()
        resp = self._req(
            host, port, "OPTIONS", "/v1/turn", headers=origin, secret=False)
        self.assertEqual(resp.status, 404)
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        resp.read()

    def test_cors_allowlist_and_preflight(self):
        self._workspace()
        host, port = self._serve(
            _test_config(cors_origins=("https://dash.example.com",)))
        origin = {"Origin": "https://dash.example.com"}
        resp = self._req(
            host, port, "GET", "/v1/traces?workspace=demo&session=ses_a",
            headers=origin)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"),
                         "https://dash.example.com")
        resp.read()

        # /health echoes the allowlist — dashboards poll it cross-origin
        resp = self._req(
            host, port, "GET", "/health", headers=origin, secret=False)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"),
                         "https://dash.example.com")
        resp.read()

        # other non-/v1 routes stay clean even for an allowed origin
        resp = self._req(
            host, port, "GET", "/", headers=origin, secret=False)
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        resp.read()

        # unlisted origin gets no CORS headers
        resp = self._req(
            host, port, "GET", "/v1/traces?workspace=demo&session=ses_a",
            headers={"Origin": "https://evil.example.com"})
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        resp.read()

        resp = self._req(
            host, port, "OPTIONS", "/v1/turn",
            headers={**origin,
                     "Access-Control-Request-Headers": "x-relay-secret, content-type"},
            secret=False)
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"),
                         "https://dash.example.com")
        self.assertIn("GET", resp.getheader("Access-Control-Allow-Methods"))
        self.assertIn("x-relay-secret",
                      resp.getheader("Access-Control-Allow-Headers"))
        resp.read()

        resp = self._req(
            host, port, "OPTIONS", "/v1/turn",
            headers={"Origin": "https://evil.example.com"}, secret=False)
        self.assertEqual(resp.status, 404)
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        resp.read()

    def test_cors_tauri_origin_implicit(self):
        # Tauri webview origins are always echoed on /v1/* so the bundled
        # desktop shell works against any relay, adopted or spawned.
        self._workspace()
        host, port = self._serve(_test_config())
        for tauri_origin in (
                "tauri://localhost", "http://tauri.localhost"):
            resp = self._req(
                host, port, "GET", "/v1/traces?workspace=demo&session=ses_a",
                headers={"Origin": tauri_origin})
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Access-Control-Allow-Origin"),
                             tauri_origin)
            resp.read()
            resp = self._req(
                host, port, "OPTIONS", "/v1/turn",
                headers={"Origin": tauri_origin}, secret=False)
            self.assertEqual(resp.status, 204)
            resp.read()

    def test_cors_origins_from_env(self):
        with patch.dict(
                "os.environ",
                {"RELAY_CORS_ORIGINS": "https://a.example.com, https://b.example.com"},
                clear=False):
            self.assertEqual(
                RelayConfig.from_env().cors_origins,
                ("https://a.example.com", "https://b.example.com"),
            )
        with patch.dict("os.environ", {"RELAY_CORS_ORIGINS": ""}, clear=False):
            self.assertEqual(RelayConfig.from_env().cors_origins, ())

class TestMemberRoleAuthz(unittest.TestCase):
    """ws members.json grants chat access before the env allowlist."""

    def setUp(self):
        from pathlib import Path as _P

        self._tmp = tempfile.TemporaryDirectory()
        root = _P(self._tmp.name)
        self._env = patch.dict(
            os.environ,
            {
                "AGENTS_HOST_DIR": str(root / "host"),
                "AGENTS_WORKSPACE": "demo",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)
        from agents_workspace import Host

        self.ws = Host(dir=root / "host").create("demo")

    def _cfg(self, allowed=()):
        return _test_config(telegram_allowed_chat_ids=allowed)

    def test_member_chat_allowed(self):
        from agents_workspace.members import write_members
        from agents_relay.telegram_adapter import chat_allowed

        write_members(self.ws, [{"id": "999", "role": "member"}])
        self.assertTrue(chat_allowed(999, self._cfg(allowed=())))

    def test_nonmember_falls_back_to_env_list(self):
        from agents_relay.telegram_adapter import chat_allowed

        self.assertFalse(chat_allowed(999, self._cfg(allowed=())))
        self.assertTrue(chat_allowed(999, self._cfg(allowed=(999,))))


class TestWave1SecurityHardening(unittest.TestCase):
    def test_v1_rejects_query_param_secret_and_token(self):
        cfg = _test_config()
        srv = serve_http(cfg)
        host, port = srv.server_address  # type: ignore[misc]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            for method, path, body in (
                ("GET", "/v1/workspaces?token=expected", None),
                ("GET", "/v1/workspaces?secret=expected", None),
                ("POST", "/v1/inject?secret=expected", json.dumps({"message": "hi"})),
            ):
                conn = HTTPConnection(host, port, timeout=5)
                hdrs = {"Content-Type": "application/json"} if body else {}
                conn.request(method, path, body=body, headers=hdrs)
                resp = conn.getresponse()
                resp.read()
                self.assertEqual(resp.status, 401, (method, path))
        finally:
            srv.shutdown()
            srv.server_close()
            th.join(timeout=2)

    def test_redact_log_text_hides_query_secrets(self):
        from agents_relay.http_adapter import _redact_log_text

        self.assertEqual(
            _redact_log_text("GET /?token=supersecret&x=1 HTTP/1.1"),
            "GET /?token=[REDACTED]&x=1 HTTP/1.1",
        )
        self.assertEqual(
            _redact_log_text("POST /v1/turn?a=1&secret=topsecret HTTP/1.1"),
            "POST /v1/turn?a=1&secret=[REDACTED] HTTP/1.1",
        )

    def test_gateway_client_prefs_and_loopback_bearer(self):
        from agents_relay.client import GatewayClient
        from agents_workspace import Host, write_secret

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prefs = root / "prefs.json"
            prefs.write_text(
                json.dumps(
                    {
                        "base_url": "http://127.0.0.1:9999",
                        "secret": "legacy-plaintext-secret",
                        "workspace": "stale-ws",
                        "provider": "echo",
                    }
                ),
                encoding="utf-8",
            )
            client = GatewayClient(
                base_url="http://127.0.0.1:8787",
                workspace="cli-ws",
                prefs_path=prefs,
            )
            client.load_prefs(explicit={"base_url", "workspace"})
            self.assertEqual(client.secret, "")
            self.assertEqual(client.base_url, "http://127.0.0.1:8787")
            self.assertEqual(client.workspace, "cli-ws")
            self.assertEqual(client.provider, "echo")

            client.secret = "ephemeral-secret"
            client._save_prefs()
            saved = json.loads(prefs.read_text(encoding="utf-8"))
            self.assertNotIn("secret", saved)

            host_dir = root / "host"
            Host.open(host_dir).create("demo")
            write_secret("RELAY_WEB_TOKEN", "vault-web-tok", host=Host.open(host_dir), scope="host")
            with patch.dict(os.environ, {"AGENTS_HOST_DIR": str(host_dir)}, clear=False):
                auto = GatewayClient(base_url="http://127.0.0.1:8787")
                hdrs = auto._headers()
                self.assertEqual(hdrs.get("Authorization"), "Bearer vault-web-tok")
                remote = GatewayClient(base_url="https://remote.example")
                self.assertNotIn("Authorization", remote._headers())


if __name__ == "__main__":
    unittest.main()


class TestSymmetricAuthAndHealth(unittest.TestCase):
    """agents-relay#29: Bearer via RELAY_WEB_TOKEN, host_id on /health."""

    def _serve(self, cfg: RelayConfig) -> tuple[str, int]:
        server = serve_http(cfg)
        host, port = server.server_address  # type: ignore[misc]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(thread.join, 2)
        return host, port

    def test_bearer_via_web_token_only(self):
        # cfg built as if only RELAY_WEB_TOKEN was set (symmetric fill).
        cfg = _test_config(relay_secret="tok123", web_token="tok123")
        host, port = self._serve(cfg)
        conn = HTTPConnection(host, port, timeout=10)
        conn.request("GET", "/v1/workspaces",
                     headers={"Authorization": "Bearer tok123"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 200)
        # Falsifying: no auth -> 401.
        conn = HTTPConnection(host, port, timeout=10)
        conn.request("GET", "/v1/workspaces")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 401)

    def test_health_has_host_id_no_path_leak(self):
        import tempfile
        from unittest.mock import patch
        from agents_workspace import Host

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENTS_HOST_DIR": tmp}):
                expected_hid = Host.open(tmp).host_id()
                host, port = self._serve(_test_config())
                conn = HTTPConnection(host, port, timeout=10)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                data = json.loads(resp.read().decode())
                self.assertEqual(resp.status, 200)
                self.assertEqual(data["host_id"], expected_hid)
                self.assertEqual(data["service"], "agents-relay")
                self.assertEqual(data["port"], port)
                raw = json.dumps(data)
                self.assertNotIn(tmp, raw)
                self.assertNotIn('"dir"', raw)
