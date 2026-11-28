"""W8 ACP executable resolution, loud crash surfacing, and agent detection."""

from __future__ import annotations

import json
import os
import sys
import threading
import types
from http.client import HTTPConnection
from pathlib import Path

import pytest

from agents_relay import acp
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.turn import (
    StreamEvent,
    TurnParams,
    build_loop_argv,
    iter_loop_stream,
    iter_loop_stream_live,
)


FAKE_ACP_AGENT = r'''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}}), flush=True)
    elif method == "session/new":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "acp_fake"}}), flush=True)
    elif method == "session/load":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {}}), flush=True)
'''


def _cfg(tmp_path: Path, *, loop_cmd: tuple[str, ...] = ("python", "-m", "runner.loop"), secret: str = "") -> RelayConfig:
    return RelayConfig(
        loop_cmd=loop_cmd,
        loop_provider="echo",
        relay_secret=secret,
        telegram_bot_token="",
        telegram_allowed_chat_ids=(),
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
        state_dir=str(tmp_path / "relay"),
    )


def _write_provider(tmp_path: Path, name: str, kind: str, **extra) -> None:
    providers = tmp_path / "host" / "providers"
    providers.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "kind": kind,
        "when": "on_request",
        "cadence": "on_request",
        "verb": "chat.completions",
        "rests_on": "test provider manifest",
        "expected_exit": 0,
        **extra,
    }
    (providers / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


class TestResolveAgentExe:
    def test_direct_file_path_wins(self, tmp_path: Path) -> None:
        exe = tmp_path / "bin with space" / "devin.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"x")
        assert acp.resolve_agent_exe(str(exe)) == os.path.abspath(exe)

    def test_registry_paths_expand_user_then_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Key:
            def __init__(self, value: str) -> None:
                self.value = value

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> bool:
                return False

        class FakeWinreg:
            HKEY_CURRENT_USER = "HKCU"
            HKEY_LOCAL_MACHINE = "HKLM"

            def __init__(self) -> None:
                self.values = {
                    (self.HKEY_CURRENT_USER, "Environment"): r"%CLI_ROOT%\user;C:\UserBin",
                    (
                        self.HKEY_LOCAL_MACHINE,
                        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                    ): r"C:\MachineBin;%CLI_ROOT%\machine",
                }

            def OpenKey(self, root, subkey):
                value = self.values[(root, subkey)]
                return Key(value)

            def QueryValueEx(self, key, name):
                assert name == "Path"
                return key.value, 1

        fake = FakeWinreg()
        monkeypatch.setitem(sys.modules, "winreg", fake)
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setenv("CLI_ROOT", r"C:\cli")
        assert acp._windows_registry_paths() == [
            r"C:\cli\user;C:\UserBin",
            r"C:\MachineBin;C:\cli\machine",
        ]

    def test_registry_path_wins_before_vendor_fallback(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        registry_bin = tmp_path / "registry-bin"
        vendor_bin = tmp_path / "vendor-bin"
        registry_bin.mkdir()
        vendor_bin.mkdir()
        resolved = registry_bin / "devin.EXE"
        resolved.write_bytes(b"x")
        calls: list[str | None] = []

        def fake_which(cmd: str, path: str | None = None):
            calls.append(path)
            if path and str(registry_bin) in path.split(os.pathsep):
                return str(resolved)
            if path and str(vendor_bin) in path.split(os.pathsep):
                return str(vendor_bin / "devin.EXE")
            return None

        monkeypatch.setattr(acp.shutil, "which", fake_which)
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(acp, "_windows_registry_paths", lambda: [str(registry_bin)])
        monkeypatch.setattr(acp, "_fallback_cli_dirs", lambda: [str(vendor_bin)])
        assert acp.resolve_agent_exe("devin") == os.path.abspath(resolved)
        assert len(calls) == 2
        assert calls[0] is None
        assert calls[1] == str(registry_bin)

    def test_vendor_fallback_after_path_and_registry_miss(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        vendor_bin = tmp_path / "vendor-bin"
        vendor_bin.mkdir()
        resolved = vendor_bin / "devin.EXE"
        resolved.write_bytes(b"x")

        def fake_which(cmd: str, path: str | None = None):
            if path and str(vendor_bin) in path.split(os.pathsep):
                return str(resolved)
            return None

        monkeypatch.setattr(acp.shutil, "which", fake_which)
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(acp, "_windows_registry_paths", lambda: [])
        monkeypatch.setattr(acp, "_fallback_cli_dirs", lambda: [str(vendor_bin)])
        assert acp.resolve_agent_exe("devin") == os.path.abspath(resolved)

    def test_windows_vendor_dirs_are_ordered_and_must_exist(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        local = tmp_path / "local"
        profile = tmp_path / "profile"
        appdata = tmp_path / "appdata"
        devin_bin = local / "devin" / "cli" / "bin"
        npm_bin = appdata / "npm"
        devin_bin.mkdir(parents=True)
        npm_bin.mkdir(parents=True)
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.setenv("USERPROFILE", str(profile))
        monkeypatch.setenv("APPDATA", str(appdata))
        assert acp._fallback_cli_dirs() == [str(devin_bin), str(npm_bin)]


class TestAcpCrashSurfacing:
    def test_missing_agent_spawn_is_one_loud_stderr_line(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        cfg = _cfg(tmp_path)
        rc = acp.run_acp_turn(
            agent_argv=["definitely-not-an-acp-agent", "acp"],
            message="hello",
            session="ses_missing",
            workdir=str(tmp_path),
            config=cfg,
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err.strip() == (
            "ACP error: agent command not on PATH: definitely-not-an-acp-agent"
        )
        assert "Traceback" not in captured.err
        assert "---agents-loop-trailer---" not in captured.out

    def test_end_to_end_acp_spawn_crash_reaches_stream_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        missing = "definitely-not-an-acp-agent"
        monkeypatch.setenv("AGENTS_ACP_CMD_DEVIN", f"{missing} acp")
        detail = f"ACP error: agent command not on PATH: {missing}"
        events = list(
            iter_loop_stream_live(
                TurnParams(channel="web", user="u", message="hi", provider="acp.devin"),
                _cfg(tmp_path),
                timeout_sec=10,
            )
        )
        assert detail in [event.text for event in events if event.type == "error"]
        trailer = [event for event in events if event.type == "trailer"][0]
        assert trailer.returncode == 1
        assert trailer.data["error"] == detail

    def test_nonzero_stream_without_reply_emits_error_and_trailer(self, tmp_path: Path) -> None:
        detail = "ACP error: agent command not on PATH: devin"
        crash = tmp_path / "crash_loop.py"
        crash.write_text(
            "import sys\n"
            f"print({detail!r}, file=sys.stderr)\n"
            "raise SystemExit(3)\n",
            encoding="utf-8",
        )
        cfg = _cfg(tmp_path, loop_cmd=(sys.executable, str(crash)))
        events = list(
            iter_loop_stream_live(
                TurnParams(channel="web", user="u", message="hi"),
                cfg,
                timeout_sec=10,
            )
        )
        errors = [event.text for event in events if event.type == "error"]
        assert detail in errors
        trailers = [event for event in events if event.type == "trailer"]
        assert len(trailers) == 1
        assert trailers[0].returncode == 3
        assert trailers[0].data["error"] == detail
        assert not [event for event in events if event.type == "delta"]

    def test_legacy_stream_nonzero_without_reply_also_surfaces_stderr(self, tmp_path: Path) -> None:
        detail = "FileNotFoundError: agent command not on PATH: devin"
        crash = tmp_path / "crash_loop.py"
        crash.write_text(
            "import sys\n"
            f"print({detail!r}, file=sys.stderr)\n"
            "raise SystemExit(4)\n",
            encoding="utf-8",
        )
        cfg = _cfg(tmp_path, loop_cmd=(sys.executable, str(crash)))
        events = list(
            iter_loop_stream(
                TurnParams(channel="http", user="u", message="hi"),
                cfg,
                timeout_sec=10,
            )
        )
        assert detail in [event.text for event in events if event.type == "error"]
        trailer = [event for event in events if event.type == "trailer"][0]
        assert trailer.returncode == 4
        assert trailer.data["error"] == detail

    def test_web_trailer_payload_carries_error(self) -> None:
        from agents_relay.web import _event_payload

        entry = {"meta": {}}
        event = StreamEvent(
            type="trailer",
            session="ses_x",
            returncode=3,
            data={"error": "boom"},
        )
        payload = _event_payload(entry, event)
        assert payload is not None
        assert payload["returncode"] == 3
        assert payload["error"] == "boom"


class TestAcpProviderRouting:
    def test_legacy_kind_acp_stub_does_not_route_to_acp_run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _write_provider(tmp_path, "acp.cursor", "acp")
        monkeypatch.delenv("AGENTS_ACP_CMD", raising=False)
        monkeypatch.delenv("AGENTS_ACP_CMD_CURSOR", raising=False)
        cfg = _cfg(tmp_path)
        argv = build_loop_argv(TurnParams(provider="acp.cursor"), cfg)
        assert argv[:3] == ["python", "-m", "runner.loop"]
        assert argv[:4] != [sys.executable, "-m", "agents_relay", "acp-run"]

    def test_kind_acp_agent_manifest_routes_to_acp_run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _write_provider(tmp_path, "acp.devin", "acp-agent", command="devin acp")
        monkeypatch.delenv("AGENTS_ACP_CMD", raising=False)
        monkeypatch.delenv("AGENTS_ACP_CMD_DEVIN", raising=False)
        cfg = _cfg(tmp_path)
        argv = build_loop_argv(TurnParams(provider="acp.devin"), cfg)
        assert argv[:4] == [sys.executable, "-m", "agents_relay", "acp-run"]


class _Server:
    def __init__(self, cfg: RelayConfig) -> None:
        self.server = serve_http(cfg)
        self.host, self.port = self.server.server_address  # type: ignore[misc]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, headers: dict[str, str] | None = None):
        conn = HTTPConnection(self.host, self.port, timeout=10)
        conn.request(method, path, headers=headers or {})
        res = conn.getresponse()
        raw = res.read() or b"{}"
        conn.close()
        try:
            return res.status, json.loads(raw)
        except json.JSONDecodeError:
            return res.status, {"error": raw.decode("utf-8", "replace")}


class TestAgentsEndpoint:
    def test_auth_shape_and_detection(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        resolved_devin = r"C:\tools\devin.EXE" if os.name == "nt" else "/opt/tools/devin"
        monkeypatch.setattr(
            acp,
            "resolve_agent_exe",
            lambda binary: resolved_devin if binary == "devin" else None,
        )
        srv = _Server(_cfg(tmp_path, secret="expected"))
        try:
            status, _ = srv.request("GET", "/v1/providers/agents")
            assert status == 401

            from agents_relay import devices

            client = devices.mint_device(label="client", role="client")
            status, _ = srv.request(
                "GET",
                "/v1/providers/agents",
                headers={"Authorization": f"Bearer {client['device_token']}"},
            )
            assert status == 403
            member = devices.mint_device(label="member", role="ws_member", allowlist=["default"])
            status, _ = srv.request(
                "GET",
                "/v1/providers/agents",
                headers={"Authorization": f"Bearer {member['device_token']}"},
            )
            assert status == 403

            admin = {"X-Relay-Secret": "expected"}
            status, data = srv.request("GET", "/v1/providers/agents", headers=admin)
            assert status == 200
            assert data["ok"] is True
            rows = data["agents"]
            assert [row["id"] for row in rows] == ["devin", "claude", "codex"]
            devin = rows[0]
            assert devin["provider_name"] == "acp.devin"
            assert devin["label"] == "Devin CLI"
            assert devin["command"] == "devin acp"
            assert devin["binary"] == "devin"
            assert devin["installed"] is True
            assert devin["resolved_path"] == resolved_devin
            assert devin["configured"] is False
            assert rows[1]["installed"] is False
            assert rows[1]["resolved_path"] is None
            assert rows[2]["installed"] is False
            for row in rows:
                assert set(row) == {
                    "id",
                    "provider_name",
                    "label",
                    "command",
                    "binary",
                    "installed",
                    "resolved_path",
                    "configured",
                    "description",
                }

            _write_provider(tmp_path, "acp.devin", "acp-agent", command="devin acp")
            status, data = srv.request("GET", "/v1/providers/agents", headers=admin)
            assert status == 200
            assert data["agents"][0]["configured"] is True
        finally:
            srv.close()

    def test_channel_rpc_denies_agents_endpoint(self) -> None:
        from agents_relay.channel_client import _rpc_denied

        assert _rpc_denied("GET", "/v1/providers/agents")

    def test_probe_acp_devin_uses_vendor_resolution(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        agent = tmp_path / "fake_agent.py"
        agent.write_text(FAKE_ACP_AGENT, encoding="utf-8")
        empty_path = tmp_path / "empty-path"
        empty_path.mkdir()
        monkeypatch.setenv("PATH", str(empty_path))
        monkeypatch.setattr(acp, "_windows_registry_paths", lambda: [])
        monkeypatch.delenv("AGENTS_ACP_CMD", raising=False)
        monkeypatch.delenv("AGENTS_ACP_CMD_DEVIN", raising=False)

        if os.name == "nt":
            local = tmp_path / "local"
            bin_dir = local / "devin" / "cli" / "bin"
            bin_dir.mkdir(parents=True)
            shim = bin_dir / "devin.cmd"
            shim.write_text(
                f'@echo off\r\n"{sys.executable}" "{agent}" %*\r\n',
                encoding="ascii",
            )
            monkeypatch.setenv("LOCALAPPDATA", str(local))
            monkeypatch.setenv("USERPROFILE", str(tmp_path / "no-profile"))
            monkeypatch.setenv("APPDATA", str(tmp_path / "no-appdata"))
        else:
            home = tmp_path / "home"
            bin_dir = home / ".local" / "bin"
            bin_dir.mkdir(parents=True)
            shim = bin_dir / "devin"
            shim.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{agent}" "$@"\n',
                encoding="ascii",
            )
            shim.chmod(0o755)
            monkeypatch.setenv("HOME", str(home))

        _write_provider(tmp_path, "acp.devin", "acp-agent", command="devin acp")
        srv = _Server(_cfg(tmp_path, secret="expected"))
        try:
            status, data = srv.request(
                "GET",
                "/v1/providers/acp.devin/probe",
                headers={"X-Relay-Secret": "expected"},
            )
        finally:
            srv.close()
        assert status == 200
        assert data["status"] == "ok", data
        assert data["models"] == []
