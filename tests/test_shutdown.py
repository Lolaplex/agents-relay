"""`agents-relay serve` exits promptly on SIGTERM / SIGINT."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipIf(sys.platform == "win32", "POSIX signals")
class TestServeShutdown(unittest.TestCase):
    def _serve(self, home: str) -> tuple[subprocess.Popen, int]:
        port = _free_port()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": home,
            "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "RELAY_HOST": "127.0.0.1",
            "RELAY_PORT": str(port),
            "AGENTS_HOME": str(Path(home) / ".agents"),
            "AGENTS_NO_UPDATE_CHECK": "1",
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "agents_relay", "serve", "--no-telegram"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail(f"serve exited early: {proc.stderr.read()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                    if resp.status == 200:
                        return proc, port
            except OSError:
                time.sleep(0.1)
        proc.kill()
        self.fail("serve did not come up")

    def _assert_stops(self, sig: int) -> None:
        with tempfile.TemporaryDirectory() as home:
            proc, port = self._serve(home)
            started = time.monotonic()
            proc.send_signal(sig)
            try:
                rc = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail(f"serve did not exit on signal {sig}")
            elapsed = time.monotonic() - started
            err = proc.stderr.read()
            proc.stderr.close()
            self.assertLess(elapsed, 3.0, err)
            self.assertEqual(rc, 0, err)
            with self.assertRaises(OSError):
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)

    def test_sigterm(self) -> None:
        self._assert_stops(signal.SIGTERM)

    def test_sigint(self) -> None:
        self._assert_stops(signal.SIGINT)


if __name__ == "__main__":
    unittest.main()
