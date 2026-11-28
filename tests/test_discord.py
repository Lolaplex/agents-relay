"""Discord adapter unit tests."""

from __future__ import annotations

import json
import os
import threading
import unittest
from unittest.mock import patch

from agents_relay.config import RelayConfig
from agents_relay.jobs import reset_registry
from agents_relay.loop_client import LoopTurnResult


def _cfg(**overrides) -> RelayConfig:
    base = dict(
        loop_cmd=("python", "-m", "runner.loop"),
        loop_provider="echo",
        relay_secret="",
        telegram_bot_token="",
        telegram_allowed_chat_ids=(),
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
        discord_bot_token="discord-token",
        discord_allowed_channel_ids=(123,),
        discord_allowed_user_ids=(42,),
        discord_require_mention=True,
    )
    base.update(overrides)
    return RelayConfig(**base)


def _result(reply: str = "pong", **overrides) -> LoopTurnResult:
    base = dict(reply=reply, session="ses_d", user_id="u_d", alias="discord:123", returncode=0, stderr="")
    base.update(overrides)
    return LoopTurnResult(**base)


def _message(
    content: str,
    *,
    channel_id: str = "123",
    author_id: str = "42",
    guild_id: str | None = "777",
    bot: bool = False,
    referenced_author_id: str = "",
    attachments: list | None = None,
) -> dict:
    author = {"id": author_id, "username": "alice", "bot": bot}
    row = {
        "id": "555",
        "type": 0 if not referenced_author_id else 19,
        "channel_id": channel_id,
        "author": author,
        "content": content,
        "attachments": attachments or [],
    }
    if guild_id:
        row["guild_id"] = guild_id
    if referenced_author_id:
        row["referenced_message"] = {"author": {"id": referenced_author_id}}
    return row


class DiscordIoCapture:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.deleted: list[tuple[str, int, object]] = []

    def send(self, token: str, channel_id: int, text: str, *, components: list[dict] | None = None) -> dict:
        row = {
            "id": str(1000 + len(self.sent)),
            "token": token,
            "channel_id": channel_id,
            "text": text,
            "components": components,
        }
        self.sent.append(row)
        return {"id": row["id"]}

    def edit(self, token: str, channel_id: int, message_id: int | str, text: str, *, components=None) -> dict:
        self.edited.append(
            {
                "token": token,
                "channel_id": channel_id,
                "message_id": message_id,
                "text": text,
                "components": components,
            }
        )
        return {"id": str(message_id)}

    def delete(self, token: str, channel_id: int, message_id: int | str) -> dict:
        self.deleted.append((token, channel_id, message_id))
        return {}

    def patch(self):
        return patch.multiple(
            "agents_relay.discord_adapter",
            send_message=self.send,
            edit_message=self.edit,
            delete_message=self.delete,
        )


class TestDiscordFormat(unittest.TestCase):
    def test_short_text_not_split(self):
        from agents_relay.discord_format import split_discord_markdown

        self.assertEqual(split_discord_markdown("hello"), ["hello"])
        self.assertEqual(split_discord_markdown(""), [])

    def test_split_prefers_line_boundaries_for_short_lines(self):
        from agents_relay.discord_format import split_discord_markdown

        first = "a" * 49 + "\n"
        second = "short line\n"
        parts = split_discord_markdown(first + second + "end!!", limit=60)
        self.assertEqual(parts[0], first)
        self.assertEqual(parts[1], second + "end!!")

    def test_split_fence_at_capacity_boundary_stays_within_limit(self):
        from agents_relay.discord_format import fences_balanced, split_discord_markdown

        body = "LoNYZNTxqR\n" + "word " * 10 + "\n```python\n```\n"
        parts = split_discord_markdown(body, limit=32)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 32)
            self.assertTrue(fences_balanced(part), part)

    def test_long_text_split_within_limit_and_fences_balanced(self):
        from agents_relay.discord_format import fences_balanced, split_discord_markdown

        body = "intro\n\n```python\n" + "a" * 2400 + "\nprint(1)\n```\nafter"
        parts = split_discord_markdown(body, limit=500)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 500)
            self.assertTrue(fences_balanced(part), part)
        self.assertIn("after", parts[-1])


class TestDiscordConfig(unittest.TestCase):
    def test_approve_route_infers_discord_only_numeric_channel(self):
        from agents_relay.__main__ import _approve_route

        cfg = _cfg(
            telegram_bot_token="telegram-token",
            telegram_allowed_chat_ids=(12345,),
            discord_allowed_channel_ids=(123,),
        )
        self.assertEqual(_approve_route("123", cfg), "discord")
        self.assertEqual(_approve_route("12345", cfg), "telegram")
        self.assertEqual(_approve_route("999", cfg), "telegram")
        self.assertEqual(_approve_route("123", cfg, "discord"), "discord")
        only_discord = _cfg(telegram_bot_token="", telegram_allowed_chat_ids=())
        self.assertEqual(_approve_route("", only_discord), "discord")

    def test_from_env_reads_discord_fields(self):
        env = {
            "DISCORD_BOT_TOKEN": "tok",
            "DISCORD_ALLOWED_CHANNEL_IDS": "123, 456",
            "DISCORD_ALLOWED_USER_IDS": "42,43",
            "DISCORD_REQUIRE_MENTION": "1",
            "DISCORD_APPROVER": "123",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("agents_relay.config.resolve_secret", side_effect=lambda key: os.environ.get(key, "")):
                cfg = RelayConfig.from_env()
        self.assertEqual(cfg.discord_bot_token, "tok")
        self.assertEqual(cfg.discord_allowed_channel_ids, (123, 456))
        self.assertEqual(cfg.discord_allowed_user_ids, (42, 43))
        self.assertTrue(cfg.discord_require_mention)
        self.assertEqual(cfg.discord_approver, "123")


class TestDiscordInbound(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = _cfg()
        self.registry = reset_registry(self.cfg)
        self.io = DiscordIoCapture()
        self.patcher = self.io.patch()
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.registry.shutdown()
        reset_registry(self.cfg)

    def test_inject_runs_turn_and_deletes_thinking(self):
        from agents_relay.discord_adapter import inject_text

        seen = []

        def fake_turn(**kwargs):
            seen.append(kwargs)
            if kwargs.get("on_status"):
                kwargs["on_status"]("running tool")
            return _result("Calendar empty.")

        result = inject_text(channel_id=123, text="was steht an?", config=self.cfg, on_turn=fake_turn)

        self.assertEqual(result.reply, "Calendar empty.")
        self.assertEqual(seen[0]["channel"], "discord")
        self.assertEqual(seen[0]["user"], "123")
        self.assertEqual(seen[0]["message"], "was steht an?")
        self.assertEqual(self.io.deleted, [("discord-token", 123, "1000")])
        self.assertEqual(self.io.sent[-1]["text"], "Calendar empty.")

    def test_inject_denies_non_allowlisted_channel(self):
        from agents_relay.discord_adapter import inject_text

        with self.assertRaises(PermissionError):
            inject_text(channel_id=124, text="hi", config=self.cfg, on_turn=lambda **kw: _result())

    def test_send_to_user_splits_long_messages(self):
        from agents_relay.discord_adapter import DISCORD_MESSAGE_LIMIT, send_to_user

        send_to_user(
            token="discord-token",
            channel_id=123,
            text="x" * (DISCORD_MESSAGE_LIMIT + 100),
            allowed=(123,),
        )
        self.assertGreater(len(self.io.sent), 1)
        for row in self.io.sent:
            self.assertLessEqual(len(row["text"]), DISCORD_MESSAGE_LIMIT)

    def test_attachment_failure_note_reaches_prompt_and_reply(self):
        from agents_relay.discord_adapter import process_message

        seen = []

        def fake_turn(**kwargs):
            seen.append(kwargs)
            return _result("done")

        msg = _message("look at this", attachments=[{"filename": "big.bin", "size": 1}])
        with patch("agents_relay.discord_adapter._collect_attachments", return_value=(["/tmp/a.txt"], ["Attachment dropped: big.bin"])):
            process_message(msg, config=self.cfg, on_turn=fake_turn)

        self.assertEqual(seen[0]["attachments"], ["/tmp/a.txt"])
        self.assertIn("Attachment dropped: big.bin", seen[0]["message"])
        self.assertIn("Attachment dropped: big.bin", self.io.sent[-1]["text"])


class TestDiscordAuthorization(unittest.TestCase):
    def test_empty_user_allowlist_fails_closed_without_open_opt_in(self):
        from agents_relay.discord_adapter import user_allowed

        cfg = _cfg(discord_allowed_user_ids=())
        self.assertFalse(user_allowed(42, cfg))
        open_cfg = _cfg(discord_allowed_user_ids=(), allow_anyone=True)
        self.assertTrue(user_allowed(42, open_cfg))

    def test_gateway_refuses_empty_user_allowlist_without_open_opt_in(self):
        from agents_relay.discord_adapter import discord_gateway_loop

        cfg = _cfg(discord_allowed_user_ids=())

        def connect_should_not_run(*args, **kwargs):
            raise AssertionError("gateway connect must not run")

        with self.assertLogs("agents_relay.discord", level="ERROR") as logged:
            discord_gateway_loop(cfg, stop_event=threading.Event(), connect=connect_should_not_run)
        self.assertTrue(any("DISCORD_ALLOWED_USER_IDS" in row for row in logged.output))


class TestDiscordDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = _cfg()
        self.registry = reset_registry(self.cfg)
        self.io = DiscordIoCapture()
        self.patcher = self.io.patch()
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.registry.shutdown()
        reset_registry(self.cfg)

    def _wait_event(self, event: threading.Event) -> None:
        self.assertTrue(event.wait(2.0), "timed out waiting for discord job")

    def test_ignores_bot_authors(self):
        from agents_relay.discord_adapter import dispatch_message

        calls = []
        dispatch_message(
            _message("<@999> hi", bot=True),
            config=self.cfg,
            on_turn=lambda **kw: calls.append(kw) or _result(),
            registry=self.registry,
            bot_user_id=999,
        )
        self.assertEqual(calls, [])
        self.assertEqual(self.io.sent, [])

    def test_ignores_unallowlisted_channel_and_user(self):
        from agents_relay.discord_adapter import dispatch_message

        calls = []
        cfg = _cfg(discord_allowed_user_ids=(42,))
        for msg in (
            _message("<@999> hi", channel_id="124"),
            _message("<@999> hi", author_id="99"),
        ):
            dispatch_message(
                msg,
                config=cfg,
                on_turn=lambda **kw: calls.append(kw) or _result(),
                registry=self.registry,
                bot_user_id=999,
            )
        self.assertEqual(calls, [])

    def test_require_mention_ignores_plain_guild_message(self):
        from agents_relay.discord_adapter import dispatch_message

        calls = []
        dispatch_message(
            _message("hello everyone"),
            config=self.cfg,
            on_turn=lambda **kw: calls.append(kw) or _result(),
            registry=self.registry,
            bot_user_id=999,
        )
        self.assertEqual(calls, [])

    def test_mention_is_stripped_and_dispatched(self):
        from agents_relay.discord_adapter import dispatch_message

        seen = []
        done = threading.Event()

        def fake_turn(**kwargs):
            seen.append(kwargs)
            done.set()
            return _result("ok")

        dispatch_message(
            _message("<@999> hello there"),
            config=self.cfg,
            on_turn=fake_turn,
            registry=self.registry,
            bot_user_id=999,
        )
        self._wait_event(done)
        self.assertEqual(seen[0]["message"], "hello there")
        self.assertNotIn("<@999>", seen[0]["message"])

    def test_message_update_without_edited_timestamp_is_ignored(self):
        from agents_relay.discord_adapter import DiscordEventRouter

        calls = []

        def fake_dispatch(message, **kwargs):
            calls.append(message)

        with patch("agents_relay.discord_adapter.dispatch_message", side_effect=fake_dispatch):
            router = DiscordEventRouter(self.cfg)
            router.bot_user_id = 999
            router.dispatch("MESSAGE_UPDATE", _message("<@999> embed refresh"))
        self.assertEqual(calls, [])

    def test_dm_is_addressed_without_mention(self):
        from agents_relay.discord_adapter import dispatch_message

        done = threading.Event()
        dispatch_message(
            _message("hello", guild_id=None),
            config=self.cfg,
            on_turn=lambda **kw: done.set() or _result(),
            registry=self.registry,
            bot_user_id=999,
        )
        self._wait_event(done)

    def test_reply_to_bot_is_addressed_without_mention(self):
        from agents_relay.discord_adapter import dispatch_message

        done = threading.Event()
        dispatch_message(
            _message("thanks", referenced_author_id="999"),
            config=self.cfg,
            on_turn=lambda **kw: done.set() or _result(),
            registry=self.registry,
            bot_user_id=999,
        )
        self._wait_event(done)

    def test_require_mention_can_be_disabled_for_channel_mirror(self):
        from agents_relay.discord_adapter import dispatch_message

        done = threading.Event()
        cfg = _cfg(discord_require_mention=False)
        dispatch_message(
            _message("plain channel traffic"),
            config=cfg,
            on_turn=lambda **kw: done.set() or _result(),
            registry=self.registry,
            bot_user_id=999,
        )
        self._wait_event(done)

    def test_jobs_control_does_not_start_turn(self):
        from agents_relay.discord_adapter import dispatch_message

        calls = []
        dispatch_message(
            _message("<@999> /jobs"),
            config=self.cfg,
            on_turn=lambda **kw: calls.append(kw) or _result(),
            registry=self.registry,
            bot_user_id=999,
        )
        self.assertEqual(calls, [])
        self.assertTrue(any("No running jobs" in row["text"] for row in self.io.sent))

    def test_new_message_interrupts_single_running_job(self):
        from agents_relay.discord_adapter import dispatch_message

        first_started = threading.Event()
        release_first = threading.Event()
        second_done = threading.Event()
        seen: list[str] = []

        def fake_turn(**kwargs):
            message = str(kwargs.get("message") or "")
            seen.append(message)
            if message == "first":
                first_started.set()
                self.assertTrue(release_first.wait(2.0))
            else:
                second_done.set()
            return _result("ok")

        stopped = []
        original_stop = self.registry.stop

        def spy_stop(chat_id, job_id=None):
            stopped.append((chat_id, job_id))
            return original_stop(chat_id, job_id)

        self.registry.stop = spy_stop  # type: ignore[method-assign]
        try:
            dispatch_message(
                _message("<@999> first"),
                config=self.cfg,
                on_turn=fake_turn,
                registry=self.registry,
                bot_user_id=999,
            )
            self._wait_event(first_started)
            dispatch_message(
                _message("<@999> second"),
                config=self.cfg,
                on_turn=fake_turn,
                registry=self.registry,
                bot_user_id=999,
            )
            self.assertEqual(stopped, [(123, None)])
            release_first.set()
            self._wait_event(second_done)
        finally:
            release_first.set()
        self.assertEqual(seen, ["first", "second"])


class TestDiscordApprovals(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = _cfg()
        self.io = DiscordIoCapture()
        self.patcher = self.io.patch()
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()

    def test_approval_components_use_shared_callback_encoding(self):
        from agents_relay.discord_adapter import approval_components

        rows = approval_components("abc123")
        buttons = rows[0]["components"]
        self.assertEqual(buttons[0]["custom_id"], "ap:abc123:1")
        self.assertEqual(buttons[1]["custom_id"], "ap:abc123:0")

    def test_approval_markdown_stays_below_discord_limit(self):
        from agents_relay.discord_adapter import DISCORD_MESSAGE_LIMIT, format_approval_markdown

        text = format_approval_markdown(
            {
                "id": "ap_long",
                "tool": "shell",
                "summary": "s" * 5000,
                "session": "ses",
                "args": {"cmd": "x" * 1000},
            }
        )
        self.assertLess(len(text), DISCORD_MESSAGE_LIMIT)

    def test_run_approve_prompts_with_buttons(self):
        from agents_relay.discord_adapter import run_approve

        request = {"id": "dcrappr1", "tool": "shell", "args": {"cmd": "ls"}, "summary": "run ls"}
        with patch("agents_relay.discord_adapter.wait_for_decision", return_value=("approved", "approved")):
            code, note = run_approve(channel_id=123, timeout=1, request=request, config=self.cfg)
        self.assertEqual((code, note), (0, "approved"))
        prompt = self.io.sent[-1]
        self.assertIn("Approval needed", prompt["text"])
        self.assertEqual(prompt["components"][0]["components"][0]["custom_id"], "ap:dcrappr1:1")

    def test_run_approve_rejects_replayed_id_with_changed_args(self):
        from agents_relay.approvals import create_pending, record_decision
        from agents_relay.discord_adapter import run_approve

        original = {"id": "dcrreplay", "tool": "shell", "args": {"cmd": "ls"}}
        create_pending(self.cfg, 123, original)
        record_decision(
            self.cfg,
            "dcrreplay",
            status="approved",
            note="approved",
            decided_by=42,
            expected_chat_id=123,
        )
        changed = {"id": "dcrreplay", "tool": "shell", "args": {"cmd": "rm -rf /"}}
        code, note = run_approve(channel_id=123, timeout=0.01, request=changed, config=self.cfg)
        self.assertEqual((code, note), (2, "approval request changed"))

    def test_interaction_approval_records_decision_and_edits_message(self):
        from agents_relay.approvals import create_pending, read_approval
        from agents_relay.discord_adapter import handle_interaction

        request = {"id": "dcrappr2", "tool": "shell", "args": {}}
        create_pending(self.cfg, 123, request)
        responses = []
        interaction = {
            "type": 3,
            "id": "interaction-1",
            "token": "interaction-token",
            "channel_id": "123",
            "user": {"id": "42"},
            "data": {"custom_id": "ap:dcrappr2:1"},
            "message": {"content": "old prompt"},
        }
        with patch("agents_relay.discord_adapter.respond_interaction", side_effect=lambda *args: responses.append(args) or {}):
            handle_interaction(interaction, config=self.cfg)
        record = read_approval(self.cfg, "dcrappr2")
        self.assertEqual(record["status"], "approved")
        self.assertEqual(record["decided_by"], 42)
        self.assertEqual(responses[0][2]["type"], 7)
        self.assertIn("**Approved**", responses[0][2]["data"]["content"])
        self.assertEqual(responses[0][2]["data"]["components"], [])

    def test_interaction_rejects_wrong_channel_without_deciding(self):
        from agents_relay.approvals import create_pending, read_approval
        from agents_relay.discord_adapter import handle_interaction

        request = {"id": "dcrappr3", "tool": "shell", "args": {}}
        create_pending(self.cfg, 123, request)
        responses = []
        interaction = {
            "type": 3,
            "id": "interaction-2",
            "token": "interaction-token",
            "channel_id": "999",
            "user": {"id": "42"},
            "data": {"custom_id": "ap:dcrappr3:1"},
            "message": {"content": "old prompt"},
        }
        with patch("agents_relay.discord_adapter.respond_interaction", side_effect=lambda *args: responses.append(args) or {}):
            handle_interaction(interaction, config=self.cfg)
        self.assertEqual(read_approval(self.cfg, "dcrappr3")["status"], "pending")
        self.assertEqual(responses[0][2]["data"]["content"], "Not allowed")


class TestDiscordGateway(unittest.TestCase):
    def test_send_message_disables_outbound_mentions(self):
        from agents_relay.discord_adapter import send_message

        calls = []
        with patch("agents_relay.discord_adapter._api_request", side_effect=lambda *args, **kwargs: calls.append((args, kwargs)) or {"id": "1"}):
            send_message("tok", 123, "hello <@42>")
        payload = calls[0][0][3]
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_router_accepts_message_update_like_telegram_edits(self):
        from agents_relay.discord_adapter import DiscordEventRouter

        cfg = _cfg()
        registry = reset_registry(cfg)
        done = threading.Event()
        io = DiscordIoCapture()
        try:
            with io.patch():
                router = DiscordEventRouter(
                    cfg,
                    on_turn=lambda **kw: done.set() or _result(),
                    registry=registry,
                )
                router.bot_user_id = 999
                row = _message("<@999> edited")
                row["edited_timestamp"] = "2026-01-01T00:00:00Z"
                router.dispatch("MESSAGE_UPDATE", row)
            self.assertTrue(done.wait(2.0))
        finally:
            registry.shutdown()
            reset_registry(cfg)

    def test_router_swallows_malformed_event_after_logging(self):
        from agents_relay.discord_adapter import DiscordEventRouter

        cfg = _cfg()
        registry = reset_registry(cfg)
        try:
            router = DiscordEventRouter(cfg, on_turn=lambda **kw: _result(), registry=registry)
            with self.assertLogs("agents_relay.discord", level="ERROR"):
                router.dispatch("MESSAGE_CREATE", {"channel_id": "123", "author": {"id": "42"}, "type": "not-an-int"})
        finally:
            registry.shutdown()
            reset_registry(cfg)

    def test_gateway_ws_url_and_identify_payload(self):
        from agents_relay.discord_adapter import GATEWAY_INTENTS, gateway_ws_url, identify_payload

        self.assertEqual(gateway_ws_url("wss://gateway.discord.gg/"), "wss://gateway.discord.gg/?v=10&encoding=json")
        payload = identify_payload("tok")
        self.assertEqual(payload["op"], 2)
        self.assertEqual(payload["d"]["token"], "tok")
        self.assertEqual(payload["d"]["intents"], GATEWAY_INTENTS)

    def test_dispatch_ready_message_and_gateway_control_ops(self):
        from agents_relay.discord_adapter import DiscordGatewayClient

        events = []
        client = DiscordGatewayClient("tok", on_dispatch=lambda t, d: events.append((t, d)))

        class FakeWs:
            def __init__(self):
                self.sent = []

            def send(self, raw):
                self.sent.append(json.loads(raw))

            def close(self):
                pass

        ws = FakeWs()
        ack = [False]
        ready = {
            "op": 0,
            "s": 1,
            "t": "READY",
            "d": {"session_id": "sess", "resume_gateway_url": "wss://resume", "user": {"id": "999"}},
        }
        self.assertEqual(client._handle_payload(ready, ws, ack), "")
        self.assertEqual(client.session_id, "sess")
        self.assertEqual(client.resume_url, "wss://resume")
        self.assertEqual(client.seq, 1)
        self.assertEqual(events[0][0], "READY")

        self.assertEqual(client._handle_payload({"op": 1, "d": None}, ws, ack), "")
        self.assertEqual(ws.sent[-1], {"op": 1, "d": 1})
        self.assertEqual(client._handle_payload({"op": 11, "d": None}, ws, ack), "")
        self.assertTrue(ack[0])
        self.assertEqual(client._handle_payload({"op": 7, "d": None}, ws, ack), "reconnect")
        self.assertEqual(client._handle_payload({"op": 9, "d": False}, ws, ack), "reconnect")
        self.assertEqual(client.session_id, "")
        self.assertIsNone(client.seq)

    def test_run_connection_identifies_and_dispatches_ready(self):
        from agents_relay.discord_adapter import DiscordGatewayClient

        events = []
        client = DiscordGatewayClient("tok", on_dispatch=lambda t, d: events.append((t, d)))
        frames = [
            json.dumps({"op": 10, "d": {"heartbeat_interval": 45000}}),
            json.dumps({
                "op": 0,
                "s": 1,
                "t": "READY",
                "d": {"session_id": "sess", "resume_gateway_url": "wss://resume", "user": {"id": "999"}},
            }),
        ]

        class FakeWs:
            def __init__(self):
                self.sent = []
                self.recv_count = 0

            def settimeout(self, value):
                pass

            def recv(self):
                if self.recv_count < len(frames):
                    raw = frames[self.recv_count]
                    self.recv_count += 1
                    return raw
                raise ConnectionError("closed")

            def send(self, raw):
                self.sent.append(json.loads(raw))

            def close(self):
                pass

        ws = FakeWs()
        with self.assertRaises(ConnectionError):
            client._run_connection(ws, threading.Event())
        self.assertEqual(ws.sent[0]["op"], 2)
        self.assertEqual(ws.sent[0]["d"]["token"], "tok")
        self.assertEqual(events[0][0], "READY")
        self.assertEqual(client.session_id, "sess")
        self.assertEqual(client.resume_url, "wss://resume")

    def test_connection_timeout_tracks_heartbeat_interval(self):
        from agents_relay.discord_adapter import DiscordGatewayClient

        client = DiscordGatewayClient("tok", on_dispatch=lambda t, d: None)

        class FakeWs:
            def __init__(self):
                self.timeout = None
                self.recv_count = 0

            def settimeout(self, value):
                self.timeout = value

            def recv(self):
                self.recv_count += 1
                if self.recv_count == 1:
                    return json.dumps({"op": 10, "d": {"heartbeat_interval": 45000}})
                raise ConnectionError("closed")

            def send(self, raw):
                pass

            def close(self):
                pass

        ws = FakeWs()
        with self.assertRaises(ConnectionError):
            client._run_connection(ws, threading.Event())
        self.assertEqual(ws.timeout, 67.5)


if __name__ == "__main__":
    unittest.main()
