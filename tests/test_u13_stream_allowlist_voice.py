import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_relay import devices
from agents_relay.channel_client import _rpc_denied
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.turn import StreamEvent, TurnParams, iter_loop_stream_live
from agents_workspace import delete_secret


class _FakePipe:
    def __init__(self, chunks: list[bytes], wait_empty_other: "_FakePipe | None" = None):
        self._chunks = list(chunks)
        self._wait_other = wait_empty_other

    def read1(self, _size: int = 4096) -> bytes:
        if self._wait_other is not None:
            import time

            deadline = time.monotonic() + 1.0
            while self._wait_other._chunks and time.monotonic() < deadline:
                time.sleep(0.005)
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def read(self, _size: int = -1) -> bytes:
        return self.read1(_size)


class _FakeProc:
    def __init__(self, stdout_chunks: list[bytes], stderr_chunks: list[bytes], rc: int = 0):
        self.stderr = _FakePipe(stderr_chunks)
        self.stdout = _FakePipe(stdout_chunks, wait_empty_other=self.stderr)
        self.pid = 424242
        self._rc = rc
        self.returncode = rc

    def poll(self):
        return None if (self.stdout._chunks or self.stderr._chunks) else self._rc

    def wait(self, timeout=None):
        return self._rc


def _cfg(**overrides) -> RelayConfig:
    base = dict(
        loop_cmd=("python", "-c", "print('skip')"),
        loop_provider="openai.default",
        relay_secret="sec-u13",
        telegram_bot_token="",
        telegram_allowed_chat_ids=(),
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
    )
    base.update(overrides)
    return RelayConfig(**base)


def _start_srv(cfg: RelayConfig):
    srv = serve_http(cfg)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


def _req(port: int, method: str, path: str, *, body: bytes | dict | None = None, headers: dict | None = None):
    hdrs = dict(headers or {})
    raw = b""
    if isinstance(body, dict):
        raw = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif isinstance(body, bytes):
        raw = body
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=raw, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    status = resp.status
    ctype = resp.getheader("Content-Type") or ""
    conn.close()
    return status, ctype, data


class TestU13StructuredTurnStream(unittest.TestCase):
    def test_iter_loop_stream_live_parses_sideband_events(self):
        stderr = [
            b"thinking...\n",
            b'__KLANKER_EVT__ {"type":"thought","text":"analyzing"}\n',
            b'__KLANKER_EVT__ {"type":"tool_start","id":"c1","tool":"mcp_terminal_list","args_preview":"{}"}\n',
            b'__KLANKER_EVT__ {malformed json\n',
            b'__KLANKER_EVT__ {"type":"tool_end","id":"c1","tool":"mcp_terminal_list","ok":true,"duration_ms":12.5,"result_preview":"ok"}\n',
            b'__KLANKER_EVT__ {"type":"usage","tokens_in":15,"tokens_out":9,"est_cost_usd":0.0002,"model":"gpt-x"}\n',
        ]
        stdout = [b'hello\n---agents-loop-trailer---\n{"session":"ses_1","user_id":"u_1","alias":"a"}\n']
        proc = _FakeProc(stdout, stderr)
        cfg = RelayConfig.from_env()
        with patch("agents_relay.turn._spawn_stream", return_value=(proc, None)):
            evts = list(iter_loop_stream_live(TurnParams(channel="web", user="u1", message="hi"), cfg))
        kinds = [e.type for e in evts]
        self.assertIn("status", kinds)
        self.assertIn("thought", kinds)
        self.assertIn("tool_start", kinds)
        self.assertIn("tool_end", kinds)
        self.assertIn("usage", kinds)
        self.assertIn("delta", kinds)
        self.assertEqual(kinds[-1], "trailer")
        status_texts = [e.text for e in evts if e.type == "status"]
        self.assertEqual(status_texts, ["thinking..."])

    def test_chat_stream_sse_emits_typed_events(self):
        def _fake_stream(_params, _cfg, *, cancel=None, on_pid=None):
            yield StreamEvent(type="status", text="thinking...")
            yield StreamEvent(type="thought", text="deep check", data={"type": "thought", "text": "deep check"})
            yield StreamEvent(
                type="tool_start",
                data={"type": "tool_start", "id": "t1", "tool": "mcp_terminal_read", "args_preview": "{}"},
            )
            yield StreamEvent(
                type="tool_end",
                data={"type": "tool_end", "id": "t1", "tool": "mcp_terminal_read", "ok": True, "duration_ms": 8.0, "result_preview": "x"},
            )
            yield StreamEvent(
                type="usage",
                data={"type": "usage", "tokens_in": 20, "tokens_out": 10, "est_cost_usd": 0.001, "model": "m1"},
            )
            yield StreamEvent(type="delta", text="Answer")
            yield StreamEvent(type="trailer", session="ses_sse", user_id="u1", alias="a1", returncode=0)

        cfg = _cfg()
        srv, port = _start_srv(cfg)
        try:
            with patch("agents_relay.web.iter_loop_stream_live", side_effect=_fake_stream):
                status, ctype, raw = _req(
                    port,
                    "POST",
                    "/v1/chat/stream",
                    body={"text": "hi", "session": "ses_sse"},
                    headers={"X-Relay-Secret": "sec-u13"},
                )
            self.assertEqual(status, 200)
            self.assertIn("text/event-stream", ctype)
            frames = []
            for line in raw.decode("utf-8").splitlines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
            types = [f["type"] for f in frames]
            self.assertIn("thought", types)
            self.assertIn("tool_start", types)
            self.assertIn("tool_end", types)
            self.assertIn("usage", types)
            self.assertIn("delta", types)
            self.assertIn("trailer", types)
            usage_frame = next(f for f in frames if f["type"] == "usage")
            self.assertEqual(usage_frame["tokens_in"], 20)
            self.assertEqual(usage_frame["tokens_out"], 10)
        finally:
            srv.shutdown()
            srv.server_close()


class TestU13AllowlistAndVoice(unittest.TestCase):
    def test_rpc_denied_blocks_new_admin_and_voice_paths(self):
        for p in (
            "/v1/approvals/allowlist",
            "/v1/voice/transcribe",
            "/v1/voice/speak",
            "/v1/klanker/doctor",
            "/v1/klanker/bg",
            "/v1/klanker/files",
            "/v1/klanker/stats",
            "/v1/klanker/sessions/fork",
        ):
            self.assertTrue(_rpc_denied("GET", p), f"expected _rpc_denied(GET, {p})")
            self.assertTrue(_rpc_denied("POST", p), f"expected _rpc_denied(POST, {p})")

    def test_allowlist_list_delete_and_rbac(self):
        cfg = _cfg()
        with tempfile.TemporaryDirectory() as tmp:
            allow_file = Path(tmp) / "allowlist.json"
            allow_file.write_text(json.dumps({"patterns": ["mcp_terminal_run pytest*", "mcp_terminal_read *"]}), encoding="utf-8")
            rec = devices.mint_device(label="laptop", role="client")
            member_rec = devices.mint_device(label="guest", role="ws_member", allowlist=["default"])
            dev_tok = rec["device_token"]
            dev_id = rec["device_id"]
            mem_tok = member_rec["device_token"]
            mem_id = member_rec["device_id"]
            srv, port = _start_srv(cfg)
            try:
                with patch.dict(os.environ, {"AGENTS_APPROVAL_ALLOWLIST": str(allow_file)}):
                    # Unauth -> 401
                    st, _, _ = _req(port, "GET", "/v1/approvals/allowlist")
                    self.assertEqual(st, 401)
                    # ws_member / client devices -> 403
                    st, _, _ = _req(port, "GET", "/v1/approvals/allowlist", headers={"Authorization": f"Bearer {mem_tok}", "X-Device-Id": mem_id})
                    self.assertEqual(st, 403)
                    st, _, _ = _req(port, "GET", "/v1/approvals/allowlist", headers={"Authorization": f"Bearer {dev_tok}", "X-Device-Id": dev_id})
                    self.assertEqual(st, 403)
                    # Admin GET -> 200
                    st, _, raw = _req(port, "GET", "/v1/approvals/allowlist", headers={"X-Relay-Secret": "sec-u13"})
                    self.assertEqual(st, 200)
                    self.assertEqual(json.loads(raw)["patterns"], ["mcp_terminal_run pytest*", "mcp_terminal_read *"])
                    # Device DELETE -> 403
                    st, _, _ = _req(
                        port,
                        "DELETE",
                        "/v1/approvals/allowlist",
                        body={"pattern": "mcp_terminal_run pytest*"},
                        headers={"Authorization": f"Bearer {dev_tok}", "X-Device-Id": dev_id},
                    )
                    self.assertEqual(st, 403)
                    # Admin DELETE -> 200
                    st, _, raw = _req(
                        port,
                        "DELETE",
                        "/v1/approvals/allowlist",
                        body={"pattern": "mcp_terminal_run pytest*"},
                        headers={"X-Relay-Secret": "sec-u13"},
                    )
                    self.assertEqual(st, 200)
                    self.assertEqual(json.loads(raw)["patterns"], ["mcp_terminal_read *"])
                    # Missing -> 404
                    st, _, _ = _req(
                        port,
                        "DELETE",
                        "/v1/approvals/allowlist",
                        body={"pattern": "does-not-exist"},
                        headers={"X-Relay-Secret": "sec-u13"},
                    )
                    self.assertEqual(st, 404)
            finally:
                srv.shutdown()
                srv.server_close()
                delete_secret(f"DEVICE_{dev_id}_TOKEN", scope="host")
                delete_secret(f"DEVICE_{mem_id}_TOKEN", scope="host")

    def test_voice_endpoints_loud_503_size_limit_and_roundtrips(self):
        cfg_nokey = _cfg(groq_api_key="")
        srv, port = _start_srv(cfg_nokey)
        try:
            st, _, _ = _req(
                port,
                "POST",
                "/v1/voice/transcribe",
                body=b"webm-bytes",
                headers={"X-Relay-Secret": "sec-u13", "Content-Type": "audio/webm"},
            )
            self.assertEqual(st, 503)
            st, _, _ = _req(
                port,
                "POST",
                "/v1/voice/speak",
                body={"text": "hello"},
                headers={"X-Relay-Secret": "sec-u13"},
            )
            self.assertEqual(st, 503)
        finally:
            srv.shutdown()
            srv.server_close()

        cfg_ok = _cfg(groq_api_key="gsk-test")
        srv2, port2 = _start_srv(cfg_ok)
        try:
            # Oversized Content-Length -> 413
            conn = http.client.HTTPConnection("127.0.0.1", port2, timeout=10)
            conn.putrequest("POST", "/v1/voice/transcribe")
            conn.putheader("X-Relay-Secret", "sec-u13")
            conn.putheader("Content-Type", "audio/webm")
            conn.putheader("Content-Length", str(20 * 1024 * 1024))
            conn.endheaders()
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 413)
            conn.close()

            with (
                patch("agents_relay.voice.transcribe_audio", return_value="hello from mic") as mock_stt,
                patch("agents_relay.voice.synthesize_speech", return_value=b"ID3-MP3-DATA") as mock_tts,
            ):
                # Raw audio upload
                st, _, raw = _req(
                    port2,
                    "POST",
                    "/v1/voice/transcribe",
                    body=b"RAW-WEBM",
                    headers={"X-Relay-Secret": "sec-u13", "Content-Type": "audio/webm"},
                )
                self.assertEqual(st, 200)
                self.assertEqual(json.loads(raw)["text"], "hello from mic")
                self.assertTrue(mock_stt.called)

                # Multipart audio upload
                boundary = "----klankerbound"
                mp_body = (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="file"; filename="clip.wav"\r\n'
                    "Content-Type: audio/wav\r\n\r\n"
                    "RIFF-WAV-BYTES\r\n"
                    f"--{boundary}--\r\n"
                ).encode("utf-8")
                st, _, raw = _req(
                    port2,
                    "POST",
                    "/v1/voice/transcribe",
                    body=mp_body,
                    headers={
                        "X-Relay-Secret": "sec-u13",
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                    },
                )
                self.assertEqual(st, 200)
                self.assertEqual(json.loads(raw)["text"], "hello from mic")

                # TTS speak
                st, ctype, audio = _req(
                    port2,
                    "POST",
                    "/v1/voice/speak",
                    body={"text": "Say hi"},
                    headers={"X-Relay-Secret": "sec-u13"},
                )
                self.assertEqual(st, 200)
                self.assertEqual(ctype, "audio/mpeg")
                self.assertEqual(audio, b"ID3-MP3-DATA")
                self.assertTrue(mock_tts.called)
        finally:
            srv2.shutdown()
            srv2.server_close()


if __name__ == "__main__":
    unittest.main()
