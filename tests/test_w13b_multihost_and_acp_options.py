"""W13B multihost identity/peers and ACP config-option regression tests."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

import agents_relay.acp as acp
import agents_relay.channel_client as channel_client
import agents_relay.http_adapter as ha
from agents_relay import channel, devices, pairing
from agents_relay.__main__ import build_parser
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.loop_client import LoopTurnResult
from agents_relay.read_cache import relay_read_cache
from agents_relay.turn import StreamEvent, TurnParams, build_loop_argv, prepare_loop
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
        web_token="",
        web_user="",
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

    def request(self, method: str, path: str, body=None, headers: dict | None = None):
        payload = json.dumps(body) if body is not None else None
        hdrs = {"Content-Type": "application/json"} if payload else {}
        hdrs.update(headers or {})
        conn = HTTPConnection(self.host, self.port, timeout=10)
        conn.request(method, path, body=payload, headers=hdrs)
        res = conn.getresponse()
        raw = res.read()
        status = res.status
        conn.close()
        return status, raw

    def json(self, method: str, path: str, body=None, headers: dict | None = None):
        status, raw = self.request(method, path, body, headers)
        try:
            return status, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return status, {"error": raw.decode("utf-8", "replace")}


def _auth() -> dict[str, str]:
    return {"X-Relay-Secret": "expected"}


@pytest.fixture(autouse=True)
def _clean_w13b_state():
    ha.ws_invite_proxy_fn = None
    ha.ws_invite_auth = None
    ha.ws_grant_hook = None
    ha.spoke_hosts_fn = None
    pairing.clear_pairs()
    channel.reset_hub()
    relay_read_cache.clear()
    yield
    ha.ws_invite_proxy_fn = None
    ha.ws_invite_auth = None
    ha.ws_grant_hook = None
    ha.spoke_hosts_fn = None
    pairing.clear_pairs()
    channel.reset_hub()
    relay_read_cache.clear()


def _ws(ws_id: str = "w1", **kwargs):
    host = Host.from_env()
    if not host.workspace_exists(ws_id):
        host.create(ws_id)
    if kwargs:
        host.update_workspace(ws_id, **kwargs)
    return host.workspace(ws_id)


# ---------------------------------------------------------------------------
# Part 1 — bidirectional pairing identity


def test_pair_redeem_and_connect_hub_use_real_spoke_and_hub_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host.from_env()
    host.set_host_label("hub-vps")
    hub_host_id = host.host_id()
    srv = _Server(_cfg(tmp_path))
    try:
        st, pair = srv.json("POST", "/v1/pair/create", {"mode": "remote"}, headers=_auth())
        assert st == 200, pair
        monkeypatch.setattr(
            channel_client,
            "_local_host_identity",
            lambda: ("host_spoke", "FelixPC"),
        )

        row = channel_client.connect_hub(
            f"http://{srv.host}:{srv.port}", pair["token"])

        assert row["hub_id"] == hub_host_id
        assert row["hub_label"] == "hub-vps"
        assert row["label"] == "hub-vps"
        assert row["label"] != "FelixPC"
        stored = channel_client._load_hubs()[hub_host_id]
        assert stored["label"] == "hub-vps"
        meta = devices.load_devices()[row["device_id"]]
        assert meta["label"] == "FelixPC"
        assert meta["host_id"] == "host_spoke"
        assert meta["host_id"] != hub_host_id

        # Direct redeem exposes the hub label explicitly; omitting
        # client_host_id keeps the legacy entry-host fallback.
        st, pair2 = srv.json("POST", "/v1/pair/create", {"mode": "remote"}, headers=_auth())
        assert st == 200, pair2
        st, res = srv.json(
            "POST",
            "/v1/pair/redeem",
            {"token": pair2["token"], "label": "LegacyPC"},
        )
        assert st == 200, res
        assert res["host_id"] == hub_host_id
        assert res["host_label"] == "hub-vps"
        legacy = devices.load_devices()[res["device_id"]]
        assert legacy["label"] == "LegacyPC"
        assert legacy["host_id"] == hub_host_id
    finally:
        srv.close()


def test_connect_hub_passes_spoke_identity_and_prefers_hub_response_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_redeem(hub_url, credential, *, label="", bearer="", client_host_id=""):
        captured.update(label=label, bearer=bearer, client_host_id=client_host_id)
        return {
            "ok": True,
            "mode": "remote",
            "host_id": "hub-host",
            "host_label": "klanker-vps",
            "device_id": "dev-spoke",
            "token": "",
        }

    monkeypatch.setattr(channel_client, "redeem_device", fake_redeem)
    monkeypatch.setattr(
        channel_client,
        "_local_host_identity",
        lambda: ("host_spoke", "FelixPC"),
    )
    row = channel_client.connect_hub("http://hub.example:8787", "CODE")

    assert captured["label"] == "FelixPC"
    assert captured["client_host_id"] == "host_spoke"
    assert row["label"] == "klanker-vps"
    assert row["hub_label"] == "klanker-vps"
    assert channel_client._load_hubs()["hub-host"]["label"] == "klanker-vps"


# ---------------------------------------------------------------------------
# Part 2 — hub peers + accurate spoke host maps


def test_hub_peers_admin_and_host_device_but_never_ws_member(tmp_path: Path) -> None:
    host = Host.from_env()
    host.set_host_label("hub-vps")
    hub_host_id = host.host_id()
    host_dev = devices.mint_device(
        device_id="dev-host", label="FelixPC", host_id="host_spoke", role="host")
    devices.mint_device(device_id="dev-client", label="client", role="client", allowlist=[])
    member = devices.mint_device(
        device_id="dev-member", label="guest", role="ws_member", allowlist=["w1"])
    link, _, _ = channel.hub().connect("dev-host")
    srv = _Server(_cfg(tmp_path))
    try:
        st, res = srv.json("GET", "/v1/hub/peers", headers=_auth())
        assert st == 200, res
        assert res["hub"] == {
            "device_id": hub_host_id,
            "host_id": hub_host_id,
            "label": "hub-vps",
            "role": "hub",
            "online": True,
            "via": "hub",
        }
        assert res["peers"][0] == res["hub"]
        rows = {row["device_id"]: row for row in res["peers"]}
        assert rows["dev-host"]["host_id"] == "host_spoke"
        assert rows["dev-host"]["label"] == "FelixPC"
        assert rows["dev-host"]["role"] == "host"
        assert rows["dev-host"]["online"] is True
        assert rows["dev-host"]["via"] == "channel"
        assert "dev-client" not in rows
        assert "dev-member" not in rows
        encoded = json.dumps(res)
        assert host_dev["device_token"] not in encoded
        assert member["device_token"] not in encoded

        st, host_res = srv.json(
            "GET", "/v1/hub/peers",
            headers={"Authorization": f"Bearer {host_dev['device_token']}"})
        assert st == 200, host_res
        assert host_res["peers"][0]["role"] == "hub"

        st, _ = srv.json(
            "GET", "/v1/hub/peers",
            headers={"Authorization": f"Bearer {member['device_token']}"})
        assert st == 403
    finally:
        channel.hub().disconnect("dev-host", generation=link.generation)
        srv.close()


def test_spoke_hosts_map_honors_hub_offline_and_dedupes_self() -> None:
    ws = _ws("w1", sync_policy="synced", sync_hosts=["dev-spoke"])
    local_host_id = Host.from_env().host_id()
    hub_map = {
        "hub_offline": True,
        "self_device_id": "dev-spoke",
        "hosts": [
            {"device_id": "dev-spoke", "label": "FelixPC", "role": "host",
             "self": False, "online": False, "via": "channel",
             "last_seen": 1, "status": "offline", "leases": []},
            {"device_id": "dev-spoke", "label": "duplicate", "role": "host",
             "self": False, "online": False, "via": "channel",
             "last_seen": 1, "status": "offline", "leases": []},
            {"device_id": "dev-other", "label": "other", "role": "member",
             "self": False, "online": True, "via": "channel",
             "last_seen": 1, "status": "online", "leases": []},
        ],
    }
    out = ha._ws_hosts_payload(ws, {"device_id": ""}, spoke_hosts_fn=lambda _ws: dict(hub_map))

    assert out["hub_offline"] is True
    ids = [row["device_id"] for row in out["hosts"]]
    assert ids.count("dev-spoke") == 1
    assert local_host_id not in ids
    spoke = next(row for row in out["hosts"] if row["device_id"] == "dev-spoke")
    assert spoke["self"] is True
    assert spoke["online"] is True
    assert any(row["device_id"] == "dev-other" for row in out["hosts"])


def test_local_workspace_with_stored_hub_link_is_not_hub_offline() -> None:
    ws = _ws("localws")
    channel_client._store_hubs({
        "hubx": {"hub_url": "http://hub:8787", "device_id": "dev-me",
                 "label": "hub", "connected_at": 1, "last_seq": 0}
    })
    out = ha._ws_hosts_payload(ws, {"device_id": ""}, spoke_hosts_fn=None)
    assert out["hub_offline"] is False


# ---------------------------------------------------------------------------
# Part 3 — spoke guest-invite proxy hook


def test_ws_invite_proxy_success_error_and_none_fallback(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []

    def proxy(ws_id: str, ttl_sec: int):
        calls.append((ws_id, ttl_sec))
        return {"ok": True, "code": "ABCD-EFGH", "ws": ws_id, "proxied": True}

    ha.ws_invite_proxy_fn = proxy
    srv = _Server(_cfg(tmp_path))
    try:
        st, res = srv.json(
            "POST", "/v1/ws/invite", {"ws": "w1", "ttl_sec": 90}, headers=_auth())
        assert st == 200, res
        assert res["proxied"] is True
        assert calls == [("w1", 90)]

        ha.ws_invite_proxy_fn = lambda _ws, _ttl: {
            "ok": False, "error": "hub unavailable", "status": 409}
        st, res = srv.json("POST", "/v1/ws/invite", {"ws": "w1"}, headers=_auth())
        assert st == 409
        assert res["error"] == "hub unavailable"

        # None means normal local minting behavior; without local ws hooks
        # that is the pre-existing 503, proving control fell through.
        ha.ws_invite_proxy_fn = lambda _ws, _ttl: None
        st, res = srv.json("POST", "/v1/ws/invite", {"ws": "w1"}, headers=_auth())
        assert st == 503
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# Part 4 — ACP thought_level/speed/mode end to end

_CONFIG_AGENT = r'''
import json, os, sys
OPTS = [
  {"id":"mode","name":"Mode","category":"agent","currentValue":"smart",
   "options":[{"value":"smart"},{"value":"plan"},{"value":"bypass"}]},
  {"id":"model","name":"Model","category":"model","currentValue":"swe-2",
   "options":[{"value":"swe-2"},{"value":"swe-2-max"}]},
  {"id":"thought_level","name":"Thought","category":"agent","currentValue":"high",
   "options":[{"value":"medium"},{"value":"high"},{"value":"max"}]},
  {"id":"speed","name":"Speed","category":"agent","currentValue":"ultrafast",
   "options":[{"value":"standard"},{"value":"ultrafast"}]},
]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":1}}), flush=True)
    elif method == "session/new":
        print(json.dumps({"jsonrpc":"2.0","id":mid,"result":{"sessionId":"acp_cfg","configOptions":OPTS}}), flush=True)
    elif method == "session/load":
        print(json.dumps({"jsonrpc":"2.0","id":mid,"result":{}}), flush=True)
    elif method == "session/set_config_option":
        log = os.environ.get("AGENT_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(msg.get("params") or {}) + "\n")
        print(json.dumps({"jsonrpc":"2.0","id":mid,"result":{}}), flush=True)
    elif method == "session/prompt":
        print(json.dumps({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"end_turn"}}), flush=True)
'''


def _write_config_agent(tmp_path: Path) -> Path:
    path = tmp_path / "config_agent.py"
    path.write_text(_CONFIG_AGENT, encoding="utf-8")
    return path


def test_probe_acp_agent_preserves_all_config_options(tmp_path: Path) -> None:
    agent = _write_config_agent(tmp_path)
    out = acp.probe_acp_agent([sys.executable, str(agent)], timeout=8)

    assert out["mode"] == "smart"
    assert out["modes"] == ["smart", "plan", "bypass"]
    assert out["model"] == "swe-2"
    assert out["models"] == ["swe-2", "swe-2-max"]
    assert out["thought_level"] == "high"
    assert out["thought_levels"] == ["medium", "high", "max"]
    assert out["speed"] == "ultrafast"
    assert out["speeds"] == ["standard", "ultrafast"]
    assert [row["id"] for row in out["config_options"]] == [
        "mode", "model", "thought_level", "speed"]
    thought = next(row for row in out["config_options"] if row["id"] == "thought_level")
    assert thought == {
        "id": "thought_level",
        "name": "Thought",
        "category": "agent",
        "current": "high",
        "options": ["medium", "high", "max"],
    }


def test_run_acp_turn_sets_mode_thought_level_speed_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _write_config_agent(tmp_path)
    log_path = tmp_path / "agent.log"
    monkeypatch.setenv("AGENT_LOG", str(log_path))
    with contextlib.redirect_stdout(io.StringIO()):
        rc = acp.run_acp_turn(
            agent_argv=[sys.executable, str(agent)],
            message="ping",
            session="ses_w13b_cfg",
            workdir=str(tmp_path),
            config=_cfg(tmp_path),
            model="swe-2-max",
            acp_mode="plan",
            thought_level="max",
            speed="standard",
        )
    assert rc == 0
    calls = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [(row["configId"], row["value"]) for row in calls] == [
        ("model", "swe-2-max"),
        ("mode", "plan"),
        ("thought_level", "max"),
        ("speed", "standard"),
    ]
    assert all(row["sessionId"] == "acp_cfg" for row in calls)


def _write_acp_provider() -> None:
    providers = Host.from_env().dir / "providers"
    providers.mkdir(parents=True, exist_ok=True)
    (providers / "acp.devin.json").write_text(json.dumps({
        "name": "acp.devin",
        "kind": "acp-agent",
        "when": "on_request",
        "cadence": "on_request",
        "verb": "chat.completions",
        "rests_on": "test provider",
        "expected_exit": 0,
        "command": "devin acp",
    }), encoding="utf-8")


def test_providers_row_and_cache_expose_acp_config_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_acp_provider()
    monkeypatch.setattr(acp, "resolve_agent_exe", lambda exe: "/tmp/devin" if exe == "devin" else None)
    probe = {
        "model": "swe-2", "models": ["swe-2"],
        "mode": "smart", "modes": ["smart", "plan"],
        "thought_level": "high", "thought_levels": ["medium", "high", "max"],
        "speed": "ultrafast", "speeds": ["standard", "ultrafast"],
        "config_options": [{"id": "speed", "name": "Speed", "category": "agent",
                            "current": "ultrafast", "options": ["standard", "ultrafast"]}],
        "ts": time.time(),
    }
    cache = acp.acp_models_cache_path("acp.devin", _cfg(tmp_path))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(probe), encoding="utf-8")

    cached = acp._read_acp_models_cache(cache)
    assert cached["thought_levels"] == ["medium", "high", "max"]
    assert cached["speeds"] == ["standard", "ultrafast"]
    assert cached["config_options"][0]["id"] == "speed"

    relay_read_cache.invalidate("providers")
    srv = _Server(_cfg(tmp_path))
    try:
        st, res = srv.json("GET", "/v1/providers", headers=_auth())
        assert st == 200, res
        row = next(row for row in res["providers"] if row["name"] == "acp.devin")
        assert row["thought_level"] == "high"
        assert row["thought_levels"] == ["medium", "high", "max"]
        assert row["speed"] == "ultrafast"
        assert row["speeds"] == ["standard", "ultrafast"]
        assert row["config_options"][0]["id"] == "speed"
    finally:
        srv.close()


def test_turn_and_chat_stream_accept_forward_and_validate_acp_options(tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake_turn(**kwargs):
        calls.append(kwargs)
        return LoopTurnResult(
            reply="ok", session="ses_w13b", user_id="u", alias="",
            returncode=0, stderr="")

    srv = _Server(_cfg(tmp_path), on_turn=fake_turn)
    try:
        st, res = srv.json(
            "POST",
            "/v1/turn",
            {"text": "hi", "acp_mode": "smart", "thought_level": "high", "speed": "ultrafast"},
            headers=_auth(),
        )
        assert st == 200, res
        assert calls[0]["acp_mode"] == "smart"
        assert calls[0]["thought_level"] == "high"
        assert calls[0]["speed"] == "ultrafast"

        for path in ("/v1/turn", "/v1/chat/stream"):
            for key in ("acp_mode", "thought_level", "speed"):
                st, raw = srv.request(
                    "POST", path, {"text": "hi", key: "bad value!"}, headers=_auth())
                assert st == 400, (path, key, raw)

        seen: dict = {}

        def fake_stream(params, config, *, cancel=None, on_pid=None):
            seen.update(
                acp_mode=params.acp_mode,
                thought_level=params.thought_level,
                speed=params.speed,
            )
            yield StreamEvent(type="trailer", session="ses_stream", returncode=0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("agents_relay.web.iter_loop_stream_live", fake_stream)
            st, raw = srv.request(
                "POST",
                "/v1/chat/stream",
                {"text": "hi", "run": "r-w13b", "acp_mode": "plan",
                 "thought_level": "max", "speed": "standard"},
                headers=_auth(),
            )
        assert st == 200, raw
        assert seen == {"acp_mode": "plan", "thought_level": "max", "speed": "standard"}
    finally:
        srv.close()


def test_acp_run_argv_env_flags_and_parser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTS_ACP_CMD", "devin acp")
    params = TurnParams(
        message="hi",
        provider="acp.devin",
        acp_mode="smart",
        thought_level="high",
        speed="ultrafast",
    )
    argv = build_loop_argv(params, _cfg(tmp_path))
    assert argv[argv.index("--acp-mode") + 1] == "smart"
    assert argv[argv.index("--thought-level") + 1] == "high"
    assert argv[argv.index("--speed") + 1] == "ultrafast"
    assert "--turn-mode" not in argv
    assert "--reasoning" not in argv
    _prepared, _notes, extra = prepare_loop(params, _cfg(tmp_path))
    assert extra["AGENTS_ACP_MODE"] == "smart"
    assert extra["AGENTS_ACP_THOUGHT_LEVEL"] == "high"
    assert extra["AGENTS_ACP_SPEED"] == "ultrafast"

    parsed = build_parser().parse_args([
        "acp-run", "--message", "hi", "--acp-mode", "plan",
        "--thought-level", "max", "--speed", "standard",
        "--turn-mode", "deep", "--reasoning", "high",
    ])
    assert parsed.acp_mode == "plan"
    assert parsed.thought_level == "max"
    assert parsed.speed == "standard"

    captured: dict = {}
    monkeypatch.setenv("AGENTS_ACP_MODE", "smart")
    monkeypatch.setenv("AGENTS_ACP_THOUGHT_LEVEL", "high")
    monkeypatch.setenv("AGENTS_ACP_SPEED", "ultrafast")
    monkeypatch.setattr(acp, "run_acp_turn", lambda **kwargs: captured.update(kwargs) or 0)
    args = SimpleNamespace(
        provider="acp.devin", message="hi", session="", workdir="",
        deliver="stream", new_session=False, model="",
        acp_mode="", thought_level="", speed="")
    assert acp.acp_loop_main(args, _cfg(tmp_path)) == 0
    assert captured["acp_mode"] == "smart"
    assert captured["thought_level"] == "high"
    assert captured["speed"] == "ultrafast"
