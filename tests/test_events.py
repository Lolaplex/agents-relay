"""Live /v1/events SSE bus tests."""
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

from agents_relay import events
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http


def _cfg(**overrides) -> RelayConfig:
    base = dict(
        loop_cmd=("python", "-c", "print('skip')"),
        loop_provider="openai.default",
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


class _Srv:
    def __init__(self, cfg: RelayConfig) -> None:
        self.server = serve_http(cfg)
        self.server.events_heartbeat_sec = 0.05  # type: ignore[attr-defined]
        self.host, self.port = self.server.server_address  # type: ignore[misc]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class TestEventsSSE(unittest.TestCase):
    def setUp(self) -> None:
        events.reset_bus()  # module bus may carry _closed from elsewhere

    def tearDown(self) -> None:
        events.shutdown_all()
        events.reset_bus()

    def test_events_requires_auth(self):
        srv = _Srv(_cfg())
        try:
            conn = HTTPConnection(srv.host, srv.port, timeout=3)
            conn.request("GET", "/v1/events")
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 401)
            conn.close()
        finally:
            srv.close()

    def test_events_receives_workspace_and_remote_mutations_and_shuts_down_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"AGENTS_HOST_DIR": td}):
                srv = _Srv(_cfg())
                try:
                    stream = HTTPConnection(srv.host, srv.port, timeout=3)
                    stream.request(
                        "GET",
                        "/v1/events",
                        headers={"Authorization": "Bearer expected"},
                    )
                    resp = stream.getresponse()
                    self.assertEqual(resp.status, 200)
                    first_line = resp.readline()
                    self.assertIn(b": connected", first_line)

                    api = HTTPConnection(srv.host, srv.port, timeout=3)
                    payload = json.dumps({"id": "ws-live", "label": "Live"}).encode("utf-8")
                    api.request(
                        "POST",
                        "/v1/workspaces",
                        body=payload,
                        headers={
                            "Authorization": "Bearer expected",
                            "Content-Type": "application/json",
                        },
                    )
                    ws_resp = api.getresponse()
                    ws_resp.read()
                    self.assertEqual(ws_resp.status, 201)
                    api.close()

                    captured = []
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline:
                        line = resp.readline()
                        if not line:
                            break
                        captured.append(line.decode("utf-8", errors="replace"))
                        if "workspace.changed" in captured[-1]:
                            # drain up to blank terminator
                            while True:
                                tail = resp.readline().decode("utf-8", errors="replace")
                                captured.append(tail)
                                if tail.strip() == "":
                                    break
                            break
                    joined = "".join(captured)
                    self.assertIn("event: workspace.changed", joined)
                    self.assertIn('"workspace": "ws-live"', joined)
                    stream.close()
                finally:
                    t0 = time.monotonic()
                    srv.close()
                    self.assertLess(time.monotonic() - t0, 1.5)

    def test_events_workspace_filter_isolates_other_workspaces(self):
        sub_a = events.subscribe(workspace="ws-a")
        try:
            events.emit_event("workspace.changed", {"n": 1}, workspace="ws-b")
            events.emit_event("workspace.changed", {"n": 2}, workspace="ws-a")
            got = sub_a.queue.get(timeout=1.0)
            self.assertIsNotNone(got)
            self.assertEqual(got["workspace"], "ws-a")  # type: ignore[index]
            self.assertEqual(got["payload"]["n"], 2)  # type: ignore[index]
            self.assertTrue(sub_a.queue.empty())
        finally:
            events.unsubscribe(sub_a)

    def test_global_events_never_reach_scoped_subscribers(self):
        """agents-relay#42: global (workspace="") events stay out of
        ws-scoped subscriptions unless include_global opted in."""
        sub = events.subscribe(workspace="ws-a")
        sub_all = events.subscribe(workspace="ws-a", include_global=True)
        try:
            events.emit_event("device.connected", {"d": 1})  # global
            events.emit_event("workspace.changed", {"n": 2},
                              workspace="ws-a")
            first = sub.queue.get(timeout=1.0)
            # global event skipped — the ws event arrives first
            self.assertEqual(first["kind"], "workspace.changed")
            self.assertTrue(sub.queue.empty())
            got = [sub_all.queue.get(timeout=1.0),
                   sub_all.queue.get(timeout=1.0)]
            self.assertEqual([g["kind"] for g in got],
                             ["device.connected", "workspace.changed"])
        finally:
            events.unsubscribe(sub)
            events.unsubscribe(sub_all)

    def test_disconnect_tag_poisons_device_subscriptions(self):
        """Revocation kills a device's event streams immediately."""
        sub = events.subscribe(workspace="w1", tag="dev-1")
        other = events.subscribe(workspace="w1", tag="dev-2")
        try:
            self.assertEqual(events.disconnect_tag("dev-1"), 1)
            self.assertIsNone(sub.queue.get(timeout=1.0))
            # untouched subscriber still receives events
            events.emit_event("workspace.changed", {"n": 1},
                              workspace="w1")
            self.assertIsNotNone(other.queue.get(timeout=1.0))
            self.assertEqual(events.disconnect_tag("dev-1"), 0)
        finally:
            events.unsubscribe(sub)
            events.unsubscribe(other)
