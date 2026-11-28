"""Tests for Wave 11A: guaranteed terminal SSE event, hidden providers, edit model preservation."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents_relay import web as relay_web
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.read_cache import relay_read_cache
from agents_workspace import Host


def _cfg(**overrides) -> RelayConfig:
    base = dict(
        loop_cmd=("python", "-c", "print('skip')"),
        loop_provider="echo",
        relay_secret="expected",
        telegram_bot_token="",
        telegram_allowed_chat_ids=(),
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
    )
    base.update(overrides)
    return RelayConfig(**base)


class _Server:
    def __init__(self, cfg: RelayConfig) -> None:
        self.server = serve_http(cfg)
        self.host, self.port = self.server.server_address  # type: ignore[misc]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, body: dict | None = None):
        conn = HTTPConnection(self.host, self.port, timeout=10)
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json", "X-Relay-Secret": "expected"}
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        raw = res.read() or b"{}"
        try:
            return res.status, json.loads(raw)
        except json.JSONDecodeError:
            return res.status, {"error": raw.decode("utf-8", "replace")}


class TestWave11AProvidersAndStream(unittest.TestCase):
    def setUp(self) -> None:
        relay_read_cache.invalidate()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.host = Host(dir=Path(self.tmp.name))
        self.host.create("qa", env=os.environ)
        self.srv = _Server(_cfg(state_dir=str(Path(self.tmp.name) / "relay")))

    def tearDown(self) -> None:
        self.srv.close()
        relay_read_cache.invalidate()

    def test_drive_run_emits_error_when_stream_ends_without_terminal_event(self):
        entry = {
            "events": [],
            "cond": threading.Condition(),
            "done": False,
            "done_at": 0.0,
            "cancel": threading.Event(),
            "meta": {"session": "ses_test"},
        }

        def _empty_stream(_params, _config, cancel=None):
            yield SimpleNamespace(type="status", text="thinking")

        with patch.object(relay_web, "iter_loop_stream_live", _empty_stream), patch.object(
            relay_web, "_schedule_reap", lambda *_a, **_kw: None
        ):
            relay_web._drive_run("run-empty", entry, object(), _cfg())

        self.assertTrue(entry["done"])
        types = [ev.get("type") for ev in entry["events"]]
        self.assertIn("error", types)
        err = next(ev for ev in entry["events"] if ev.get("type") == "error")
        self.assertIn("ended", err.get("text", "").lower())

    def test_hide_and_restore_builtin_provider_preserves_delete_409(self):
        st, data = self.srv.request("GET", "/v1/providers")
        self.assertEqual(st, 200)
        names = [r["name"] for r in data["providers"]]
        self.assertIn("openai.default", names)
        self.assertEqual(data["hidden"], [])

        # DELETE on env provider still returns 409
        st, _ = self.srv.request("DELETE", "/v1/providers/openai.default")
        self.assertEqual(st, 409)

        # POST /v1/providers/hidden hides openai.default cleanly
        st, hid = self.srv.request(
            "POST", "/v1/providers/hidden", {"name": "openai.default", "hidden": True}
        )
        self.assertEqual(st, 200)
        self.assertEqual(hid["hidden"], ["openai.default"])

        st, data = self.srv.request("GET", "/v1/providers")
        self.assertEqual(st, 200)
        self.assertNotIn("openai.default", [r["name"] for r in data["providers"]])
        self.assertEqual(data["hidden"], ["openai.default"])

        st, all_data = self.srv.request("GET", "/v1/providers?include_hidden=1")
        self.assertEqual(st, 200)
        row = next(r for r in all_data["providers"] if r["name"] == "openai.default")
        self.assertTrue(row["hidden"])

        # Restore all restores openai.default
        st, restored = self.srv.request("POST", "/v1/providers/hidden", {"restore_all": True})
        self.assertEqual(st, 200)
        self.assertEqual(restored["hidden"], [])
        st, data = self.srv.request("GET", "/v1/providers")
        self.assertIn("openai.default", [r["name"] for r in data["providers"]])

    def test_edit_provider_preserves_models_and_supports_label(self):
        st, _ = self.srv.request(
            "POST",
            "/v1/providers",
            {
                "name": "openrouter.local",
                "label": "OpenRouter",
                "url": "https://openrouter.ai/api/v1",
                "key_env": "LLM_API_KEY",
                "model": "z-ai/glm-5.3-flash",
                "models": ["z-ai/glm-5.3-flash", "anthropic/claude-sonnet-5.5"],
            },
        )
        self.assertEqual(st, 200)

        # Edit URL/model without passing `models` -> existing models + label must survive
        st, _ = self.srv.request(
            "POST",
            "/v1/providers",
            {
                "name": "openrouter.local",
                "url": "https://openrouter.ai/api/v1",
                "key_env": "LLM_API_KEY",
                "model": "anthropic/claude-sonnet-5.5",
            },
        )
        self.assertEqual(st, 200)

        st, data = self.srv.request("GET", "/v1/providers?refresh=1")
        self.assertEqual(st, 200)
        row = next(r for r in data["providers"] if r["name"] == "openrouter.local")
        self.assertEqual(row["label"], "OpenRouter")
        self.assertEqual(row["model"], "anthropic/claude-sonnet-5.5")
        self.assertEqual(row["models"], ["z-ai/glm-5.3-flash", "anthropic/claude-sonnet-5.5"])


if __name__ == "__main__":
    unittest.main()
