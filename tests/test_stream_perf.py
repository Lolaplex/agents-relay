"""Streaming perf: OS-chunk reads, split UTF-8, split trailer, keepalives."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_relay.config import RelayConfig
from agents_relay.turn import LOOP_TRAILER_MARKER, StreamEvent, iter_loop_stream, iter_loop_stream_live


def _cfg(tmp: str) -> RelayConfig:
    return RelayConfig(
        loop_cmd=("echo",),
        loop_provider="echo",
        relay_secret="",
        telegram_bot_token="",
        telegram_allowed_chat_ids=(),
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
        state_dir=str(Path(tmp) / "relay"),
    )


class _ChunkStream:
    """Binary stream doling out pre-set byte chunks (then EOF)."""

    def __init__(self, chunks: list[bytes], hold: bool = False):
        self._chunks = list(chunks)
        self._hold = hold
        self.closed = False

    def read1(self, n: int) -> bytes:
        while self._hold and not self._chunks and not self.closed:
            import time

            time.sleep(0.01)
        if not self._chunks:
            return b""
        block = self._chunks.pop(0)
        return block[:n] if len(block) > n else block

    def fileno(self):
        raise OSError("no fd")


class FakeProc:
    def __init__(self, out: list[bytes], err: list[bytes] | None = None, hold: bool = False):
        self.stdout = _ChunkStream(out, hold=hold)
        self.stderr = _ChunkStream(err or [])
        self.pid = 4242
        self.returncode = 0
        self.args = []
        self._dead = False

    def poll(self):
        return 0 if self._dead else None

    def wait(self, timeout=None):
        self._dead = True
        return 0

    def kill(self):
        self._dead = True
        self.stdout.closed = True

    def communicate(self, input=None, timeout=None):
        return (b"", b"")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._dead = True
        self.stdout.closed = True
        return False


def _deltas(events):
    return [e.text for e in events if e.type == "delta"]


class IterLoopStreamTests(unittest.TestCase):
    def test_chunked_reads_split_utf8_and_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            trailer = json.dumps({"session": "s1", "user_id": "u1", "alias": "a"})
            text = "héllo wörld €"
            marker = LOOP_TRAILER_MARKER
            raw = (text + marker + "\n" + trailer + "\n").encode("utf-8")
            # Split mid-multibyte (é/€ are 2-3 bytes) and inside the marker.
            chunks = [raw[:5], raw[5:9], raw[9:30], raw[30: len(raw) - 10], raw[len(raw) - 10:]]
            proc = FakeProc(chunks)
            with patch("agents_relay.turn.subprocess.Popen", return_value=proc):
                with patch("agents_relay.turn._container_rewrite", side_effect=lambda a, w, wd, e=None: (a, None, None)):
                    events = list(iter_loop_stream(
                        __import__("agents_relay.turn", fromlist=["TurnParams"]).TurnParams(
                            channel="http", user="u", message="hi"),
                        config=_cfg(tmp),
                    ))
        body = "".join(_deltas(events))
        self.assertEqual(body, text + "…" if False else text)
        trailers = [e for e in events if e.type == "trailer"]
        self.assertEqual(len(trailers), 1)
        self.assertEqual(trailers[0].session, "s1")
        self.assertNotIn(marker, body)


class IterLoopStreamLiveTests(unittest.TestCase):
    def _params(self):
        from agents_relay.turn import TurnParams

        return TurnParams(channel="web", user="w", message="hi")

    def test_live_batches_deltas(self):
        with tempfile.TemporaryDirectory() as tmp:
            trailer = json.dumps({"session": "s2"})
            raw = ("abc\ndef\n" + LOOP_TRAILER_MARKER + "\n" + trailer + "\n").encode()
            chunks = [bytes([b]) for b in raw]  # 1-byte writes like the old code
            proc = FakeProc(chunks)
            with (
                patch("agents_relay.turn.subprocess.Popen", return_value=proc),
                patch("agents_relay.turn._container_rewrite", side_effect=lambda a, w, wd, e=None: (a, None, None)),
            ):
                events = list(iter_loop_stream_live(self._params(), _cfg(tmp)))
        text = "".join(_deltas(events))
        self.assertEqual(text, "abc\ndef\n")
        deltas = [e for e in events if e.type == "delta"]
        # Batched: far fewer delta frames than input bytes.
        self.assertLess(len(deltas), 10)
        self.assertTrue(any(e.type == "trailer" for e in events))

    def test_live_keepalive_when_quiet(self):
        import threading
        import time

        with tempfile.TemporaryDirectory() as tmp:
            proc = FakeProc([], hold=True)
            cancel = threading.Event()
            with (
                patch("agents_relay.turn.subprocess.Popen", return_value=proc),
                patch("agents_relay.turn._container_rewrite", side_effect=lambda a, w, wd, e=None: (a, None, None)),
                patch("agents_relay.turn._KEEPALIVE_SEC", 0.05),
            ):
                gen = iter_loop_stream_live(self._params(), _cfg(tmp), cancel=cancel)
                threading.Timer(0.2, cancel.set).start()
                events = list(gen)
        self.assertTrue(any(e.type == "keepalive" for e in events))

    def test_live_stderr_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            trailer = json.dumps({"session": "s4"})
            out = ("ok" + LOOP_TRAILER_MARKER + "\n" + trailer + "\n").encode()
            err = [b"[*] Running 'tool'...\npart", b"ial line\nmore\n"]
            proc = FakeProc([out], err)
            with (
                patch("agents_relay.turn.subprocess.Popen", return_value=proc),
                patch("agents_relay.turn._container_rewrite", side_effect=lambda a, w, wd, e=None: (a, None, None)),
            ):
                events = list(iter_loop_stream_live(self._params(), _cfg(tmp)))
        statuses = [e.text for e in events if e.type == "status"]
        self.assertIn("[*] Running 'tool'...", statuses)
        self.assertIn("partial line", statuses)
        self.assertIn("more", statuses)


if __name__ == "__main__":
    unittest.main()
