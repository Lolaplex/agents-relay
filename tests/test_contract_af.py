"""Contract sections A and F: approval gate, concurrency, splits, attachments."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.jobs import JobRegistry, kill_process_tree, reset_registry
from agents_relay.loop_client import LoopTurnResult
from agents_relay.telegram_format import split_telegram_html, utf16_len


def _result(reply: str = "ok") -> LoopTurnResult:
    return LoopTurnResult(reply=reply, session="s", user_id="u", alias="a", returncode=0, stderr="")


def _cfg(
    tmp: Path,
    *,
    allowed: tuple[int, ...] = (12345,),
    token: str = "fake-token",
    allow_anyone: bool = False,
    max_jobs: int = 8,
    max_jobs_per_chat: int = 3,
    approver: str = "",
    attach_dir: str = "",
) -> RelayConfig:
    return RelayConfig(
        loop_cmd=(sys.executable, "-c", "print('skip')"),
        loop_provider="echo",
        relay_secret="expected",
        telegram_bot_token=token,
        telegram_allowed_chat_ids=allowed,
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
        allow_anyone=allow_anyone,
        state_dir=str(tmp),
        max_jobs=max_jobs,
        max_jobs_per_chat=max_jobs_per_chat,
        approver=approver,
        attach_dir=attach_dir,
    )


def _message(chat_id: int, text: str, **extra) -> dict:
    body = {"chat": {"id": chat_id}, "text": text, "from": {"id": chat_id, "username": "alice"}}
    body.update(extra)
    return {"message": body}


class TestSplitHtml(unittest.TestCase):
    def test_paragraph_boundary_and_tag_balance(self):
        body = "para-one\n\n" + ("word " * 800) + "\n\npara-two"
        html = "<b>" + body + "</b>"
        parts = split_telegram_html(html, limit=500)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(utf16_len(part), 500)
            self.assertEqual(part.count("<b>"), part.count("</b>"))
        visible = "".join(re.sub(r"<[^>]+>", "", part) for part in parts)
        self.assertEqual(visible, body)
        self.assertTrue(parts[0].endswith("</b>"))
        self.assertIn("para-one", parts[0])
        self.assertIn("para-two", parts[-1])
        self.assertNotIn("para-two", parts[0])

    def test_code_tag_reopened(self):
        html = "<code>" + ("a" * 2500) + "</code>"
        parts = split_telegram_html(html, limit=1000)
        self.assertGreater(len(parts), 1)
        visible = "".join(re.sub(r"<[^>]+>", "", part) for part in parts)
        self.assertEqual(visible, "a" * 2500)
        for part in parts:
            self.assertLessEqual(utf16_len(part), 1000)
            self.assertTrue(part.startswith("<code>"))
            self.assertTrue(part.endswith("</code>"))

    def test_emoji_uses_utf16_units(self):
        text = "\U0001F600" * 300
        parts = split_telegram_html(text, limit=100)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(utf16_len(part), 100)
        self.assertEqual("".join(parts), text)

    def test_entity_not_split(self):
        text = ("A" * 50) + "&amp;" + ("B" * 50)
        parts = split_telegram_html(text, limit=53)
        self.assertTrue(any("&amp;" in part for part in parts))
        for part in parts:
            self.assertNotRegex(part, r"&(?!amp;)[^;]*$")
            self.assertFalse(part.startswith("amp;"))
        self.assertEqual("".join(parts), text)

    def test_href_entity_stays_inside_the_tag(self):
        link = '<a href="https://example.com/q?a=1&amp;b=2">' + ("A" * 40) + "</a>"
        parts = split_telegram_html(link, limit=70)
        self.assertGreater(len(parts), 1)
        visible = "".join(re.sub(r"<[^>]+>", "", part) for part in parts)
        self.assertEqual(visible, "A" * 40)
        for part in parts:
            self.assertLessEqual(utf16_len(part), 70)
            self.assertNotRegex(part, r"<a\b[^>]*$")
            self.assertEqual(part.count("<a "), part.count("</a>"))

    def test_control_command_parse(self):
        from agents_relay.telegram_adapter import parse_control_command

        self.assertEqual(parse_control_command("/stop@klanker_bot abc"), ("stop", "abc"))
        self.assertEqual(parse_control_command("/jobs"), ("jobs", ""))
        self.assertIsNone(parse_control_command("/new session"))
        self.assertIsNone(parse_control_command("hello"))


class TestAllowlistSafety(unittest.TestCase):
    def test_empty_allowlist_refuses_poll(self):
        from agents_relay.telegram_adapter import telegram_poll_loop

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp), allowed=(), token="tok")
            with patch("agents_relay.telegram_adapter.urllib.request.urlopen") as opened:
                with self.assertLogs("agents_relay.telegram", level="ERROR") as logs:
                    telegram_poll_loop(cfg)
            opened.assert_not_called()
            self.assertTrue(any("REFUSING" in line for line in logs.output))

    def test_allow_anyone_warns_and_polls(self):
        from agents_relay.telegram_adapter import telegram_poll_loop

        stop = threading.Event()

        def _boom(*_a, **_k):
            stop.set()
            raise OSError("stop")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp), allowed=(), token="tok", allow_anyone=True)
            with patch("agents_relay.telegram_adapter.urllib.request.urlopen", side_effect=_boom):
                with self.assertLogs("agents_relay.telegram", level="WARNING") as logs:
                    telegram_poll_loop(cfg, stop_event=stop)
            self.assertTrue(any("ALLOW_ANYONE" in line for line in logs.output))

    def test_send_rejects_empty_allowlist(self):
        from agents_relay.telegram_adapter import send_to_user

        with self.assertRaises(PermissionError):
            send_to_user(token="t", chat_id=1, text="hi", allowed=())

    def test_from_env_relay_state_and_caps(self):
        env = {
            "AGENTS_RELAY_ALLOW_ANYONE": "1",
            "AGENTS_RELAY_STATE": "/tmp/relay-state",
            "AGENTS_RELAY_MAX_JOBS": "4",
            "AGENTS_RELAY_MAX_JOBS_PER_CHAT": "2",
            "AGENTS_RELAY_APPROVER": "4242",
            "AGENTS_RELAY_ATTACH_DIR": "/tmp/relay-attach",
            "TELEGRAM_ALLOWED_CHAT_IDS": "",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = RelayConfig.from_env()
        self.assertTrue(cfg.allow_anyone)
        self.assertEqual(cfg.state_dir, "/tmp/relay-state")
        self.assertEqual(cfg.max_jobs, 4)
        self.assertEqual(cfg.max_jobs_per_chat, 2)
        self.assertEqual(cfg.telegram_allowed_chat_ids, ())
        self.assertEqual(cfg.approver, "4242")
        self.assertEqual(cfg.attach_dir, "/tmp/relay-attach")


class TestApproval(unittest.TestCase):
    def _request(self) -> dict:
        return {
            "id": "11111111-2222-3333-4444-555555555555",
            "session": "ses_1",
            "user": "12345",
            "tool": "docs.write",
            "args": {"path": "a.md"},
            "summary": "write a.md",
        }

    def test_callback_data_fits_telegram_limit(self):
        from agents_relay.approvals import approval_keyboard

        data = approval_keyboard(self._request()["id"])["inline_keyboard"][0][0]["callback_data"]
        self.assertLessEqual(len(data.encode("utf-8")), 64)
        self.assertTrue(data.endswith(":1"))

    def test_roundtrip_approve_and_deny(self):
        from agents_relay.telegram_adapter import handle_callback_query, run_approve

        for decision, expected in (("1", 0), ("0", 1)):
            with tempfile.TemporaryDirectory() as tmp:
                cfg = _cfg(Path(tmp))
                request = dict(self._request())
                if decision == "0":
                    request["id"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                holder: dict[str, int | str] = {}

                def _run() -> None:
                    code, note = run_approve(
                        chat_id=12345,
                        timeout=5,
                        request=request,
                        config=cfg,
                        poll_interval=0.05,
                    )
                    holder["code"] = code
                    holder["note"] = note

                with patch("agents_relay.telegram_adapter.send_message", return_value={"ok": True}) as sent:
                    with patch("agents_relay.telegram_adapter._post_json") as posted:
                        with patch("agents_relay.telegram_adapter.edit_message", return_value={"ok": True}):
                            thread = threading.Thread(target=_run)
                            thread.start()
                            deadline = time.time() + 2
                            path = Path(tmp) / "approvals" / f"{request['id']}.json"
                            while time.time() < deadline and not path.is_file():
                                time.sleep(0.02)
                            self.assertTrue(path.is_file())
                            handle_callback_query(
                                {
                                    "id": "cb-1",
                                    "from": {"id": 12345},
                                    "data": f"ap:{request['id']}:{decision}",
                                    "message": {"message_id": 9, "chat": {"id": 12345}, "text": "Approval needed"},
                                },
                                config=cfg,
                            )
                            thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(holder["code"], expected)
                sent.assert_called()
                markup = sent.call_args.kwargs["reply_markup"]
                self.assertEqual(markup["inline_keyboard"][0][0]["text"], "Approve")
                self.assertEqual(markup["inline_keyboard"][0][1]["text"], "Deny")
                answered = posted.call_args[0][1]["text"]
                self.assertIn("Approved" if expected == 0 else "Denied", answered)
                self.assertNotIn(str(tmp), json.dumps(sent.call_args.args) + "secret")

    def test_non_allowlisted_cannot_decide(self):
        from agents_relay.approvals import create_pending, read_approval
        from agents_relay.telegram_adapter import handle_callback_query

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp))
            request = self._request()
            create_pending(cfg, 12345, request)
            with patch("agents_relay.telegram_adapter._post_json") as posted:
                handle_callback_query(
                    {
                        "id": "cb-2",
                        "from": {"id": 999},
                        "data": f"ap:{request['id']}:1",
                        "message": {"message_id": 3, "chat": {"id": 12345}, "text": "Approval"},
                    },
                    config=cfg,
                )
            self.assertEqual(read_approval(cfg, request["id"])["status"], "pending")
            self.assertEqual(posted.call_args[0][1]["text"], "Not allowed")

    def test_timeout_exit_2(self):
        from agents_relay.telegram_adapter import run_approve

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp))
            with patch("agents_relay.telegram_adapter.send_message", return_value={"ok": True}):
                code, note = run_approve(
                    chat_id=12345,
                    timeout=0.3,
                    request=self._request(),
                    config=cfg,
                    poll_interval=0.05,
                )
            self.assertEqual(code, 2)
            self.assertEqual(note, "timeout")

    def test_cli_invalid_stdin_and_help(self):
        import io

        from agents_relay.__main__ import _help_json
        from agents_relay.__main__ import main as relay_main

        blob = json.dumps(_help_json())
        self.assertIn("approve", blob)
        self.assertIn("--timeout", blob)
        with patch("sys.stdin", io.StringIO("not-json")):
            self.assertEqual(relay_main(["approve", "--user", "12345", "--timeout", "1"]), 2)

    def test_not_allowlisted_is_unavailable(self):
        from agents_relay.telegram_adapter import run_approve

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp))
            code, note = run_approve(chat_id=99, timeout=1, request=self._request(), config=cfg)
            self.assertEqual(code, 2)
            self.assertIn("allowlist", note)

    def test_non_numeric_user_falls_back_to_approver_or_single_chat(self):
        import io

        from agents_relay.__main__ import main as relay_main
        from agents_relay.approvals import resolve_approver_chat

        chat, note = resolve_approver_chat("anonymous", approver="12345", allowed=(9, 8))
        self.assertEqual(chat, 12345)
        self.assertEqual(note, "")
        chat, note = resolve_approver_chat("anonymous", approver="", allowed=(42,))
        self.assertEqual(chat, 42)
        self.assertEqual(note, "")
        chat, note = resolve_approver_chat("12345", approver="99", allowed=(99,))
        self.assertEqual(chat, 12345)
        chat, note = resolve_approver_chat("anonymous", approver="", allowed=(1, 2))
        self.assertIsNone(chat)
        self.assertIn("AGENTS_RELAY_APPROVER", note)
        chat, note = resolve_approver_chat("", approver="nope", allowed=(42,))
        self.assertIsNone(chat)
        self.assertIn("not a numeric chat id", note)

        request = json.dumps(self._request())
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp), allowed=(7, 8), approver="7")
            with patch("agents_relay.__main__.RelayConfig.from_env", return_value=cfg):
                with patch("agents_relay.__main__.run_approve", return_value=(0, "approved")) as approved:
                    with patch("sys.stdin", io.StringIO(request)):
                        with patch("sys.stdout", io.StringIO()):
                            rc = relay_main(["approve", "--user", "anonymous", "--timeout", "5"])
            self.assertEqual(rc, 0)
            self.assertEqual(approved.call_args.kwargs["chat_id"], 7)

            buf = io.StringIO()
            denied_cfg = _cfg(Path(tmp), allowed=(7, 8), approver="")
            with patch("agents_relay.__main__.RelayConfig.from_env", return_value=denied_cfg):
                with patch("agents_relay.__main__.run_approve") as not_called:
                    with patch("sys.stdin", io.StringIO(request)):
                        with patch("sys.stdout", buf):
                            rc = relay_main(["approve", "--user", "anonymous", "--timeout", "5"])
            self.assertEqual(rc, 2)
            not_called.assert_not_called()
            self.assertIn("anonymous", buf.getvalue())
            self.assertIn("AGENTS_RELAY_APPROVER", buf.getvalue())


class TestConcurrency(unittest.TestCase):
    def test_other_chat_is_not_blocked(self):
        from agents_relay.telegram_adapter import dispatch_update

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp), allowed=(1, 2))
            registry = reset_registry(cfg)
            release = threading.Event()
            started: list[str] = []
            both = threading.Event()

            def turn(**kwargs):
                started.append(str(kwargs["user"]))
                if len(started) >= 2:
                    both.set()
                if kwargs["user"] == "1":
                    release.wait(3)
                return _result(kwargs["message"])

            with patch("agents_relay.telegram_adapter.send_message", return_value={"result": {"message_id": 1}}):
                with patch("agents_relay.telegram_adapter.edit_message", return_value={"ok": True}):
                    began = time.monotonic()
                    dispatch_update(_message(1, "slow"), config=cfg, on_turn=turn, registry=registry)
                    self.assertLess(time.monotonic() - began, 0.5)
                    dispatch_update(_message(2, "fast"), config=cfg, on_turn=turn, registry=registry)
                    self.assertTrue(both.wait(2), started)
                    release.set()
                    for job in registry.list_chat(1) + registry.list_chat(2):
                        job.done.wait(3)
            self.assertEqual(set(started), {"1", "2"})

    def test_per_chat_cap_queues(self):
        from agents_relay.telegram_adapter import dispatch_update

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp), allowed=(7,), max_jobs_per_chat=2, max_jobs=8)
            registry = reset_registry(cfg)
            release = threading.Event()
            started: list[str] = []

            def turn(**kwargs):
                started.append(kwargs["message"])
                release.wait(3)
                return _result()

            with patch("agents_relay.telegram_adapter.send_message", return_value={"result": {"message_id": 1}}):
                with patch("agents_relay.telegram_adapter.edit_message", return_value={"ok": True}):
                    for index in range(3):
                        dispatch_update(_message(7, f"m{index}"), config=cfg, on_turn=turn, registry=registry)
                    deadline = time.time() + 2
                    while time.time() < deadline and len(started) < 2:
                        time.sleep(0.01)
                    self.assertEqual(sorted(started), ["m0", "m1"])
                    queued = [job for job in registry.list_chat(7) if not job.running]
                    self.assertEqual(len(queued), 1)
                    release.set()
                    for job in list(registry.list_chat(7)):
                        self.assertTrue(job.done.wait(3))
                    deadline = time.time() + 2
                    while time.time() < deadline and len(started) < 3:
                        time.sleep(0.01)
                    self.assertEqual(sorted(started), ["m0", "m1", "m2"])

    def test_jobs_and_stop_kill_tree(self):
        from agents_relay.telegram_adapter import dispatch_update

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp), allowed=(5,))
            registry = reset_registry(cfg)
            release = threading.Event()
            running = threading.Event()

            def turn(**kwargs):
                on_pid = kwargs.get("on_pid")
                if on_pid:
                    on_pid(4242)
                running.set()
                release.wait(3)
                return _result("done")

            sent: list[str] = []

            def fake_send(_token, _chat, text, **_kwargs):
                sent.append(text)
                return {"result": {"message_id": 1}}

            with patch("agents_relay.telegram_adapter.send_message", side_effect=fake_send):
                with patch("agents_relay.telegram_adapter.edit_message", return_value={"ok": True}):
                    with patch("agents_relay.jobs.kill_process_tree") as killer:
                        dispatch_update(_message(5, "work"), config=cfg, on_turn=turn, registry=registry)
                        self.assertTrue(running.wait(2))
                        dispatch_update(_message(5, "/jobs"), config=cfg, on_turn=turn, registry=registry)
                        listing = "\n".join(sent)
                        self.assertIn("Jobs", listing)
                        self.assertIn("4242", listing)
                        job_id = registry.list_chat(5)[0].id
                        self.assertIn(job_id, listing)
                        dispatch_update(_message(5, f"/stop {job_id}"), config=cfg, on_turn=turn, registry=registry)
                        killer.assert_called_with(4242)
                        self.assertTrue(any("Stopped" in text for text in sent))
                        job = registry.list_chat(5)[0]
                        release.set()
                        self.assertTrue(job.done.wait(3))

    def test_stop_all_and_jobs_skip_turn(self):
        from agents_relay.telegram_adapter import dispatch_update

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp), allowed=(5,))
            registry = reset_registry(cfg)
            called: list[str] = []

            def turn(**kwargs):
                called.append(kwargs["message"])
                return _result()

            with patch("agents_relay.telegram_adapter.send_message", return_value={"result": {"message_id": 1}}) as sent:
                with patch("agents_relay.telegram_adapter.edit_message", return_value={"ok": True}):
                    dispatch_update(_message(5, "/jobs@klanker_bot"), config=cfg, on_turn=turn, registry=registry)
                    dispatch_update(_message(5, "/stop"), config=cfg, on_turn=turn, registry=registry)
            self.assertEqual(called, [])
            texts = [call.args[2] for call in sent.call_args_list]
            self.assertTrue(any("No running jobs" in text for text in texts))

    def test_callback_not_blocked_by_running_turn(self):
        from agents_relay.telegram_adapter import dispatch_update

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp), allowed=(5,))
            registry = reset_registry(cfg)
            release = threading.Event()

            def turn(**_kwargs):
                release.wait(3)
                return _result()

            with patch("agents_relay.telegram_adapter.send_message", return_value={"result": {"message_id": 1}}):
                with patch("agents_relay.telegram_adapter.edit_message", return_value={"ok": True}):
                    with patch("agents_relay.telegram_adapter._post_json") as posted:
                        dispatch_update(_message(5, "slow"), config=cfg, on_turn=turn, registry=registry)
                        began = time.monotonic()
                        dispatch_update(
                            {
                                "callback_query": {
                                    "id": "cb",
                                    "from": {"id": 5},
                                    "data": "ap:nope:1",
                                    "message": {"message_id": 1, "chat": {"id": 5}, "text": "x"},
                                }
                            },
                            config=cfg,
                            on_turn=turn,
                            registry=registry,
                        )
                        self.assertLess(time.monotonic() - began, 0.5)
                        self.assertTrue(posted.called)
                        release.set()


class TestAttachmentsAndKill(unittest.TestCase):
    def test_loop_argv_includes_attach(self):
        from agents_relay.loop_client import run_loop_turn

        trailer = json.dumps({"session": "s", "user_id": "u", "alias": "a"})

        class FakeProc:
            returncode = 0
            stdout = f"pong\n---agents-loop-trailer---\n{trailer}\n"
            stderr = ""

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp))
            with patch("agents_relay.loop_client.subprocess.run", return_value=FakeProc()) as run:
                run_loop_turn(
                    channel="http",
                    user="u",
                    message="ping",
                    config=cfg,
                    attachments=["/tmp/a.png", "/tmp/b.pdf"],
                )
            argv = run.call_args.args[0]
            self.assertEqual(argv.count("--attach"), 2)
            self.assertEqual(argv[argv.index("--attach") + 1], "/tmp/a.png")
            self.assertIn("/tmp/b.pdf", argv)

    def test_on_pid_real_process(self):
        from agents_relay.loop_client import run_loop_turn

        with tempfile.TemporaryDirectory() as tmp:
            cfg = RelayConfig(
                loop_cmd=(sys.executable, "-c", "print('hi')"),
                loop_provider="echo",
                relay_secret="",
                telegram_bot_token="",
                telegram_allowed_chat_ids=(),
                relay_host="127.0.0.1",
                relay_port=0,
                telegram_poll_timeout=1,
                state_dir=str(tmp),
            )
            pids: list[int] = []
            result = run_loop_turn(
                channel="http",
                user="u",
                message="x",
                config=cfg,
                on_status=lambda _s: None,
                on_pid=pids.append,
                timeout_sec=10,
            )
            self.assertEqual(result.reply, "hi")
            self.assertTrue(pids and pids[0] > 0)

    def test_kill_process_tree(self):
        code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen(['sleep', '120'])\n"
            "sys.stdout.write(str(child.pid) + '\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(120)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        assert proc.stdout is not None
        child_pid = int(proc.stdout.readline().strip())
        try:
            kill_process_tree(proc.pid)
            proc.wait(timeout=3)
            deadline = time.time() + 2
            while time.time() < deadline and _alive(child_pid):
                time.sleep(0.05)
            self.assertFalse(_alive(child_pid))
            self.assertFalse(_alive(proc.pid))
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            if _alive(proc.pid):
                kill_process_tree(proc.pid)
                proc.wait(timeout=2)

    def test_photo_and_document_passed_as_attach(self):
        from agents_relay.telegram_adapter import process_update

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp))
            seen: dict = {}

            def turn(**kwargs):
                seen.update(kwargs)
                return _result("saw it")

            photo = _message(12345, "")
            photo["message"]["text"] = ""
            photo["message"]["caption"] = "what is this?"
            photo["message"]["photo"] = [{"file_id": "small"}, {"file_id": "big"}]
            photo["message"]["document"] = {"file_id": "doc", "file_name": "notes.pdf"}

            def fake_download(_token, file_id, dest, *, dest_name=None):
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / f"{file_id}.bin"
                target.write_bytes(b"x")
                self.assertNotIn(".agents", str(dest))
                self.assertTrue(str(dest).startswith(tmp))
                return target

            with patch("agents_relay.telegram_adapter._download_telegram_file", side_effect=fake_download):
                with patch("agents_relay.telegram_adapter.send_message", return_value={"result": {"message_id": 1}}):
                    with patch("agents_relay.telegram_adapter.edit_message", return_value={"ok": True}):
                        process_update(photo, config=cfg, on_turn=turn)
            self.assertEqual(seen["user"], "12345")
            self.assertEqual(seen["message"], "what is this?")
            self.assertEqual(len(seen["attachments"]), 2)
            self.assertTrue(all(path.startswith(tmp) for path in seen["attachments"]))
            self.assertNotIn("[Photo", seen["message"])

    def test_v1_turn_attachments(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp))
            photo = Path(tmp) / "pic.png"
            photo.write_bytes(b"png")
            seen: dict = {}

            def turn(**kwargs):
                seen.update(kwargs)
                return _result("ok")

            server = serve_http(cfg, on_turn=turn)
            host, port = server.server_address
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = HTTPConnection(host, port, timeout=5)
                body = json.dumps(
                    {
                        "user": "t",
                        "text": "look",
                        "attachments": [
                            {"path": str(photo), "mime": "image/png"},
                            {"path": str(Path(tmp) / "missing.png"), "mime": "image/png"},
                        ],
                    }
                )
                conn.request(
                    "POST",
                    "/v1/turn",
                    body=body,
                    headers={"Content-Type": "application/json", "X-Relay-Secret": "expected"},
                )
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                self.assertEqual(seen["attachments"], [str(photo)])
            finally:
                server.shutdown()
                thread.join(timeout=2)

    def test_v1_turn_url_attachment_downloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp))
            seen: dict = {}

            def turn(**kwargs):
                seen.update(kwargs)
                return _result("ok")

            class FakeResp:
                def read(self, _n=None):
                    if not hasattr(self, "_done"):
                        self._done = True
                        return b"hello-bytes"
                    return b""

                def __enter__(self):
                    return self

                def __exit__(self, *_a):
                    return False

            server = serve_http(cfg, on_turn=turn)
            host, port = server.server_address
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch("agents_relay.attachments.urllib.request.urlopen", return_value=FakeResp()):
                    conn = HTTPConnection(host, port, timeout=5)
                    body = json.dumps(
                        {
                            "text": "look",
                            "attachments": [{"url": "https://example.com/a.png", "mime": "image/png"}],
                        }
                    )
                    conn.request(
                        "POST",
                        "/v1/turn",
                        body=body,
                        headers={"Content-Type": "application/json", "X-Relay-Secret": "expected"},
                    )
                    self.assertEqual(conn.getresponse().status, 200)
                self.assertEqual(len(seen["attachments"]), 1)
                saved = Path(seen["attachments"][0])
                self.assertTrue(saved.is_file())
                self.assertEqual(saved.read_bytes(), b"hello-bytes")
                self.assertTrue(str(saved).startswith(tmp))
                self.assertEqual(saved.suffix, ".png")
            finally:
                server.shutdown()
                thread.join(timeout=2)

    def test_local_path_confined_and_mime_extension(self):
        from agents_relay.attachments import AttachmentRejected, materialize_attachments

        with tempfile.TemporaryDirectory() as root_s, tempfile.TemporaryDirectory() as other_s:
            root = Path(root_s)
            other = Path(other_s)
            inbox = root / "inbox"
            secret = other / "secret.txt"
            secret.write_text("nope")
            blob = root / "blob"
            blob.write_bytes(b"img")
            ready = root / "pic.png"
            ready.write_bytes(b"png")
            link = root / "linked.png"
            link.symlink_to(secret)

            with self.assertRaises(AttachmentRejected):
                materialize_attachments(
                    [{"path": str(secret), "mime": "text/plain"}],
                    inbox,
                    allowed_root=root,
                )
            with self.assertRaises(AttachmentRejected):
                materialize_attachments([{"path": str(link)}], inbox, allowed_root=root)

            copied = materialize_attachments(
                [{"path": str(blob), "mime": "image/jpeg"}],
                inbox,
                allowed_root=root,
            )
            self.assertEqual(Path(copied[0]).suffix, ".jpg")
            self.assertEqual(Path(copied[0]).read_bytes(), b"img")
            self.assertTrue(Path(copied[0]).resolve().is_relative_to(inbox.resolve()))

            kept = materialize_attachments(
                [{"path": str(ready), "mime": "image/png"}],
                inbox,
                allowed_root=root,
            )
            self.assertEqual(Path(kept[0]).resolve(), ready.resolve())

            cfg = _cfg(root, attach_dir=str(inbox))
            seen: dict = {}

            def turn(**kwargs):
                seen.update(kwargs)
                return _result("ok")

            server = serve_http(cfg, on_turn=turn)
            host, port = server.server_address
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = HTTPConnection(host, port, timeout=5)
                body = json.dumps({"text": "look", "attachments": [{"path": str(secret)}]})
                conn.request(
                    "POST",
                    "/v1/turn",
                    body=body,
                    headers={"Content-Type": "application/json", "X-Relay-Secret": "expected"},
                )
                resp = conn.getresponse()
                resp.read()
                self.assertEqual(resp.status, 400)
                self.assertEqual(seen, {})

                inside = inbox / "ok.png"
                inside.parent.mkdir(parents=True, exist_ok=True)
                inside.write_bytes(b"ok")
                conn = HTTPConnection(host, port, timeout=5)
                body = json.dumps(
                    {
                        "text": "look",
                        "attachments": [{"url": "https://example.com/file.txt", "mime": "image/webp"}],
                    }
                )

                class FakeResp:
                    def read(self, _n=None):
                        if not hasattr(self, "_done"):
                            self._done = True
                            return b"webp-bytes"
                        return b""

                    def __enter__(self):
                        return self

                    def __exit__(self, *_a):
                        return False

                with patch("agents_relay.attachments.urllib.request.urlopen", return_value=FakeResp()):
                    conn.request(
                        "POST",
                        "/v1/turn",
                        body=body,
                        headers={"Content-Type": "application/json", "X-Relay-Secret": "expected"},
                    )
                    self.assertEqual(conn.getresponse().status, 200)
                saved = Path(seen["attachments"][0])
                self.assertEqual(saved.suffix, ".webp")
                self.assertEqual(saved.read_bytes(), b"webp-bytes")
            finally:
                server.shutdown()
                thread.join(timeout=2)

    def test_long_reply_is_split_across_messages(self):
        from agents_relay.telegram_adapter import handle_inbound_text

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp))
            pieces: list[str] = []

            def fake_send(_token, _chat, text, **_kwargs):
                pieces.append(text)
                return {"result": {"message_id": 4}}

            def fake_edit(_token, _chat, _mid, text, **_kwargs):
                pieces.append(text)
                return {"ok": True}

            def turn(**_kwargs):
                return _result("A" * 5000)

            with patch("agents_relay.telegram_adapter.send_message", side_effect=fake_send):
                with patch("agents_relay.telegram_adapter.edit_message", side_effect=fake_edit):
                    handle_inbound_text(chat_id=12345, text="go", config=cfg, on_turn=turn)
            bodies = [re.sub(r"<[^>]+>", "", piece) for piece in pieces]
            self.assertEqual("".join(bodies).count("A"), 5000)
            for piece in pieces:
                self.assertLessEqual(utf16_len(piece), 4096)
            self.assertGreater(len(pieces), 2)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestUrlRejected(unittest.TestCase):
    def test_file_url_rejected(self):
        from agents_relay.attachments import materialize_attachments

        with tempfile.TemporaryDirectory() as tmp:
            paths = materialize_attachments([{"url": "file:///etc/passwd"}], Path(tmp))
            self.assertEqual(paths, [])


class TestJobRegistryDirect(unittest.TestCase):
    def test_queue_full_returns_none(self):
        registry = JobRegistry(max_per_chat=1, max_global=1, max_queue=1)
        release = threading.Event()

        def block(_job):
            release.wait(2)

        first = registry.submit(1, "a", block)
        self.assertIsNotNone(first)
        second = registry.submit(1, "b", block)
        self.assertIsNotNone(second)
        third = registry.submit(1, "c", block)
        self.assertIsNone(third)
        release.set()
        assert first is not None and second is not None
        first.done.wait(2)
        second.done.wait(2)


if __name__ == "__main__":
    unittest.main()
