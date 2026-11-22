"""Agent Client Protocol (ACP) — bidirectional stdio adapter.

Two roles, one wire:

- ``acp-serve`` — klanker *as an agent*. A client (Zed, any ACP editor)
  spawns this over stdio; ``session/prompt`` drives ``runner.loop`` and
  streams the reply back as ``agent_message_chunk`` updates. Harness tool
  approvals (``AGENTS_APPROVAL_CMD`` writing pending files under
  ``<host>/run/approvals``) are bridged to
  ``session/request_permission``, so the client's native allow/deny UI
  decides them.

- ``acp-run`` — klanker *as a client*. Presents a ``runner.loop``-shaped
  CLI (``--message``, ``--session``, ``--deliver stream``) while spawning a
  foreign ACP agent command (``devin acp``, ``ssh box devin acp``) per turn.
  Usable as ``LOOP_CMD``: the relay fronts it unchanged.

Wire: NDJSON JSON-RPC 2.0 — one message per line, no framing headers.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO

from .approvals import decide_approval
from .config import RelayConfig
from .state import approval_dir, read_json, write_json_atomic
from .turn import TurnParams, iter_loop_stream_live

PROTOCOL_VERSION = 1

ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603


# ---------------------------------------------------------------------------
# wire


def _read_messages(stream: TextIO) -> Iterator[dict[str, Any]]:
    """Yield JSON-RPC messages; one NDJSON line each. Loud on garbage."""
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            yield {"__parse_error__": line}
            continue
        if isinstance(msg, dict):
            yield msg


class _Writer:
    """Serialized NDJSON writer — notifications and responses interleave."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=False)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()

    def respond(self, req_id: Any, result: Any) -> None:
        self.send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def respond_error(self, req_id: Any, code: int, message: str) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": message},
            }
        )

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})


# ---------------------------------------------------------------------------
# acp-serve — klanker as the agent


def _session_update(
    writer: _Writer, session_id: str, update: dict[str, Any]
) -> None:
    writer.notify(
        "session/update",
        {"sessionId": session_id, "update": update},
    )


def _chunk(writer: _Writer, session_id: str, text: str, *, thought: bool = False) -> None:
    kind = "agent_thought_chunk" if thought else "agent_message_chunk"
    _session_update(
        writer,
        session_id,
        {
            "sessionUpdate": kind,
            "content": {"type": "text", "text": text},
        },
    )


def _prompt_text(prompt: Any) -> str:
    """Flatten ACP content blocks to text for the loop's --message."""
    if isinstance(prompt, str):
        return prompt
    parts: list[str] = []
    for block in prompt or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "resource_link":
            parts.append(str(block.get("uri") or ""))
    return "\n".join(p for p in parts if p)


class _AcpSession:
    def __init__(self, session_id: str, cwd: str) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.cancel = threading.Event()
        self.busy = False
        self.provider = ""
        self.model = ""


_STATUS_NOISE = re.compile(
    r"Processing request of type|server\.py:\d+|ListToolsRequest"
    r"|^\[\d{2}/\d{2}/\d{2} [^\]]*\] INFO"
)


def _config_options(session: _AcpSession) -> list[dict[str, Any]]:
    """ACP configOptions for session/new — provider + model selects."""
    options: list[dict[str, Any]] = []
    try:
        from runner.providers import find_provider, list_providers
    except ImportError:
        return options
    providers = [
        str(r.get("name") or "") for r in list_providers() if r.get("name")
    ]
    current_provider = (
        session.provider
        or os.environ.get("LOOP_PROVIDER", "").strip()
        or "openai.default"
    )
    if providers:
        options.append(
            {
                "id": "provider",
                "name": "Provider",
                "type": "select",
                "options": [{"value": p, "name": p} for p in providers],
                "currentValue": current_provider
                if current_provider in providers
                else providers[0],
            }
        )
    manifest = find_provider(current_provider) or {}
    models = [str(m) for m in manifest.get("models") or [] if m]
    current_model = (
        session.model
        or os.environ.get(manifest.get("model_env") or "LLM_MODEL", "")
        or str(manifest.get("model") or "")
    )
    if models:
        options.append(
            {
                "id": "model",
                "name": "Model",
                "type": "select",
                "options": [{"value": m, "name": m} for m in models],
                "currentValue": current_model
                if current_model in models
                else models[0],
            }
        )
    return options


def _bridge_pending_approvals(
    config: RelayConfig,
    writer: _Writer,
    session: _AcpSession,
    seen: set[str],
    pending_rpc: dict[Any, str],
    next_id: itertools.count,
    stop: threading.Event,
) -> None:
    """Watch the relay approval dir; forward new pending files to the client
    as session/request_permission. The loop's AGENTS_APPROVAL_CMD child is
    blocked polling the same file — our record_decision unblocks it."""
    try:
        adir = approval_dir(config)
    except Exception:
        return
    while not stop.is_set():
        try:
            files = sorted(adir.glob("*.json")) if adir.is_dir() else []
        except Exception:
            files = []
        for path in files:
            ident = path.stem
            if ident in seen:
                continue
            rec = read_json(path) or {}
            if rec.get("status") != "pending":
                continue
            seen.add(ident)
            request = rec.get("request") if isinstance(rec.get("request"), dict) else rec
            title = str(request.get("title") or request.get("command") or "Tool approval")
            rpc_id = f"perm-{next(next_id)}"
            pending_rpc[rpc_id] = ident
            writer.send(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": session.session_id,
                        "toolCall": {
                            "toolCallId": ident,
                            "title": title,
                            "rawInput": request,
                        },
                        "options": [
                            {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                        ],
                    },
                }
            )
        stop.wait(0.4)


def _provider_problem(provider: str = "") -> str:
    """Empty when the selected provider can resolve url+model+key.

    Mirrors OpenAICompatProvider's resolution so a misconfigured turn fails
    loudly here instead of inside the spawned loop process.
    """
    name = (
        provider.strip()
        or os.environ.get("LOOP_PROVIDER", "").strip()
        or "openai.default"
    )
    try:
        from agents_workspace import resolve_secret
        from runner.providers import find_provider
    except ImportError:
        return ""
    manifest = find_provider(name)
    if manifest is None:
        return f"unknown provider {name!r}"
    if manifest.get("kind", "openai_compat") != "openai_compat":
        return ""
    missing = [
        env
        for env, field in (
            (manifest.get("url_env") or "LLM_BASE_URL", "url"),
            (manifest.get("model_env") or "LLM_MODEL", "model"),
        )
        if not (os.environ.get(env) or manifest.get(field))
    ]
    key_env = manifest.get("key_env") or "LLM_API_KEY"
    if not (resolve_secret(key_env) or manifest.get("api_key")):
        missing.append(key_env)
    return ", ".join(missing)


def _run_prompt(
    config: RelayConfig,
    writer: _Writer,
    session: _AcpSession,
    message: str,
) -> str:
    """Drive one loop turn; returns the ACP stopReason."""
    params = TurnParams(
        channel="acp",
        user="acp",
        message=message,
        session=session.session_id,
        workdir=session.cwd,
        provider=session.provider,
        model=session.model,
    )
    stop_reason = "end_turn"
    for event in iter_loop_stream_live(
        params, config, cancel=session.cancel
    ):
        if session.cancel.is_set():
            stop_reason = "cancelled"
            continue
        if event.type == "delta" and event.text:
            _chunk(writer, session.session_id, event.text)
        elif event.type == "status" and event.text:
            if _STATUS_NOISE.search(event.text):
                continue
            _chunk(writer, session.session_id, event.text + "\n", thought=True)
        elif event.type == "error" and event.text:
            _chunk(writer, session.session_id, event.text)
        elif event.type == "trailer":
            if event.returncode != 0:
                _chunk(
                    writer,
                    session.session_id,
                    f"\n[loop exited {event.returncode}]",
                )
    if session.cancel.is_set():
        stop_reason = "cancelled"
    return stop_reason


def serve_acp(config: RelayConfig) -> int:
    """ACP agent server over stdio. One prompt at a time per session."""
    # Quiet FastMCP children; their INFO chatter would flood thought chunks.
    os.environ.setdefault("FASTMCP_LOG_LEVEL", "WARNING")
    reader = _read_messages(sys.stdin)
    writer = _Writer(sys.stdout)
    sessions: dict[str, _AcpSession] = {}
    seen_approvals: set[str] = set()
    pending_rpc: dict[Any, str] = {}  # rpc id -> approval ident
    id_counter = itertools.count(1)
    bridge_stop = threading.Event()
    bridge_session: dict[str, _AcpSession | None] = {"s": None}
    bridge_thread: threading.Thread | None = None

    def _start_bridge(session: _AcpSession) -> None:
        nonlocal bridge_thread
        bridge_session["s"] = session
        if bridge_thread is None or not bridge_thread.is_alive():
            bridge_stop.clear()
            bridge_thread = threading.Thread(
                target=_bridge_pending_approvals,
                args=(
                    config,
                    writer,
                    session,
                    seen_approvals,
                    pending_rpc,
                    id_counter,
                    bridge_stop,
                ),
                daemon=True,
            )
            bridge_thread.start()

    for msg in reader:
        method = msg.get("method")
        req_id = msg.get("id")

        if "__parse_error__" in msg:
            if req_id is not None:
                writer.respond_error(req_id, ERR_PARSE, "invalid JSON")
            continue

        # Responses to requests we sent (permission decisions).
        if method is None and req_id is not None and "result" in msg:
            ident = pending_rpc.pop(req_id, None)
            if ident:
                outcome = (msg.get("result") or {}).get("outcome") or {}
                selected = str(outcome.get("optionId") or "")
                decide_approval(config, ident, allow=selected != "deny")
            continue

        if method == "initialize":
            writer.respond(
                req_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "agentCapabilities": {
                        "loadSession": True,
                        "promptCapabilities": {"image": False, "audio": False},
                    },
                    "agentInfo": {
                        "name": "klanker",
                        "title": "Klanker (agents-harness)",
                        "version": "0.1.0",
                    },
                    "authMethods": [
                        {
                            "id": "llm_api_key",
                            "name": "LLM API Key",
                            "description": "Stored as LLM_API_KEY in the host secrets.json.",
                        }
                    ],
                },
            )
            continue

        if method == "authenticate":
            params = msg.get("params") or {}
            meta = params.get("meta") or {}
            key = str(
                params.get("apiKey") or meta.get("api_key") or params.get("key") or ""
            ).strip()
            if not key:
                writer.respond_error(
                    req_id, ERR_INVALID_PARAMS, "no api key in authenticate params"
                )
                continue
            try:
                from agents_workspace import Host

                secrets_path = Host.from_env().secrets
                data = read_json(secrets_path) or {}
                data["LLM_API_KEY"] = key
                write_json_atomic(secrets_path, data, mode=0o600)
                if os.name != "nt":
                    os.chmod(secrets_path, 0o600)
                writer.respond(req_id, {})
            except Exception as exc:
                writer.respond_error(
                    req_id, ERR_INTERNAL, f"store key failed: {exc}"
                )
            continue

        if method in ("session/new", "session/load"):
            params = msg.get("params") or {}
            cwd = str(params.get("cwd") or os.getcwd())
            sid = (
                str(params.get("sessionId") or "").strip()
                if method == "session/load"
                else ""
            )
            if not sid:
                sid = f"ses_acp_{uuid.uuid4().hex[:16]}"
            session = _AcpSession(sid, cwd)
            sessions[sid] = session
            writer.respond(
                req_id,
                {"sessionId": sid, "configOptions": _config_options(session)},
            )
            continue

        if method == "session/set_config_option":
            params = msg.get("params") or {}
            sid = str(params.get("sessionId") or "")
            session = sessions.get(sid)
            if session is None:
                writer.respond_error(
                    req_id, ERR_INVALID_PARAMS, f"unknown sessionId {sid!r}"
                )
                continue
            config_id = str(params.get("configId") or "")
            value = str(params.get("value") or "")
            if config_id == "provider":
                session.provider = value
                session.model = ""  # models are per-provider; reset pick
            elif config_id == "model":
                session.model = value
            writer.respond(
                req_id, {"configOptions": _config_options(session)}
            )
            continue

        if method == "session/prompt":
            params = msg.get("params") or {}
            sid = str(params.get("sessionId") or "")
            session = sessions.get(sid)
            if session is None:
                writer.respond_error(
                    req_id, ERR_INVALID_PARAMS, f"unknown sessionId {sid!r}"
                )
                continue
            if session.busy:
                writer.respond_error(
                    req_id, ERR_INVALID_REQUEST, "session already prompting"
                )
                continue
            message = _prompt_text(params.get("prompt"))
            problem = _provider_problem(session.provider)
            if problem:
                _chunk(
                    writer,
                    sid,
                    f"provider not configured: {problem}\n"
                    "Use the client's Authenticate button or set host "
                    "secrets.json / env vars, then retry.",
                )
                writer.respond(req_id, {"stopReason": "end_turn"})
                continue
            session.busy = True
            session.cancel.clear()
            _start_bridge(session)
            try:
                stop_reason = _run_prompt(config, writer, session, message)
            except Exception as exc:  # loop spawn failure must still answer
                _chunk(writer, sid, f"[turn failed: {exc}]")
                stop_reason = "end_turn"
            finally:
                session.busy = False
            writer.respond(req_id, {"stopReason": stop_reason})
            continue

        if method == "session/cancel":
            params = msg.get("params") or {}
            sid = str(params.get("sessionId") or "")
            session = sessions.get(sid)
            if session is not None:
                session.cancel.set()
            continue  # notification: no response

        if method is None:
            continue  # stray result/notification-shaped dict

        if req_id is not None:
            writer.respond_error(req_id, ERR_METHOD_NOT_FOUND, method)

    bridge_stop.set()
    return 0


# ---------------------------------------------------------------------------
# acp client — drive a foreign ACP agent, present a runner.loop-shaped CLI


def _acp_env(workdir: str = "") -> dict[str, str] | None:
    """Construct child env for foreign ACP agent subprocesses with role='acp'."""
    try:
        from agents_workspace import Workspace

        ws = Workspace.from_env()
        return (
            ws.env(workdir=workdir, role="acp")
            if workdir.strip()
            else ws.env(role="acp")
        )
    except Exception:
        pass
    try:
        from agents_workspace.workspace import (
            INFRA_SECRET_KEYS,
            LLM_SECRET_KEYS,
            _ACP_STRIP_PREFIXES,
            _TOOL_ONLY_STRIP_KEYS,
        )
    except ImportError:
        return None
    out = dict(os.environ)
    for key in list(out):
        if (
            key in INFRA_SECRET_KEYS
            or key == "AGENTS_SYNC_TOKEN"
            or key in LLM_SECRET_KEYS
            or key in _TOOL_ONLY_STRIP_KEYS
            or any(key.startswith(p) for p in _ACP_STRIP_PREFIXES)
        ):
            out.pop(key, None)
    return out


class _AgentConn:
    """Spawn an ACP agent child; send requests, consume responses/updates."""

    def __init__(
        self,
        argv: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=cwd,
            env=env,
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.writer = _Writer(self.proc.stdin)
        self._id = itertools.count(1)
        self.replies: dict[Any, "threading.Event"] = {}
        self.results: dict[Any, dict[str, Any]] = {}
        self.on_update: Callable[[dict[str, Any]], None] | None = None
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        for msg in _read_messages(self.proc.stdout):
            mid = msg.get("id")
            if "result" in msg or "error" in msg:
                if mid in self.replies:
                    self.results[mid] = msg
                    self.replies[mid].set()
                continue
            if msg.get("method") == "session/update" and self.on_update:
                self.on_update(msg.get("params") or {})
                continue
            if msg.get("method") and mid is not None:
                self._handle_client_request(msg)

    def _handle_client_request(self, msg: dict[str, Any]) -> None:
        """Agent -> client calls: answer permissions, refuse the rest."""
        method = msg["method"]
        if method == "session/request_permission":
            options = ((msg.get("params") or {}).get("options")) or []
            pick = "deny"
            for opt in options:
                if str(opt.get("kind", "")).startswith("allow"):
                    pick = str(opt.get("optionId"))
                    break
            self.writer.respond(
                msg["id"],
                {"outcome": {"outcome": "selected", "optionId": pick}},
            )
        else:
            # fs/terminal/elicitation not advertised; refuse loudly.
            self.writer.respond_error(
                msg["id"], ERR_METHOD_NOT_FOUND, f"client has no {method}"
            )

    def request(
        self, method: str, params: dict[str, Any], timeout: float = 0
    ) -> dict[str, Any]:
        rid = next(self._id)
        ev = threading.Event()
        self.replies[rid] = ev
        self.writer.send(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        )
        if not ev.wait(timeout if timeout > 0 else None):
            raise TimeoutError(f"{method}: no response")
        self.replies.pop(rid, None)
        return self.results.pop(rid, {})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.writer.notify(method, params)

    def close(self) -> None:
        try:
            self.proc.terminate()
        except Exception:
            pass


def _acp_sessions_dir(config: RelayConfig | None = None) -> Path:
    """Store ACP session id mappings under host cache (<host>/cache/acp_sessions)."""
    del config
    from agents_workspace import Host

    path = Host.from_env().cache / "acp_sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def acp_agent_cmd(provider: str = "") -> str:
    """Agent command for provider ``acp.<name>``.

    Manifest ``command`` wins (file-managed agents, ``POST /v1/providers``),
    then ``AGENTS_ACP_CMD_<NAME>`` over the shared ``AGENTS_ACP_CMD`` so a
    host can register several agents (``acp.devin`` -> ``AGENTS_ACP_CMD_DEVIN``).
    """
    if provider.startswith("acp."):
        try:
            from runner.providers import find_provider

            manifest = find_provider(provider)
            if isinstance(manifest, dict):
                cmd = str(manifest.get("command") or "").strip()
                if cmd:
                    return cmd
        except Exception:
            pass
    suffix = provider.split(".", 1)[1] if provider.startswith("acp.") else ""
    if suffix:
        val = os.environ.get(f"AGENTS_ACP_CMD_{suffix.upper()}", "").strip()
        if val:
            return val
    return os.environ.get("AGENTS_ACP_CMD", "").strip()


def acp_provider_name() -> str:
    """``acp.<name>`` — explicit AGENTS_ACP_NAME, else first argv basename."""
    name = os.environ.get("AGENTS_ACP_NAME", "").strip()
    if not name:
        import shlex

        cmd = acp_agent_cmd()
        argv = shlex.split(cmd, posix=(os.name != "nt")) if cmd else []
        name = Path(argv[0]).stem if argv else "agent"
    return f"acp.{name}"


def probe_acp_agent(agent_argv: list[str], timeout: float = 20) -> dict[str, Any]:
    """Spawn the agent briefly; collect its model/mode config options."""
    out: dict[str, Any] = {"model": "", "models": [], "mode": "", "modes": []}
    conn = _AgentConn(agent_argv, env=_acp_env())
    try:
        conn.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {},
                "clientInfo": {"name": "agents-relay", "version": "0.1.0"},
            },
            timeout=timeout,
        )
        res = conn.request(
            "session/new", {"cwd": os.getcwd(), "mcpServers": []}, timeout=timeout
        )
        for opt in (res.get("result") or {}).get("configOptions") or []:
            oid = str(opt.get("id") or "")
            if oid not in out:
                continue
            out[oid] = str(opt.get("currentValue") or "")
            out[oid + "s"] = [
                str(v.get("value")) for v in opt.get("options") or [] if v.get("value")
            ]
        return out
    finally:
        conn.close()


_ACP_INFO_TTL = 24 * 3600


def acp_models_cache_path(provider: str, config: RelayConfig | None = None) -> Path:
    """``<state>/acp_models/<name>.json`` for provider ``acp.<name>``."""
    name = provider.split(".", 1)[1] if provider.startswith("acp.") else provider
    base = Path(getattr(config, "state_dir", "") or ".")
    return base / "acp_models" / f"{name}.json"


def acp_agent_info(
    config: RelayConfig, provider: str = "", refresh: bool = False
) -> dict[str, Any]:
    """Cached agent probe (<state>/acp_models/<name>.json, 24h TTL)."""
    name = provider.split(".", 1)[1] if provider.startswith("acp.") else ""
    name = name or acp_provider_name().split(".", 1)[1]
    path = acp_models_cache_path(f"acp.{name}", config)
    cached = read_json(path) or {}
    if cached.get("models") and not refresh and (
        time.time() - float(cached.get("ts") or 0) < _ACP_INFO_TTL
    ):
        return cached
    cmd = acp_agent_cmd(provider)
    if not cmd:
        return {"error": "AGENTS_ACP_CMD unset", "model": "", "models": []}
    import shlex

    try:
        data = probe_acp_agent(shlex.split(cmd, posix=(os.name != "nt")))
        data["ts"] = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, data)
        return data
    except Exception as exc:
        if cached:
            cached["error"] = str(exc)
            return cached
        return {"error": str(exc), "model": "", "models": []}


def acp_provider_rows(config: RelayConfig, refresh: bool = False) -> list[dict[str, Any]]:
    """``acp-agent`` manifest rows enriched with probed model/mode lists.

    Rows surface even without a resolvable command — ``error`` marks a
    misconfigured agent instead of silently dropping the row.
    """
    ensure_acp_manifest()
    try:
        from runner.providers import list_providers
    except ImportError:
        return []
    rows: list[dict[str, Any]] = []
    for raw in list_providers():
        if raw.get("kind") != "acp-agent":
            continue
        name = str(raw.get("name") or "")
        row: dict[str, Any] = {
            "name": name, "kind": "acp-agent",
            "model": raw.get("model"), "when": raw.get("when"),
        }
        if str(raw.get("command") or "").strip():
            row["command"] = str(raw["command"]).strip()
        if not acp_agent_cmd(name):
            row["error"] = "no agent command configured"
            rows.append(row)
            continue
        # Plain listing must not spawn the agent — a slow ``ssh box`` probe
        # would hang every /v1/providers read. Probing happens on refresh=1
        # and the dedicated probe endpoint; here we surface the cache only.
        if refresh:
            info = acp_agent_info(config, name, refresh=True)
        else:
            info = read_json(acp_models_cache_path(name, config)) or {}
        for key in ("model", "models", "mode", "modes", "error"):
            if info.get(key):
                row[key] = info[key]
        rows.append(row)
    return rows


def ensure_acp_manifest() -> None:
    """Drop ``<host>/providers/acp.<name>.json`` when AGENTS_ACP_CMD is set.

    The provider-manifest overlay is the existing extension point: the row
    shows up in /v1/providers, /v1/model, and runner.model.use validation
    without touching any of them. ``kind=acp-agent`` (not the harness
    ``acp`` completer stub) marks turns for the acp-run spawn path.
    """
    if not acp_agent_cmd():
        return
    try:
        from agents_workspace import Host

        providers_dir = Host.from_env().dir / "providers"
    except Exception:
        return
    name = acp_provider_name()
    path = providers_dir / f"{name}.json"
    if path.exists():
        return
    payload = {
        "name": name,
        "kind": "acp-agent",
        "when": "on_request",
        "cadence": "on_request",
        "verb": "chat.completions",
        "rests_on": "Foreign ACP agent loop; agents-relay acp-run spawns it per turn",
        "expected_exit": 0,
        "source": "env",
    }
    providers_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_acp_turn(
    *,
    agent_argv: list[str],
    message: str,
    session: str,
    workdir: str,
    config: RelayConfig,
    deliver: str = "stream",
    model: str = "",
) -> int:
    """One loop-shaped turn against a foreign ACP agent.

    Prints reply tokens to stdout (stream) or the buffered reply, then the
    loop trailer marker + session JSON, mirroring runner.loop's contract so
    this works as a LOOP_CMD drop-in.
    """
    state_dir = _acp_sessions_dir(config)
    key = session or f"ses_acp_{uuid.uuid4().hex[:12]}"
    state_path = state_dir / f"{key}.json"
    stored = read_json(state_path) or {}
    conn = _AgentConn(agent_argv, cwd=workdir or None, env=_acp_env(workdir=workdir))
    try:
        init = conn.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {},
                "clientInfo": {"name": "agents-relay", "version": "0.1.0"},
            },
        )
        auth_methods = (init.get("result") or {}).get("authMethods") or []
        api_key = os.environ.get("AGENTS_ACP_API_KEY", "").strip()
        if auth_methods and api_key:
            res = conn.request(
                "authenticate",
                {"methodId": str(auth_methods[0].get("id")), "meta": {"api_key": api_key}},
            )
            if "error" in res:
                sys.stderr.write(f"acp authenticate failed: {res['error']}\n")
                return 1
        # No key: proceed anyway — the agent may already be authenticated
        # out-of-band (`devin auth login` creds, SSH, etc.). If not, the
        # session request below fails loudly instead of a speculative
        # refusal here.
        acp_sid = str(stored.get("acpSessionId") or "")
        loaded = False
        if acp_sid:
            res = conn.request(
                "session/load",
                {"sessionId": acp_sid, "cwd": workdir or os.getcwd(), "mcpServers": []},
            )
            loaded = "error" not in res
        if not loaded:
            res = conn.request(
                "session/new",
                {"cwd": workdir or os.getcwd(), "mcpServers": []},
            )
            if "error" in res:
                sys.stderr.write(f"acp session/new failed: {res['error']}\n")
                return 1
            acp_sid = str((res.get("result") or {}).get("sessionId") or "")
            if not acp_sid:
                sys.stderr.write("acp session/new returned no sessionId\n")
                return 1
            stored["acpSessionId"] = acp_sid
            write_json_atomic(state_path, stored)

        if model:
            res = conn.request(
                "session/set_config_option",
                {"sessionId": acp_sid, "configId": "model", "value": model},
            )
            if "error" in res:
                sys.stderr.write(
                    f"acp set model {model!r} failed: {res['error']} — agent default stays\n"
                )

        buffered: list[str] = []

        def _on_update(params: dict[str, Any]) -> None:
            update = params.get("update") or {}
            kind = update.get("sessionUpdate")
            if kind == "agent_message_chunk":
                text = str((update.get("content") or {}).get("text") or "")
                if deliver == "stream":
                    sys.stdout.write(text)
                    sys.stdout.flush()
                else:
                    buffered.append(text)

        conn.on_update = _on_update
        res = conn.request(
            "session/prompt",
            {
                "sessionId": acp_sid,
                "prompt": [{"type": "text", "text": message}],
            },
        )
        if deliver != "stream" and buffered:
            sys.stdout.write("".join(buffered))
        if deliver == "stream":
            sys.stdout.write("\n")
        if "error" in res:
            sys.stderr.write(f"acp session/prompt failed: {res['error']}\n")
            return 1
        sys.stdout.write(
            f"\n---agents-loop-trailer---\n{json.dumps({'session': key})}\n"
        )
        sys.stdout.flush()
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# argv compat shim: acp-run looks like runner.loop to LOOP_CMD


def acp_loop_main(args: Any, config: RelayConfig) -> int:
    """Drive AGENTS_ACP_CMD with the parsed loop-shaped flags.

    Unknown runner.loop flags are ignored at the parser layer so this works
    as a LOOP_CMD drop-in without tracking the loop's full CLI."""
    provider = str(getattr(args, "provider", "") or "").strip()
    raw_cmd = acp_agent_cmd(provider)
    if not raw_cmd:
        sys.stderr.write(
            "AGENTS_ACP_CMD is unset — the ACP agent command "
            '(e.g. "devin acp" or "ssh box devin acp") is required.\n'
        )
        return 2
    import shlex

    agent_argv = shlex.split(raw_cmd, posix=(os.name != "nt"))
    session = "" if getattr(args, "new_session", False) else getattr(args, "session", "")
    return run_acp_turn(
        agent_argv=agent_argv,
        message=getattr(args, "message", "") or "",
        session=session,
        workdir=getattr(args, "workdir", "") or "",
        config=config,
        deliver="buffered" if getattr(args, "deliver", "stream") == "buffered" else "stream",
        model=str(getattr(args, "model", "") or "").strip(),
    )
