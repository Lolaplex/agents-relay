"""W10B: turn directives wire passthrough + fast invalidating read cache."""

from __future__ import annotations

import json
import os
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

import agents_relay.providers_api as providers_api
import agents_relay.workspaces as workspaces_api
from agents_relay import acp, channel, devices
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.loop_client import LOOP_TRAILER_MARKER, LoopTurnResult
import agents_relay.loop_client as loop_client
from agents_relay.read_cache import cache_key, host_dir_key, relay_read_cache
from agents_relay.turn import (
    StreamEvent,
    TurnParams,
    build_loop_argv,
    iter_loop_stream_live,
    prepare_loop,
    turn_directive_error,
)
from agents_workspace import Host


def _cfg(tmp_path: Path, **overrides) -> RelayConfig:
    base = dict(
        loop_cmd=("python", "-m", "runner.loop"),
        loop_provider="echo",
        relay_secret="expected",
        telegram_bot_token="",
        telegram_allowed_chat_ids=(),
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
        state_dir=str(tmp_path / "relay"),
        web_token="tok",
    )
    base.update(overrides)
    return RelayConfig(**base)


class _Server:
    def __init__(self, cfg: RelayConfig, *, on_turn=None) -> None:
        self.server = serve_http(cfg, on_turn=on_turn)
        self.host, self.port = self.server.server_address  # type: ignore[misc]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        conn = HTTPConnection(self.host, self.port, timeout=10)
        payload = json.dumps(body) if body is not None else None
        hdrs = {"Content-Type": "application/json"} if payload else {}
        hdrs.update(headers or {})
        conn.request(method, path, body=payload, headers=hdrs)
        res = conn.getresponse()
        raw = res.read()
        status = res.status
        response_headers = dict(res.getheaders())
        conn.close()
        return status, raw, response_headers

    def json(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        status, raw, _ = self.request(method, path, body, headers)
        try:
            return status, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return status, {"error": raw.decode("utf-8", "replace")}


@pytest.fixture(autouse=True)
def _clean_read_cache():
    relay_read_cache.clear()
    yield
    relay_read_cache.clear()


def _auth() -> dict[str, str]:
    return {"X-Relay-Secret": "expected"}


# ---------------------------------------------------------------------------
# turn_mode / reasoning validation + loop forwarding


@pytest.mark.parametrize("mode", ["auto", "fast", "agent", "deep"])
def test_turn_mode_allowlist_accepts_all_wire_values(mode: str) -> None:
    assert turn_directive_error({"turn_mode": mode}) == ""


@pytest.mark.parametrize("level", ["auto", "off", "low", "medium", "high"])
def test_reasoning_allowlist_accepts_all_wire_values(level: str) -> None:
    assert turn_directive_error({"reasoning": level}) == ""


@pytest.mark.parametrize(
    "body",
    [
        {"turn_mode": "turbo"},
        {"turn_mode": 3},
        {"reasoning": "max"},
        {"reasoning": ["high"]},
    ],
)
def test_turn_directive_validation_rejects_garbage(body: dict) -> None:
    assert turn_directive_error(body)


def test_turn_endpoint_accepts_and_forwards_directives(tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake_turn(**kwargs):
        calls.append(kwargs)
        return LoopTurnResult(
            reply="ok", session="ses_w10", user_id="u", alias="",
            returncode=0, stderr="",
        )

    srv = _Server(_cfg(tmp_path), on_turn=fake_turn)
    try:
        status, data = srv.json(
            "POST",
            "/v1/turn",
            {
                "text": "hi",
                "turn_mode": "fast",
                "reasoning": "high",
            },
            headers=_auth(),
        )
        assert status == 200, data
        assert data["reply"] == "ok"
        assert calls[0]["turn_mode"] == "fast"
        assert calls[0]["reasoning"] == "high"
    finally:
        srv.close()


def test_turn_and_chat_stream_reject_invalid_directives(tmp_path: Path) -> None:
    srv = _Server(_cfg(tmp_path))
    try:
        for path in ("/v1/turn", "/v1/chat/stream"):
            for body in (
                {"text": "hi", "turn_mode": "turbo"},
                {"text": "hi", "reasoning": "max"},
            ):
                status, raw, _ = srv.request("POST", path, body, headers=_auth())
                assert status == 400, (path, body, raw)
    finally:
        srv.close()


def test_runner_loop_gets_flags_and_harness_env(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    params = TurnParams(
        message="hi", provider="echo", turn_mode="fast", reasoning="high"
    )
    argv = build_loop_argv(params, cfg)
    assert "--turn-mode" in argv
    assert argv[argv.index("--turn-mode") + 1] == "fast"
    assert "--reasoning" in argv
    assert argv[argv.index("--reasoning") + 1] == "high"

    _prepared, _notes, extra = prepare_loop(params, cfg)
    assert extra["HARNESS_TURN_MODE"] == "fast"
    assert extra["HARNESS_REASONING"] == "high"


def test_run_loop_turn_passes_runner_flags_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = dict(kwargs.get("env") or {})
        trailer = json.dumps({"session": "ses_loop", "user_id": "u", "alias": ""})
        return SimpleNamespace(
            stdout=f"ok\n{LOOP_TRAILER_MARKER}\n{trailer}\n",
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(loop_client.subprocess, "run", fake_run)
    result = loop_client.run_loop_turn(
        channel="http",
        user="u",
        message="hi",
        config=_cfg(tmp_path),
        provider="echo",
        turn_mode="deep",
        reasoning="medium",
    )
    assert result.reply == "ok"
    argv = captured["argv"]
    assert argv[argv.index("--turn-mode") + 1] == "deep"
    assert argv[argv.index("--reasoning") + 1] == "medium"
    assert captured["env"]["HARNESS_TURN_MODE"] == "deep"
    assert captured["env"]["HARNESS_REASONING"] == "medium"


def test_custom_non_loop_cmd_gets_env_fallback_but_no_new_flags(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, loop_cmd=(sys.executable, "-c", "print('custom')"))
    params = TurnParams(
        message="hi", provider="echo", turn_mode="agent", reasoning="low"
    )
    argv = build_loop_argv(params, cfg)
    assert "--turn-mode" not in argv
    assert "--reasoning" not in argv
    _prepared, _notes, extra = prepare_loop(params, cfg)
    assert extra["HARNESS_TURN_MODE"] == "agent"
    assert extra["HARNESS_REASONING"] == "low"


def test_acp_run_argv_stays_free_of_loop_only_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTS_ACP_CMD", "devin acp")
    cfg = _cfg(tmp_path)
    params = TurnParams(
        message="hi", provider="acp.devin", turn_mode="deep", reasoning="high"
    )
    argv = build_loop_argv(params, cfg)
    assert argv[:4] == [sys.executable, "-m", "agents_relay", "acp-run"]
    assert "--turn-mode" not in argv
    assert "--reasoning" not in argv
    _prepared, _notes, extra = prepare_loop(params, cfg)
    assert extra["HARNESS_TURN_MODE"] == "deep"
    assert extra["HARNESS_REASONING"] == "high"


def test_chat_stream_accepts_directives_and_forwards_tier_sse(tmp_path: Path) -> None:
    seen: dict = {}

    def fake_stream(params, config, *, cancel=None, on_pid=None):
        seen["turn_mode"] = params.turn_mode
        seen["reasoning"] = params.reasoning
        yield StreamEvent(
            type="tier",
            data={"type": "tier", "tier": "deep", "mode": "deep"},
        )
        yield StreamEvent(type="trailer", session="ses_tier", returncode=0)

    srv = _Server(_cfg(tmp_path))
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("agents_relay.web.iter_loop_stream_live", fake_stream)
            status, raw, _ = srv.request(
                "POST",
                "/v1/chat/stream",
                {
                    "text": "hi",
                    "run": "r-w10-tier",
                    "turn_mode": "deep",
                    "reasoning": "medium",
                },
                headers=_auth(),
            )
        assert status == 200, raw
        frames = [
            json.loads(line[6:])
            for line in raw.decode().splitlines()
            if line.startswith("data: ")
        ]
        tier = next(frame for frame in frames if frame["type"] == "tier")
        assert tier["tier"] == "deep"
        assert tier["mode"] == "deep"
        assert seen == {"turn_mode": "deep", "reasoning": "medium"}
    finally:
        srv.close()


def test_harness_tier_sideband_becomes_structured_stream_event(tmp_path: Path) -> None:
    script = tmp_path / "fake_loop.py"
    script.write_text(
        "import json, sys\n"
        "evt = {'type': 'tier', 'tier': 'fast', 'mode': 'fast'}\n"
        "sys.stderr.write('__KLANKER_EVT__ ' + json.dumps(evt) + '\\n')\n"
        "sys.stderr.flush()\n"
        "meta = {'session': 'ses_sideband', 'user_id': 'u', 'alias': ''}\n"
        "sys.stdout.write('answer\\n---agents-loop-trailer---\\n' + json.dumps(meta) + '\\n')\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    cfg = _cfg(tmp_path, loop_cmd=(sys.executable, str(script)))
    events = list(iter_loop_stream_live(TurnParams(message="hi"), cfg, timeout_sec=10))
    tier = next(event for event in events if event.type == "tier")
    assert tier.data["tier"] == "fast"
    assert tier.data["mode"] == "fast"
    assert any(event.type == "trailer" for event in events)


# ---------------------------------------------------------------------------
# read cache primitive + hot endpoint behavior


def test_read_cache_hit_refresh_invalidate_and_host_dimension(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    first_host = host_dir_key()
    key = cache_key("unit", first_host, "caller")

    def load() -> dict:
        calls.append("load")
        return {"n": len(calls)}

    assert relay_read_cache.get_or_load(key, 30.0, load)["n"] == 1
    assert relay_read_cache.get_or_load(key, 30.0, load)["n"] == 1
    assert len(calls) == 1
    assert relay_read_cache.get_or_load(key, 30.0, load, refresh=True)["n"] == 2
    assert relay_read_cache.invalidate("unit") == 1
    assert relay_read_cache.get_or_load(key, 30.0, load)["n"] == 3

    other_host = tmp_path / "other-host"
    (other_host / "workspaces").mkdir(parents=True)
    monkeypatch.setenv("AGENTS_HOST_DIR", str(other_host))
    assert host_dir_key() != first_host


def test_providers_list_cache_refresh_workspace_key_and_mutation_invalidation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[bool] = []

    def fake_rows(config=None, refresh: bool = False):
        calls.append(refresh)
        return [{
            "name": "echo",
            "kind": "echo",
            "source": "env",
            "key_configured": True,
            "configurable": False,
        }]

    monkeypatch.setattr(providers_api, "provider_rows", fake_rows)
    srv = _Server(_cfg(tmp_path))
    try:
        status, data = srv.json("GET", "/v1/providers", headers=_auth())
        assert status == 200, data
        assert len(calls) == 1
        srv.json("GET", "/v1/providers", headers=_auth())
        assert len(calls) == 1

        srv.json("GET", "/v1/providers?workspace=alpha", headers=_auth())
        assert len(calls) == 2
        srv.json("GET", "/v1/providers?workspace=alpha", headers=_auth())
        assert len(calls) == 2

        srv.json("GET", "/v1/providers?refresh=1", headers=_auth())
        assert len(calls) == 3
        assert calls[-1] is True

        status, data = srv.json(
            "POST",
            "/v1/providers",
            {"name": "cache.local", "url": "http://127.0.0.1:9/v1"},
            headers=_auth(),
        )
        assert status == 200, data
        after_mutation = len(calls)
        srv.json("GET", "/v1/providers", headers=_auth())
        assert len(calls) == after_mutation + 1
        srv.json("GET", "/v1/providers", headers=_auth())
        assert len(calls) == after_mutation + 1

        status, data = srv.json(
            "POST",
            "/v1/providers/secret",
            {"key_env": "LLM_API_KEY", "value": "test-secret"},
            headers=_auth(),
        )
        assert status == 200, data
        after_secret = len(calls)
        srv.json("GET", "/v1/providers", headers=_auth())
        assert len(calls) == after_secret + 1

        status, data = srv.json("GET", "/v1/providers/echo/probe", headers=_auth())
        assert status == 200, data
        after_probe = len(calls)
        srv.json("GET", "/v1/providers", headers=_auth())
        assert len(calls) == after_probe + 1

        status, data = srv.json("DELETE", "/v1/providers/cache.local", headers=_auth())
        assert status == 200, data
        after_delete = len(calls)
        srv.json("GET", "/v1/providers", headers=_auth())
        assert len(calls) == after_delete + 1
        srv.json("GET", "/v1/providers", headers=_auth())
        assert len(calls) == after_delete + 1
    finally:
        srv.close()


def test_agents_executable_detection_cache_refresh_and_provider_invalidation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_resolve(binary: str):
        calls.append(binary)
        return str(tmp_path / "bin" / binary) if binary == "devin" else None

    monkeypatch.setattr(acp, "resolve_agent_exe", fake_resolve)
    srv = _Server(_cfg(tmp_path))
    try:
        status, data = srv.json("GET", "/v1/providers/agents", headers=_auth())
        assert status == 200, data
        assert len(calls) == 3
        assert data["agents"][0]["installed"] is True
        srv.json("GET", "/v1/providers/agents", headers=_auth())
        assert len(calls) == 3

        srv.json("GET", "/v1/providers/agents?refresh=1", headers=_auth())
        assert len(calls) == 6

        status, data = srv.json(
            "POST",
            "/v1/providers",
            {"name": "agentcache.local", "url": "http://127.0.0.1:9/v1"},
            headers=_auth(),
        )
        assert status == 200, data
        srv.json("GET", "/v1/providers/agents", headers=_auth())
        assert len(calls) == 9
        srv.json("GET", "/v1/providers/agents", headers=_auth())
        assert len(calls) == 9
    finally:
        srv.close()


def test_workspaces_cache_refresh_and_mutation_invalidation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real_row = workspaces_api.workspace_row
    calls: list[str] = []

    def counting_row(ws):
        calls.append(str(ws.id))
        return real_row(ws)

    monkeypatch.setattr(workspaces_api, "workspace_row", counting_row)
    Host.from_env().create("alpha")
    srv = _Server(_cfg(tmp_path))
    try:
        status, data = srv.json("GET", "/v1/workspaces", headers=_auth())
        assert status == 200, data
        assert len(calls) > 0
        first_count = len(calls)
        srv.json("GET", "/v1/workspaces", headers=_auth())
        assert len(calls) == first_count

        srv.json("GET", "/v1/workspaces?refresh=1", headers=_auth())
        assert len(calls) > first_count

        status, data = srv.json(
            "POST", "/v1/workspaces", {"id": "beta", "label": "Beta"}, headers=_auth()
        )
        assert status == 201, data
        after_create = len(calls)
        srv.json("GET", "/v1/workspaces", headers=_auth())
        assert len(calls) > after_create
        srv.json("GET", "/v1/workspaces", headers=_auth())
        after_repeat = len(calls)

        status, data = srv.json(
            "PATCH", "/v1/workspaces/beta", {"label": "Beta 2"}, headers=_auth()
        )
        assert status == 200, data
        after_patch = len(calls)
        assert after_patch >= after_repeat
        srv.json("GET", "/v1/workspaces", headers=_auth())
        assert len(calls) > after_patch

        status, data = srv.json(
            "POST",
            "/v1/workspaces/beta/personas",
            {"id": "worker", "name": "Worker"},
            headers=_auth(),
        )
        assert status == 201, data
        after_persona = len(calls)
        srv.json("GET", "/v1/workspaces", headers=_auth())
        assert len(calls) > after_persona

        status, data = srv.json("DELETE", "/v1/workspaces/beta", headers=_auth())
        assert status == 200, data
        after_delete = len(calls)
        srv.json("GET", "/v1/workspaces", headers=_auth())
        assert len(calls) > after_delete
    finally:
        srv.close()


def test_workspaces_cache_keys_include_host_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    Host.from_env().create("iso-one")
    srv = _Server(_cfg(tmp_path))
    try:
        status, data = srv.json("GET", "/v1/workspaces", headers=_auth())
        assert status == 200, data
        assert "iso-one" in {row.get("id") for row in data["workspaces"]}

        other_host = tmp_path / "other-host"
        (other_host / "workspaces").mkdir(parents=True)
        monkeypatch.setenv("AGENTS_HOST_DIR", str(other_host))
        status, data = srv.json("GET", "/v1/workspaces", headers=_auth())
        assert status == 200, data
        assert "iso-one" not in {row.get("id") for row in data["workspaces"]}
    finally:
        srv.close()


def test_workspaces_cache_caller_scope_and_ws_member_acl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real_row = workspaces_api.workspace_row
    calls: list[str] = []

    def counting_row(ws):
        calls.append(str(ws.id))
        return real_row(ws)

    monkeypatch.setattr(workspaces_api, "workspace_row", counting_row)
    host_device = devices.mint_device(device_id="dev-cache-host", label="host", role="host")
    member = devices.mint_device(
        device_id="dev-cache-member",
        label="member",
        role="ws_member",
        allowlist=["default"],
    )
    srv = _Server(_cfg(tmp_path))
    try:
        status, data = srv.json("GET", "/v1/workspaces", headers=_auth())
        assert status == 200, data
        admin_count = len(calls)
        assert admin_count > 0

        host_headers = {
            "Authorization": f"Bearer {host_device['device_token']}",
            "X-Device-Id": host_device["device_id"],
        }
        status, data = srv.json("GET", "/v1/workspaces", headers=host_headers)
        assert status == 200, data
        assert len(calls) > admin_count

        member_headers = {
            "Authorization": f"Bearer {member['device_token']}",
            "X-Device-Id": member["device_id"],
        }
        status, raw, _ = srv.request("GET", "/v1/workspaces", headers=member_headers)
        assert status == 403
        assert b'"workspaces"' not in raw
    finally:
        srv.close()


def test_ws_announce_and_revoke_invalidate_workspace_cache(tmp_path: Path) -> None:
    channel.reset_hub()
    calls: list[str] = []
    key = cache_key("workspaces", host_dir_key(), "admin")

    def load() -> dict:
        calls.append("load")
        return {"n": len(calls)}

    try:
        hub = channel.hub()
        hub.set_ws_handler(lambda device_id, kind, payload: True)
        assert relay_read_cache.get_or_load(key, 5.0, load)["n"] == 1
        assert relay_read_cache.get_or_load(key, 5.0, load)["n"] == 1

        hub.handle_upstream(
            "dev-x",
            [{"kind": "ws:announce", "payload": {"ws": "w1"}}],
        )
        assert relay_read_cache.get_or_load(key, 5.0, load)["n"] == 2

        hub.handle_upstream(
            "dev-x",
            [{"kind": "ws:revoke", "payload": {"ws": "w1"}}],
        )
        assert relay_read_cache.get_or_load(key, 5.0, load)["n"] == 3
    finally:
        channel.reset_hub()
