"""Remote registry + authenticated mount: CRUD, secret confinement, proxy."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http


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
        web_token="",
        web_user="",
    )
    base.update(overrides)
    return RelayConfig(**base)


class _Upstream(BaseHTTPRequestHandler):
    """Fake remote relay: echoes auth header + path."""

    seen: dict = {}

    def _echo(self):
        body = json.dumps({
            "auth": self.headers.get("Authorization") or "",
            "path": self.path,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _echo
    do_DELETE = _echo

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        self.rfile.read(length)
        self._echo()

    def log_message(self, *a):
        pass


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
        hdrs["Authorization"] = "Bearer expected"
        hdrs.update(headers or {})
        conn.request(method, path, body=payload, headers=hdrs)
        return conn.getresponse()


class TestRemotes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        self.up_port = self.upstream.server_address[1]
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        cfg = _cfg(state_dir=self.tmp.name)
        self.relay = _Server(cfg)
        self.base = f"http://127.0.0.1:{self.up_port}"

    def tearDown(self):
        self.relay.close()
        self.upstream.shutdown()
        self.tmp.cleanup()

    def test_auth_required(self):
        conn = HTTPConnection(self.relay.host, self.relay.port, timeout=10)
        conn.request("GET", "/v1/remotes")
        self.assertEqual(conn.getresponse().status, 401)

    def test_crud_and_secret_confinement(self):
        r = self.relay.request("POST", "/v1/remotes", {
            "id": "vps", "base_url": self.base, "secret": "remote-secret", "label": "VPS",
        })
        self.assertEqual(r.status, 200)
        row = json.loads(r.read())["remote"]
        self.assertTrue(row["has_secret"])
        self.assertNotIn("secret", row)

        r = self.relay.request("GET", "/v1/remotes")
        remotes = json.loads(r.read())["remotes"]
        self.assertEqual([m["id"] for m in remotes], ["vps"])
        self.assertNotIn("secret", remotes[0])

        # Empty secret keeps the stored one.
        r = self.relay.request("POST", "/v1/remotes", {
            "id": "vps", "base_url": self.base, "secret": "", "label": "VPS2",
        })
        row = json.loads(r.read())["remote"]
        self.assertTrue(row["has_secret"])

    def test_proxy_injects_bearer_and_strips_secret_param(self):
        self.relay.request("POST", "/v1/remotes", {
            "id": "vps", "base_url": self.base, "secret": "remote-secret",
        })
        r = self.relay.request(
            "GET", "/v1/remote/vps/v1/workspaces?secret=leak&limit=2")
        self.assertEqual(r.status, 200)
        echo = json.loads(r.read())
        self.assertEqual(echo["auth"], "Bearer remote-secret")
        # Client query kept, our auth params stripped upstream.
        self.assertIn("limit=2", echo["path"])
        self.assertNotIn("secret=leak", echo["path"])

    def test_proxy_post_and_delete(self):
        self.relay.request("POST", "/v1/remotes", {
            "id": "vps", "base_url": self.base, "secret": "s",
        })
        r = self.relay.request("POST", "/v1/remote/vps/v1/jobs", {"goal": "x"})
        self.assertEqual(json.loads(r.read())["path"], "/v1/jobs")
        r = self.relay.request("DELETE", "/v1/remote/vps/v1/workspaces/A")
        self.assertEqual(json.loads(r.read())["path"], "/v1/workspaces/A")

    def test_unknown_remote_404(self):
        r = self.relay.request("GET", "/v1/remote/ghost/v1/workspaces")
        self.assertEqual(r.status, 404)

    def test_delete_remote(self):
        self.relay.request("POST", "/v1/remotes", {
            "id": "vps", "base_url": self.base, "secret": "s",
        })
        r = self.relay.request("DELETE", "/v1/remotes/vps")
        self.assertEqual(r.status, 200)
        r = self.relay.request("GET", "/v1/remotes")
        self.assertEqual(json.loads(r.read())["remotes"], [])


if __name__ == "__main__":
    unittest.main()
