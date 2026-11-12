"""Goal-session jobs: POST /v1/jobs lifecycle + guards."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.loop_client import LoopTurnResult
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
        headers = {"Content-Type": "application/json"}
        headers["X-Relay-Secret"] = "expected"
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        raw = res.read() or b"{}"
        try:
            return res.status, json.loads(raw)
        except json.JSONDecodeError:
            return res.status, {"error": raw.decode("utf-8", "replace")}


class TestCodingJobs(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(
            os.environ, {"AGENTS_HOST_DIR": self.tmp.name}, clear=False
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        from pathlib import Path

        self.host = Host(dir=Path(self.tmp.name))
        self.ws = self.host.create("qa", env=os.environ)
        from agents_relay.workspaces import create_persona

        create_persona(self.ws, "dev")

    def _post(self, body: dict, turn=None):
        server = _Server(_cfg(), on_turn=turn)
        try:
            return server, server.request("POST", "/v1/jobs", body)
        except Exception:
            server.close()
            raise

    def test_job_runs_to_done(self):
        def fake(**kw):
            self.assertEqual(kw.get("message"), "say hi")
            self.assertEqual(kw.get("workspace"), "qa")
            return LoopTurnResult(
                reply="hello", session="ses_x", user_id="u_1",
                alias="", returncode=0, stderr="",
            )

        server, (status, data) = self._post(
            {"workspace": "qa", "persona": "dev", "goal": "say hi"}, turn=fake
        )
        try:
            self.assertEqual(status, 201)
            job = data["job"]
            self.assertEqual(job["status"], "queued")
            deadline = time.time() + 10
            seen = {}
            while time.time() < deadline:
                _, seen = server.request(
                    "GET", f"/v1/jobs/{job['id']}?workspace=qa&persona=dev"
                )
                if seen.get("job", {}).get("status") in ("done", "failed"):
                    break
                time.sleep(0.1)
            self.assertEqual(seen["job"]["status"], "done")
            self.assertEqual(seen["job"]["result"], "hello")
            _, lst = server.request("GET", "/v1/jobs?workspace=qa&persona=dev")
            self.assertEqual([j["id"] for j in lst["jobs"]], [job["id"]])
        finally:
            server.close()

    def test_missing_persona_404(self):
        server, (status, data) = self._post(
            {"workspace": "qa", "persona": "ghost", "goal": "x"}
        )
        try:
            self.assertEqual(status, 404)
            self.assertIn("persona", data.get("error", ""))
        finally:
            server.close()

    def test_validation(self):
        server = _Server(_cfg())
        try:
            for body, want in (
                ({"persona": "dev", "goal": "x"}, 400),
                ({"workspace": "qa", "goal": "x"}, 400),
                ({"workspace": "qa", "persona": "dev"}, 400),
            ):
                status, _ = server.request("POST", "/v1/jobs", body)
                self.assertEqual(status, want, body)
        finally:
            server.close()

    def test_failed_turn_marks_failed(self):
        def boom(**kw):
            return LoopTurnResult(
                reply="", session="", user_id="",
                alias="", returncode=2, stderr="nope",
            )

        server, (status, data) = self._post(
            {"workspace": "qa", "persona": "dev", "goal": "break"}, turn=boom
        )
        try:
            self.assertEqual(status, 201)
            deadline = time.time() + 10
            seen = {}
            while time.time() < deadline:
                _, seen = server.request(
                    "GET", f"/v1/jobs/{data['job']['id']}?workspace=qa&persona=dev"
                )
                if seen.get("job", {}).get("status") in ("done", "failed"):
                    break
                time.sleep(0.1)
            self.assertEqual(seen["job"]["status"], "failed")
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
