"""Runtime model switch: GET/POST /v1/model + Telegram /model command."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
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
    def __init__(self, cfg: RelayConfig, **kw) -> None:
        self.server = serve_http(cfg, **kw)
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


class TestModelEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"AGENTS_HOST_DIR": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.host = Host(dir=Path(self.tmp.name))
        self.ws = self.host.create("qa", env=os.environ)
        self.srv = _Server(_cfg())

    def tearDown(self) -> None:
        self.srv.close()

    def test_get_model_lists_providers_and_pick(self):
        status, data = self.srv.request("GET", "/v1/model?workspace=qa")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("providers", data)
        self.assertTrue(any(p["name"] == "echo" for p in data["providers"]))
        self.assertEqual(data["provider"], "")

    def test_post_model_switch_and_clear(self):
        status, data = self.srv.request(
            "POST", "/v1/model", {"provider": "echo", "workspace": "qa"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["provider"], "echo")
        status, data = self.srv.request("GET", "/v1/model?workspace=qa")
        self.assertEqual(data["provider"], "echo")
        status, data = self.srv.request(
            "POST", "/v1/model", {"clear": True, "workspace": "qa"}
        )
        self.assertEqual(status, 200)
        status, data = self.srv.request("GET", "/v1/model?workspace=qa")
        self.assertEqual(data["provider"], "")

    def test_post_unknown_provider_400(self):
        status, _ = self.srv.request(
            "POST", "/v1/model", {"provider": "nope.nothing", "workspace": "qa"}
        )
        self.assertEqual(status, 400)

    def test_model_requires_auth(self):
        conn = HTTPConnection(self.srv.host, self.srv.port, timeout=10)
        conn.request("GET", "/v1/model")
        res = conn.getresponse()
        res.read()
        self.assertEqual(res.status, 401)


class TestTelegramModelCommand(unittest.TestCase):
    def test_model_pref_reaches_turn(self):
        from agents_relay.telegram_adapter import _model_text, _set_chat_pref, _chat_model

        with tempfile.TemporaryDirectory() as tmp:
            host_dir = Path(tmp) / "host"
            with patch.dict(os.environ, {"AGENTS_HOST_DIR": str(host_dir)}, clear=False):
                cfg = _cfg(state_dir=str(Path(tmp) / "relay"))
                chat_id = 42

                _set_chat_pref(chat_id, "model", "echo:fast", cfg)
                self.assertEqual(_chat_model(chat_id, cfg), ("echo", "fast"))
                self.assertTrue((host_dir / "clients" / "telegram.json").is_file())
                self.assertFalse((Path(tmp) / "relay" / "chat_prefs.json").exists())

                text = _model_text(chat_id, "", cfg)
                self.assertIn("model: echo:fast", text)
                text = _model_text(chat_id, "nope.nothing", cfg)
                self.assertIn("unknown provider", text)
                text = _model_text(chat_id, "echo", cfg)
                self.assertIn("model: echo", text)
                self.assertEqual(_chat_model(chat_id, cfg), ("echo", ""))
                _model_text(chat_id, "clear", cfg)
                self.assertEqual(_chat_model(chat_id, cfg), ("", ""))

    def test_telegram_prefs_legacy_fallback(self):
        import warnings
        from agents_relay.telegram_adapter import _chat_model, _set_chat_pref

        with tempfile.TemporaryDirectory() as tmp:
            host_dir = Path(tmp) / "host"
            legacy_dir = Path(tmp) / "relay"
            legacy_dir.mkdir(parents=True, exist_ok=True)
            (legacy_dir / "chat_prefs.json").write_text(
                json.dumps({"42": {"model": "echo:legacy"}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"AGENTS_HOST_DIR": str(host_dir)}, clear=False):
                cfg = _cfg(state_dir=str(legacy_dir))
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always", DeprecationWarning)
                    self.assertEqual(_chat_model(42, cfg), ("echo", "legacy"))
                self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))
                _set_chat_pref(42, "model", "echo:fresh", cfg)
                self.assertEqual(_chat_model(42, cfg), ("echo", "fresh"))
                self.assertTrue((host_dir / "clients" / "telegram.json").is_file())


if __name__ == "__main__":
    unittest.main()
