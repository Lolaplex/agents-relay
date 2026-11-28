"""W12C ACP probe-cache healing and auth-noise regression tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents_relay import acp
from agents_relay.config import RelayConfig
from agents_workspace import Host


def _cfg(tmp_path: Path, *, state_dir: str | None = None) -> RelayConfig:
    return RelayConfig(
        loop_cmd=("python", "-c", "print('skip')"),
        loop_provider="echo",
        relay_secret="",
        telegram_bot_token="",
        telegram_allowed_chat_ids=(),
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
        state_dir=str(tmp_path / "relay") if state_dir is None else state_dir,
    )


def _write_acp_provider(name: str = "acp.devin", command: str = "devin acp") -> None:
    providers = Host.from_env().dir / "providers"
    providers.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "kind": "acp-agent",
        "when": "on_request",
        "cadence": "on_request",
        "verb": "chat.completions",
        "rests_on": "test provider manifest",
        "expected_exit": 0,
        "command": command,
    }
    (providers / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def _clear_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "AGENTS_ACP_CMD",
        "AGENTS_ACP_NAME",
        "AGENTS_ACP_CMD_DEVIN",
        "AGENTS_ACP_API_KEY",
        "AGENTS_ACP_API_KEY_DEVIN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_agent_info_missing_executable_error_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENTS_ACP_CMD_DEVIN", "definitely-missing-w12c acp")
    cfg = _cfg(tmp_path)

    info = acp.acp_agent_info(cfg, "acp.devin", refresh=True)

    assert "agent command not on PATH: definitely-missing-w12c" in info["error"]
    assert not acp.acp_models_cache_path("acp.devin", cfg).exists()


def test_agent_info_probe_error_result_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_agent_env(monkeypatch)
    monkeypatch.setenv("AGENTS_ACP_CMD_DEVIN", "devin acp")
    monkeypatch.setattr(
        acp,
        "probe_acp_agent",
        lambda *_args, **_kwargs: {
            "error": "handshake failed",
            "model": "",
            "models": [],
        },
    )
    cfg = _cfg(tmp_path)

    info = acp.acp_agent_info(cfg, "acp.devin", refresh=True)

    assert info["error"] == "handshake failed"
    assert not acp.acp_models_cache_path("acp.devin", cfg).exists()


def test_provider_rows_unlink_stale_error_cache_when_executable_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_agent_env(monkeypatch)
    _write_acp_provider()
    monkeypatch.setattr(
        acp,
        "resolve_agent_exe",
        lambda exe: "/tmp/devin" if exe == "devin" else None,
    )
    cfg = _cfg(tmp_path)
    cache = acp.acp_models_cache_path("acp.devin", cfg)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"error": "agent command not on PATH: devin", "models": [], "ts": 1}),
        encoding="utf-8",
    )

    rows = acp.acp_provider_rows(cfg)
    row = next(r for r in rows if r["name"] == "acp.devin")

    assert not row.get("error")
    assert not row.get("models")
    assert not cache.exists()


def test_provider_rows_missing_executable_error_is_live_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_agent_env(monkeypatch)
    _write_acp_provider(command="definitely-missing-w12c acp")
    monkeypatch.setattr(acp, "resolve_agent_exe", lambda _exe: None)
    cfg = _cfg(tmp_path)

    rows = acp.acp_provider_rows(cfg)
    row = next(r for r in rows if r["name"] == "acp.devin")

    assert row["error"] == "agent command not on PATH: definitely-missing-w12c"
    assert not acp.acp_models_cache_path("acp.devin", cfg).exists()


def test_models_cache_path_without_state_dir_uses_absolute_host_cache(tmp_path: Path) -> None:
    expected = Host.from_env().cache / "acp_models" / "devin.json"

    for config in (None, SimpleNamespace(state_dir="")):
        path = acp.acp_models_cache_path("acp.devin", config)
        assert path == expected
        assert path.is_absolute()

    cfg = _cfg(tmp_path)
    assert acp.acp_models_cache_path("acp.devin", cfg) == (
        tmp_path / "relay" / "acp_models" / "devin.json"
    )


def _write_auth_agent(path: Path, *, session_error: bool) -> Path:
    path.write_text(
        """import json, sys

SESSION_ERROR = %r
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "authMethods": [{"id": "devin-browser"}],
        }}), flush=True)
    elif method == "session/new":
        if SESSION_ERROR:
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32000, "message": "auth required"}}), flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
                "sessionId": "acp_auth_stub"}}), flush=True)
    elif method == "session/prompt":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "stopReason": "end_turn"}}), flush=True)
"""
        % session_error,
        encoding="utf-8",
    )
    return path


def _run_auth_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    *,
    session_error: bool,
) -> tuple[int, str]:
    _clear_agent_env(monkeypatch)
    agent = _write_auth_agent(tmp_path / "auth_agent.py", session_error=session_error)
    rc = acp.run_acp_turn(
        agent_argv=[sys.executable, str(agent)],
        message="ping",
        session="ses_w12c_auth",
        workdir=str(tmp_path),
        config=_cfg(tmp_path),
        provider="acp.devin",
    )
    captured = capsys.readouterr()
    return rc, captured.err


def test_auth_methods_do_not_emit_speculative_hint_when_session_new_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    rc, err = _run_auth_turn(tmp_path, monkeypatch, capsys, session_error=False)

    assert rc == 0
    assert "acp agent requests auth" not in err


def test_auth_hint_is_emitted_when_session_new_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    rc, err = _run_auth_turn(tmp_path, monkeypatch, capsys, session_error=True)

    assert rc == 1
    assert "acp session/new failed" in err
    assert "acp agent requests auth (devin-browser)" in err
