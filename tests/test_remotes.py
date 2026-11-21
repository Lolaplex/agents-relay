"""Remote registry + authenticated mount: CRUD, secret confinement, proxy."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

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
        self._env = patch.dict(
            os.environ,
            {"AGENTS_HOST_DIR": str(Path(self.tmp.name) / "host")},
            clear=False,
        )
        self._env.start()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        self.up_port = self.upstream.server_address[1]
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        cfg = _cfg(state_dir=str(Path(self.tmp.name) / "relay"))
        self.relay = _Server(cfg)
        self.base = f"http://127.0.0.1:{self.up_port}"

    def tearDown(self):
        self.relay.close()
        self.upstream.shutdown()
        self._env.stop()
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

    def test_health_reports_version(self):
        import agents_relay

        r = self.relay.request("GET", "/health")
        body = json.loads(r.read())
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], agents_relay.__version__)
        # klanker_version only appears when a klanker-grafted serve reports it
        self.assertNotIn("klanker_version", body)

    def test_health_reports_klanker_version_when_grafted(self):
        from agents_relay import http_adapter

        class KlankerGraft(http_adapter.TurnHandler):
            klanker_version = "9.9.9-test"

        saved = http_adapter.TurnHandler
        http_adapter.TurnHandler = KlankerGraft
        try:
            server = http_adapter.serve_http(_cfg(state_dir=self.tmp.name))
        finally:
            http_adapter.TurnHandler = saved
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = HTTPConnection(*server.server_address[:2], timeout=10)
            conn.request("GET", "/health")
            body = json.loads(conn.getresponse().read())
            self.assertEqual(body["klanker_version"], "9.9.9-test")
        finally:
            server.shutdown()

    def test_proxy_health_passthrough(self):
        self.relay.request("POST", "/v1/remotes", {
            "id": "vps", "base_url": self.base, "secret": "s",
        })
        r = self.relay.request("GET", "/v1/remote/vps/health")
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(r.read())["path"], "/health")

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

    def test_metadata_and_secret_split_and_legacy_fallback(self):
        import warnings
        from agents_workspace import resolve_secret
        from agents_relay import remotes as remotes_mod

        r = self.relay.request("POST", "/v1/remotes", {
            "id": "vps", "base_url": self.base, "secret": "vault-tok", "label": "VPS",
        })
        self.assertEqual(r.status, 200)
        meta_path = Path(self.tmp.name) / "host" / "clients" / "remotes.json"
        self.assertTrue(meta_path.is_file())
        raw_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertNotIn("secret", raw_meta["remotes"]["vps"])
        self.assertEqual(raw_meta["remotes"]["vps"]["label"], "VPS")
        self.assertEqual(resolve_secret("REMOTE_VPS_TOKEN", scope="host"), "vault-tok")

        r = self.relay.request("DELETE", "/v1/remotes/vps")
        self.assertEqual(r.status, 200)
        self.assertEqual(resolve_secret("REMOTE_VPS_TOKEN", scope="host"), "")

        # Legacy 1-release fallback read when <host>/clients/remotes.json is absent.
        meta_path.unlink()
        legacy_dir = Path(self.tmp.name) / "relay"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "remotes.json").write_text(
            json.dumps({"remotes": {"old": {"base_url": self.base, "secret": "legacy-tok", "label": "Old"}}}),
            encoding="utf-8",
        )
        cfg = _cfg(state_dir=str(legacy_dir))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            listed = remotes_mod.list_public(cfg)
            self.assertEqual([x["id"] for x in listed], ["old"])
            self.assertTrue(listed[0]["has_secret"])
            self.assertEqual(remotes_mod.load_remotes(cfg)["old"]["secret"], "legacy-tok")
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))


if __name__ == "__main__":
    unittest.main()
