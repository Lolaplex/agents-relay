"""Web surface tests: cookie auth, static assets, streaming chat, stop."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.turn import StreamEvent
from agents_relay.web import (
    COOKIE_NAME,
    active_runs,
    issue_cookie,
    verify_cookie,
)


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
        web_token="tok",
        web_user="web",
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

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        conn = HTTPConnection(self.host, self.port, timeout=10)
        payload = json.dumps(body) if body is not None else None
        hdrs = {"Content-Type": "application/json"} if payload else {}
        hdrs.update(headers or {})
        conn.request(method, path, body=payload, headers=hdrs)
        return conn.getresponse()


class TestCookie(unittest.TestCase):
    def test_roundtrip(self):
        cfg = _cfg()
        value = issue_cookie(cfg)
        self.assertEqual(verify_cookie(cfg, value), "web")

    def test_tampered_signature_rejected(self):
        cfg = _cfg()
        value = issue_cookie(cfg)
        exp, _, _sig = value.partition(".")
        self.assertIsNone(verify_cookie(cfg, f"{exp}.deadbeef"))

    def test_expired_rejected(self):
        cfg = _cfg()
        self.assertIsNone(verify_cookie(cfg, "1.abc"))

    def test_garbage_rejected(self):
        cfg = _cfg()
        self.assertIsNone(verify_cookie(cfg, ""))
        self.assertIsNone(verify_cookie(cfg, "nodot"))


class TestAuthEndpoints(unittest.TestCase):
    def setUp(self):
        self.srv = _Server(_cfg())

    def tearDown(self):
        self.srv.close()

    def test_login_rejects_wrong_token(self):
        resp = self.srv.request("POST", "/v1/auth/login", {"token": "nope"})
        self.assertEqual(resp.status, 401)

    def test_login_sets_cookie_and_status_accepts_it(self):
        resp = self.srv.request("POST", "/v1/auth/login", {"token": "tok"})
        self.assertEqual(resp.status, 200)
        raw = resp.getheader("Set-Cookie") or ""
        self.assertIn(f"{COOKIE_NAME}=", raw)
        self.assertIn("HttpOnly", raw)
        value = raw.split(f"{COOKIE_NAME}=", 1)[1].split(";", 1)[0]

        status = self.srv.request("GET", "/v1/auth/status", headers={"Cookie": f"{COOKIE_NAME}={value}"})
        data = json.loads(status.read().decode())
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["user"], "web")

    def test_status_without_cookie(self):
        resp = self.srv.request("GET", "/v1/auth/status")
        data = json.loads(resp.read().decode())
        self.assertFalse(data["authenticated"])

    def test_logout_clears_cookie(self):
        resp = self.srv.request("POST", "/v1/auth/logout")
        self.assertIn("Max-Age=0", resp.getheader("Set-Cookie") or "")


class TestStatic(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        (root / "index.html").write_text("<h1>dashboard</h1>", encoding="utf-8")
        (root / "app.js").write_text("console.log(1)", encoding="utf-8")
        self.srv = _Server(_cfg(static_dir=str(root)))

    def tearDown(self):
        self.srv.close()
        self.dir.cleanup()

    def test_root_serves_index(self):
        resp = self.srv.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type") or "")
        self.assertIn(b"dashboard", resp.read())

    def test_asset_served(self):
        resp = self.srv.request("GET", "/app.js")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"console.log", resp.read())

    def test_traversal_blocked(self):
        resp = self.srv.request("GET", "/../pyproject.toml")
        self.assertEqual(resp.status, 404)

    def test_missing_static_dir_keeps_json_root(self):
        missing = str(Path(self.dir.name) / "missing")
        with patch.dict(os.environ, {"AGENTS_RELAY_WEB_DIR": missing}, clear=False):
            srv = _Server(_cfg(static_dir=""))
            try:
                resp = srv.request("GET", "/")
                self.assertEqual(json.loads(resp.read().decode()), {"ok": True})
            finally:
                srv.close()


class TestChatStream(unittest.TestCase):
    def setUp(self):
        self.srv = _Server(_cfg())

    def tearDown(self):
        self.srv.close()

    def test_stream_requires_auth(self):
        resp = self.srv.request("POST", "/v1/chat/stream", {"text": "hi"})
        self.assertEqual(resp.status, 401)

    def test_stream_emits_status_delta_trailer(self):
        def fake_stream(params, config, *, cancel=None, on_pid=None):
            self.assertEqual(params.channel, "web")
            self.assertEqual(params.user, "web")
            yield StreamEvent(type="status", text="[*] Running shell")
            yield StreamEvent(type="delta", text="hello ")
            yield StreamEvent(type="delta", text="world")
            yield StreamEvent(type="trailer", session="ses_9", user_id="u1", alias="", returncode=0)

        with patch("agents_relay.web.iter_loop_stream_live", fake_stream):
            resp = self.srv.request(
                "POST",
                "/v1/chat/stream",
                {"text": "hi", "run": "r1"},
                headers={"X-Relay-Secret": "expected"},
            )
            self.assertEqual(resp.status, 200)
            body = resp.read().decode()

        frames = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        types = [f["type"] for f in frames]
        self.assertEqual(types[0], "start")
        self.assertIn("status", types)
        self.assertIn("delta", types)
        self.assertEqual(types[-1], "trailer")
        answer = "".join(f.get("text", "") for f in frames if f["type"] == "delta")
        self.assertEqual(answer, "hello world")
        self.assertEqual(frames[-1]["session"], "ses_9")

    def test_web_enabled_without_relay_secret_requires_cookie(self):
        srv = _Server(_cfg(relay_secret=""))
        try:
            resp = srv.request("POST", "/v1/chat/stream", {"text": "hi"})
            self.assertEqual(resp.status, 401)

            login = srv.request("POST", "/v1/auth/login", {"token": "tok"})
            value = (login.getheader("Set-Cookie") or "").split(f"{COOKIE_NAME}=", 1)[1].split(";", 1)[0]

            def fake_stream(params, config, *, cancel=None, on_pid=None):
                yield StreamEvent(type="trailer", session="s", returncode=0)

            with patch("agents_relay.web.iter_loop_stream_live", fake_stream):
                ok = srv.request(
                    "POST",
                    "/v1/chat/stream",
                    {"text": "hi"},
                    headers={"Cookie": f"{COOKIE_NAME}={value}"},
                )
                self.assertEqual(ok.status, 200)
                ok.read()
        finally:
            srv.close()

    def test_header_secret_still_works_when_web_enabled(self):
        resp = self.srv.request(
            "POST",
            "/v1/chat/stop",
            {"run": "none"},
            headers={"X-Relay-Secret": "expected"},
        )
        self.assertEqual(resp.status, 200)

    def test_stop_cancels_running_stream(self):
        def fake_stream(params, config, *, cancel=None, on_pid=None):
            for _ in range(200):
                if cancel is not None and cancel.is_set():
                    yield StreamEvent(type="error", text="stopped")
                    return
                time.sleep(0.02)
            yield StreamEvent(type="trailer", session="s", returncode=0)

        result: dict = {}

        def run_stream():
            with patch("agents_relay.web.iter_loop_stream_live", fake_stream):
                resp = self.srv.request(
                    "POST",
                    "/v1/chat/stream",
                    {"text": "long", "run": "r-stop"},
                    headers={"X-Relay-Secret": "expected"},
                )
                result["body"] = resp.read().decode()

        worker = threading.Thread(target=run_stream, daemon=True)
        worker.start()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "r-stop" not in active_runs():
            time.sleep(0.02)
        self.assertIn("r-stop", active_runs())

        stop = self.srv.request(
            "POST",
            "/v1/chat/stop",
            {"run": "r-stop"},
            headers={"X-Relay-Secret": "expected"},
        )
        self.assertEqual(stop.status, 200)
        self.assertIn("r-stop", json.loads(stop.read().decode())["stopped"])

        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertIn("stopped", result.get("body", ""))


if __name__ == "__main__":
    unittest.main()
