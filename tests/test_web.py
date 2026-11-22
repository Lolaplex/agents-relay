"""Web surface tests: cookie auth, static assets, streaming chat, stop."""

from __future__ import annotations

import json
import os
import stat
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

    def test_token_query_issues_cookie_and_redirects(self):
        resp = self.srv.request("GET", "/?token=tok")
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.getheader("Location"), "/")
        raw = resp.getheader("Set-Cookie") or ""
        self.assertIn(f"{COOKIE_NAME}=", raw)
        value = raw.split(f"{COOKIE_NAME}=", 1)[1].split(";", 1)[0]
        status = self.srv.request("GET", "/v1/auth/status", headers={"Cookie": f"{COOKIE_NAME}={value}"})
        self.assertTrue(json.loads(status.read().decode())["authenticated"])

    def test_token_query_wrong_token_falls_through(self):
        resp = self.srv.request("GET", "/?token=wrong")
        self.assertNotEqual(resp.status, 302)
        resp.read()


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

    def test_attach_replays_buffer_then_live(self):
        gate = threading.Event()

        def fake_stream(params, config, *, cancel=None, on_pid=None):
            yield StreamEvent(type="delta", text="part1 ")
            gate.wait(timeout=5)
            yield StreamEvent(type="delta", text="part2")
            yield StreamEvent(type="trailer", session="ses_att", user_id="u", alias="", returncode=0)

        result: dict = {}

        def run_stream():
            with patch("agents_relay.web.iter_loop_stream_live", fake_stream):
                resp = self.srv.request(
                    "POST",
                    "/v1/chat/stream",
                    {"text": "hi", "run": "r-att", "session": "ses_att"},
                    headers={"X-Relay-Secret": "expected"},
                )
                result["body"] = resp.read().decode()

        worker = threading.Thread(target=run_stream, daemon=True)
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "r-att" not in active_runs():
            time.sleep(0.02)
        self.assertIn("r-att", active_runs())
        time.sleep(0.15)  # let the first delta land in the buffer

        att = self.srv.request(
            "GET",
            "/v1/runs/stream?run=r-att",
            headers={"X-Relay-Secret": "expected"},
        )
        self.assertEqual(att.status, 200)
        gate.set()
        body = att.read().decode()
        worker.join(timeout=5)

        frames = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        self.assertEqual(frames[0]["type"], "attach")
        text = "".join(f.get("text", "") for f in frames if f["type"] == "delta")
        self.assertIn("part1", text)
        self.assertIn("part2", text)
        self.assertEqual(frames[-1]["type"], "trailer")
        self.assertEqual(frames[-1]["session"], "ses_att")

    def test_attach_404_for_unknown_run(self):
        resp = self.srv.request(
            "GET",
            "/v1/runs/stream?run=nope",
            headers={"X-Relay-Secret": "expected"},
        )
        self.assertEqual(resp.status, 404)
        resp.read()

    def test_attach_finds_run_by_session(self):
        gate = threading.Event()

        def fake_stream(params, config, *, cancel=None, on_pid=None):
            gate.wait(timeout=5)
            yield StreamEvent(type="trailer", session="ses_find", user_id="u", alias="", returncode=0)

        def run_stream():
            with patch("agents_relay.web.iter_loop_stream_live", fake_stream):
                resp = self.srv.request(
                    "POST",
                    "/v1/chat/stream",
                    {"text": "hi", "run": "r-find", "session": "ses_find"},
                    headers={"X-Relay-Secret": "expected"},
                )
                resp.read()

        worker = threading.Thread(target=run_stream, daemon=True)
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "r-find" not in active_runs():
            time.sleep(0.02)
        self.assertIn("r-find", active_runs())

        att = self.srv.request(
            "GET",
            "/v1/runs/stream?session=ses_find",
            headers={"X-Relay-Secret": "expected"},
        )
        self.assertEqual(att.status, 200)
        gate.set()
        body = att.read().decode()
        worker.join(timeout=5)
        self.assertIn('"run": "r-find"', body)

class TestPush(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ,
            {
                "AGENTS_KEYS_DIR": str(Path(self._tmp.name) / "keys"),
                "AGENTS_HOST_DIR": str(Path(self._tmp.name) / "host"),
            },
            clear=False,
        )
        self._env.start()
        self.srv = _Server(_cfg(state_dir=str(Path(self._tmp.name) / "relay-state")))

    def tearDown(self):
        self.srv.close()
        self._env.stop()
        self._tmp.cleanup()

    def test_vapid_key_mints_and_stays_stable(self):
        resp = self.srv.request(
            "GET", "/v1/push/vapid-key", headers={"X-Relay-Secret": "expected"}
        )
        self.assertEqual(resp.status, 200)
        key = json.loads(resp.read().decode())["key"]
        import base64

        raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
        self.assertEqual(len(raw), 65)
        self.assertEqual(raw[0], 4)
        vapid_path = Path(self._tmp.name) / "host" / "keys" / "vapid.json"
        self.assertTrue(vapid_path.is_file())
        self.assertFalse((Path(self._tmp.name) / "relay-state" / "vapid.json").exists())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(vapid_path.stat().st_mode), 0o600)
        # same key on second call (persisted under <host>/keys/vapid.json)
        resp2 = self.srv.request(
            "GET", "/v1/push/vapid-key", headers={"X-Relay-Secret": "expected"}
        )
        self.assertEqual(json.loads(resp2.read().decode())["key"], key)

    def test_subscribe_upserts_by_endpoint(self):
        sub = {
            "endpoint": "https://push.example/x1",
            "keys": {"p256dh": "a", "auth": "b"},
        }
        for _ in range(2):
            resp = self.srv.request(
                "POST",
                "/v1/push/subscribe",
                {"subscription": sub, "workspace": "", "persona": "p1"},
                headers={"X-Relay-Secret": "expected"},
            )
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read().decode())["subscriptions"], 1)
        push_path = Path(self._tmp.name) / "host" / "secrets" / "push.json"
        self.assertTrue(push_path.is_file())
        self.assertFalse((Path(self._tmp.name) / "relay-state" / "push.json").exists())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(push_path.stat().st_mode), 0o600)
        stored = json.loads(push_path.read_text(encoding="utf-8"))
        self.assertEqual(len(stored), 1)

    def test_legacy_fallback_read_and_no_cwd_fallback(self):
        import warnings
        from agents_relay import push as push_mod

        legacy_dir = Path(self._tmp.name) / "legacy-relay"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_sub = [
            {
                "endpoint": "https://push.example/legacy",
                "keys": {"p256dh": "k1", "auth": "k2"},
                "workspace": "",
                "persona": "default",
                "created_at": 1.0,
            }
        ]
        (legacy_dir / "push.json").write_text(json.dumps(legacy_sub), encoding="utf-8")
        (legacy_dir / "vapid.json").write_text(
            json.dumps({"private": "1" * 64, "public": "B64-LEGACY"}),
            encoding="utf-8",
        )
        legacy_cfg = _cfg(state_dir=str(legacy_dir))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            subs = push_mod.load_subscriptions(config=legacy_cfg)
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0]["endpoint"], "https://push.example/legacy")
            self.assertEqual(push_mod.vapid_keypair(legacy_cfg)["public"], "B64-LEGACY")
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))

        # Subscribe & VAPID with blank state_dir never fall back to CWD.
        cwd_tmp = Path(self._tmp.name) / "cwd"
        cwd_tmp.mkdir(parents=True, exist_ok=True)
        old_cwd = Path.cwd()
        blank_srv = _Server(_cfg(state_dir=""))
        try:
            os.chdir(cwd_tmp)
            resp_v = blank_srv.request(
                "GET", "/v1/push/vapid-key", headers={"X-Relay-Secret": "expected"}
            )
            self.assertEqual(resp_v.status, 200)
            resp_v.read()
            resp = blank_srv.request(
                "POST",
                "/v1/push/subscribe",
                {
                    "subscription": {
                        "endpoint": "https://push.example/new",
                        "keys": {"p256dh": "a", "auth": "b"},
                    },
                    "persona": "p2",
                },
                headers={"X-Relay-Secret": "expected"},
            )
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read().decode())["subscriptions"], 1)
            self.assertTrue((Path(self._tmp.name) / "host" / "secrets" / "push.json").is_file())
            self.assertFalse((cwd_tmp / "push.json").exists())
            self.assertFalse((cwd_tmp / "vapid.json").exists())
            # Primary host store now supersedes legacy fallback reads.
            self.assertEqual(
                [r["endpoint"] for r in push_mod.load_subscriptions(config=legacy_cfg)],
                ["https://push.example/new"],
            )
        finally:
            os.chdir(old_cwd)
            blank_srv.close()

    def test_push_send_501_without_pywebpush(self):
        resp = self.srv.request(
            "POST",
            "/v1/push/send",
            {"payload": {"type": "test"}},
            headers={"X-Relay-Secret": "expected"},
        )
        try:
            import pywebpush  # noqa: F401
        except ImportError:
            self.assertEqual(resp.status, 501)
        resp.read()

    def test_subscribe_rejects_bad_endpoint(self):
        resp = self.srv.request(
            "POST",
            "/v1/push/subscribe",
            {"subscription": {"endpoint": "http://nope"}},
            headers={"X-Relay-Secret": "expected"},
        )
        self.assertEqual(resp.status, 400)
        resp.read()


if __name__ == "__main__":
    unittest.main()


class TestTcpDropAndAuthStatus(unittest.TestCase):
    """agents-relay#30: TCP drop keeps turn alive; cookie_warning on status."""

    def test_broken_pipe_does_not_cancel_turn(self):
        from agents_relay import web as web_mod
        from agents_relay.turn import StreamEvent

        captured: dict = {}

        real_new_run_entry = web_mod._new_run_entry

        def spy_new_run_entry():
            entry = real_new_run_entry()
            captured["cancel"] = entry["cancel"]
            return entry

        def fake_stream(params, config, cancel=None):
            yield StreamEvent(type="delta", text="hi")

        class FakeWFile:
            def write(self, data):
                raise BrokenPipeError("client gone")

            def flush(self):
                raise BrokenPipeError("client gone")

        class FakeHandler:
            def __init__(self):
                self.wfile = FakeWFile()
                self.headers = {}
                self._c = _cfg()

            def _cfg(self):
                return self._c

            def _write_json(self, body, status):
                captured["error"] = (body, status)

            def send_response(self, code):
                pass

            def send_header(self, k, v):
                pass

            def end_headers(self):
                pass

        with patch.object(web_mod, "iter_loop_stream_live", fake_stream), \
                patch.object(web_mod, "_new_run_entry", spy_new_run_entry):
            web_mod.handle_chat_stream(FakeHandler(), {"text": "hi", "run": "r1"})
        self.assertIn("cancel", captured)
        self.assertFalse(captured["cancel"].is_set())
        self.assertEqual(active_runs(), [])

    def _status(self, headers: dict) -> dict:
        srv = _Server(_cfg())
        self.addCleanup(srv.close)
        conn = HTTPConnection(srv.host, srv.port, timeout=10)
        conn.request("GET", "/v1/auth/status", headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return data

    def test_cookie_warning_plain_http_non_loopback(self):
        data = self._status({"Host": "100.64.1.2:8787"})
        self.assertTrue(data["secure_cookie"])
        self.assertIn("tailscale", data["cookie_warning"])

    def test_no_cookie_warning_forwarded_https(self):
        data = self._status(
            {"Host": "100.64.1.2:8787", "X-Forwarded-Proto": "https"})
        self.assertTrue(data["secure_cookie"])
        self.assertEqual(data["cookie_warning"], "")

    def test_no_cookie_warning_loopback(self):
        data = self._status({"Host": "127.0.0.1:8787"})
        self.assertFalse(data["secure_cookie"])
        self.assertEqual(data["cookie_warning"], "")


class TestApprovalPushPayload(unittest.TestCase):
    def test_notify_approval_pending_has_title_and_body(self):
        from agents_relay import push as push_mod

        sent: dict = {}

        def fake_send(config, payload, ws_ref=""):
            sent["payload"] = payload
            sent["ws_ref"] = ws_ref
            return 1

        with patch.object(push_mod, "send_push_all", fake_send):
            push_mod.notify_approval_pending(
                _cfg(), {"id": "a1", "summary": "rm -rf /tmp/x"}, ws_ref="demo")
        payload = sent["payload"]
        self.assertEqual(payload["title"], "Klanker needs approval")
        self.assertEqual(payload["body"], "rm -rf /tmp/x")
        self.assertEqual(payload["summary"], "rm -rf /tmp/x")
        self.assertEqual(payload["type"], "approval")
        self.assertEqual(sent["ws_ref"], "demo")
