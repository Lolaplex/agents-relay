"""Turn request model and loop subprocess helpers."""

from __future__ import annotations

import codecs
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Iterator

from .config import RelayConfig

LOOP_TRAILER_MARKER = "---agents-loop-trailer---"
_STREAM_READ = 4096
_BATCH_MAX = 256
_BATCH_WAIT = 0.03
_KEEPALIVE_SEC = 12.0


def _pipe_reader(stream):
    """Return a callable reading one OS-pipe chunk (<=_STREAM_READ bytes).

    ``read1`` on the buffered reader returns whatever the kernel has ready;
    raw FileIO falls back to ``os.read`` on the fd. Never blocks for a full
    buffer, so tokens flush as soon as the child writes them.
    """
    raw = getattr(stream, "buffer", None) or stream
    read1 = getattr(raw, "read1", None)
    if callable(read1):
        return lambda: read1(_STREAM_READ)
    fileno = getattr(raw, "fileno", None)
    if callable(fileno):
        return lambda: os.read(raw.fileno(), _STREAM_READ)
    read = getattr(stream, "read", None)
    if callable(read):
        return lambda: read(_STREAM_READ)
    raise TypeError("stream is not readable")


def _decode(dec, data) -> str:
    return data if isinstance(data, str) else dec.decode(data)


def _utf8_decoder():
    return codecs.getincrementaldecoder("utf-8")(errors="replace")


@dataclass
class TurnParams:
    channel: str = "http"
    user: str = "anonymous"
    message: str = ""
    session: str = ""
    user_id: str = ""
    new_session: bool = False
    provider: str = ""
    model: str = ""
    persona: str = ""
    project: str = ""
    workspace: str = ""
    workdir: str = ""
    deliver: str = "buffered"
    attachments: tuple[str, ...] = ()


@dataclass
class LoopTurnResult:
    reply: str
    session: str
    user_id: str
    alias: str
    returncode: int
    stderr: str


@dataclass
class StreamEvent:
    type: str
    text: str = ""
    session: str = ""
    user_id: str = ""
    alias: str = ""
    returncode: int = 0


def parse_loop_stdout(stdout: str) -> dict[str, Any]:
    """Split user reply from trailer JSON after LOOP_TRAILER_MARKER."""
    text = stdout or ""
    if LOOP_TRAILER_MARKER in text:
        body, _, trailer_part = text.partition(LOOP_TRAILER_MARKER)
        reply = body.strip()
        trailer_line = trailer_part.strip().splitlines()[0] if trailer_part.strip() else "{}"
        try:
            meta = json.loads(trailer_line)
        except json.JSONDecodeError:
            meta = {}
    else:
        reply = text.strip()
        meta = {}
    return {
        "reply": reply,
        "session": str(meta.get("session") or ""),
        "user_id": str(meta.get("user_id") or ""),
        "alias": str(meta.get("alias") or ""),
    }


def turn_params_from_body(body: dict[str, Any]) -> TurnParams:
    return TurnParams(
        channel=str(body.get("channel") or "http"),
        user=str(body.get("user") or "anonymous"),
        message=str(body.get("text") or body.get("message") or ""),
        session=str(body.get("session") or ""),
        user_id=str(body.get("user_id") or ""),
        new_session=bool(body.get("new_session")),
        provider=str(body.get("provider") or ""),
        model=str(body.get("model") or ""),
        persona=str(body.get("persona") or ""),
        project=str(body.get("project") or ""),
        workspace=str(body.get("workspace") or ""),
        workdir=str(body.get("workdir") or "").strip(),
    )


def _resolve_provider_and_model(
    params: TurnParams, config: RelayConfig
) -> tuple[str, str]:
    provider = params.provider.strip()
    model = params.model.strip()
    if params.workspace and (not provider or not model):
        # Workspace pick fills unset slots (flag > pick > env default).
        try:
            from runner.model import selected

            pick = selected(resolve_workspace_id(params.workspace))
            provider = provider or str(pick.get("provider") or "")
            model = model or str(pick.get("model") or "")
        except Exception:
            pass
    return provider or config.loop_provider, model


def _flag_from_argv(argv: list[str] | tuple[str, ...], flag: str) -> str:
    items = list(argv)
    prefix = f"{flag}="
    for idx, item in enumerate(items):
        if item == flag and idx + 1 < len(items):
            return str(items[idx + 1]).strip()
        if item.startswith(prefix):
            return item[len(prefix) :].strip()
    return ""


def _provider_from_argv(argv: list[str] | tuple[str, ...]) -> str:
    return _flag_from_argv(argv, "--provider")


def _persona_from_argv(argv: list[str] | tuple[str, ...]) -> str:
    return _flag_from_argv(argv, "--persona")


def _provider_key_env(provider: str, ws: "Workspace | None" = None) -> str:
    """Resolve the active provider's API key env var from its manifest."""
    name = (provider or "").strip()
    if not name and ws is not None:
        try:
            from runner.model import selected

            name = str(selected(ws).get("provider") or "").strip()
        except Exception:
            pass
    if not name:
        name = os.environ.get("LOOP_PROVIDER", "").strip()
    if not name:
        return ""
    try:
        from runner.providers import find_provider

        manifest = find_provider(name)
    except Exception:
        manifest = None
    if not isinstance(manifest, dict):
        try:
            from agents_workspace import Host

            candidate = Host.from_env().dir / "providers" / f"{name}.json"
            if candidate.is_file():
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    manifest = raw
        except Exception:
            manifest = None
    if isinstance(manifest, dict):
        key_env = str(manifest.get("key_env") or "").strip()
        if key_env:
            return key_env
        if manifest.get("kind", "openai_compat") == "openai_compat":
            return "LLM_API_KEY"
    return ""


def build_loop_argv(params: TurnParams, config: RelayConfig) -> list[str]:
    provider, model = _resolve_provider_and_model(params, config)
    argv = list(config.loop_cmd)
    if provider.startswith("acp."):
        # Foreign ACP agent drives the turn; model maps to a config option.
        argv = [sys.executable, "-m", "agents_relay", "acp-run"]
    argv.extend(
        [
            "--channel",
            params.channel,
            "--user",
            params.user,
            "--message",
            params.message,
            "--complete",
            "--provider",
            provider,
            "--deliver",
            params.deliver,
        ]
    )
    if model:
        argv.extend(["--model", model])
    if params.session:
        argv.extend(["--session", params.session])
    if params.user_id:
        argv.extend(["--user-id", params.user_id])
    if params.new_session:
        argv.append("--new-session")
    if params.project:
        argv.extend(["--project", params.project])
    if params.persona:
        argv.extend(["--persona", params.persona])
    for path in params.attachments:
        if path:
            argv.extend(["--attach", str(path)])
    return argv


def prepare_loop(params: TurnParams, config: RelayConfig) -> tuple[TurnParams, list[str], dict[str, str]]:
    """Mirror attachments into the workdir and decide the vision notice.

    The returned env is merged into the child before container path rewrite.
    `AGENTS_RELAY_VISION` (`on`, `off`, and the resolved `auto` decision)
    becomes `AGENTS_VISION` so the harness shows or withholds image parts
    the same way the reply explains.
    """
    from .attachments import prepare_turn_files, vision_active, vision_env, vision_notice

    prepared = prepare_turn_files(
        params.attachments,
        config=config,
        workspace=params.workspace,
        workdir=params.workdir,
    )
    provider = (params.provider or config.loop_provider).strip() or config.loop_provider
    active = vision_active(config, provider)
    notice = vision_notice(prepared.paths, active=active)
    notes = list(prepared.notes)
    message = params.message
    if notice:
        notes.append(notice)
        message = f"{message}\n\n{notice}".strip() if message else notice
    extra = vision_env(config, active=active, provider=provider)
    if prepared.allow_root:
        extra["AGENTS_WORKDIR_ALLOW"] = prepared.allow_root
    updated = replace(
        params,
        attachments=prepared.paths,
        message=message,
    )
    return updated, notes, extra


def _merge_extra_env(
    base: dict[str, str] | None, extra: dict[str, str] | None
) -> dict[str, str] | None:
    env = dict(os.environ if base is None else base)
    # Child stdout must be UTF-8 regardless of the host locale: on cp1252
    # Windows the default codec turns umlauts into mojibake downstream.
    env.setdefault("PYTHONUTF8", "1")
    if not extra:
        return env
    for key, value in extra.items():
        if key == "AGENTS_WORKDIR_ALLOW" and env.get(key):
            parts = [part for part in env[key].split(os.pathsep) if part]
            for piece in value.split(os.pathsep):
                if piece and piece not in parts:
                    parts.append(piece)
            env[key] = os.pathsep.join(parts)
        else:
            env[key] = value
    return env


def _container_rewrite(
    argv: list[str],
    workspace: str,
    workdir: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str] | None, "Workspace | None"]:
    """Resolve workspace isolation. Returns (argv, env, ws) — ws set only
    when isolation=container so callers can dispatch via runtime.spawn/run.
    Without docker + container isolation the runtime raises (loud, no
    silent local fallback)."""
    provider = _provider_from_argv(argv)
    env = _merge_extra_env(
        workspace_env(workspace, workdir, provider=provider), extra_env
    )
    if not workspace:
        return argv, env, None
    try:
        ws = resolve_workspace_id(workspace)
    except Exception:
        return argv, env, None
    if env is not None and not env.get("AGENTS_ACTOR_DID"):
        try:
            from .store_api import resolve_store_actor

            actor_did = resolve_store_actor(ws, persona=_persona_from_argv(argv))
            if actor_did:
                env["AGENTS_ACTOR_DID"] = actor_did
        except Exception:
            pass
    try:
        from agents_workspace.runtime import isolation_of, to_container_argv, to_container_env
    except ImportError:
        return argv, env, None
    if isolation_of(ws) != "container":
        return argv, env, None
    return to_container_argv(ws, argv), to_container_env(ws, env), ws


_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def valid_id(ref: str) -> bool:
    """A workspace or persona id: no separators, no traversal, no labels."""
    needle = (ref or "").strip()
    return bool(_ID_RE.fullmatch(needle)) and ".." not in needle


def resolve_workspace_id(ref: str) -> "Workspace":
    """Resolve an id under the host. Raises ValueError for ids, labels, paths."""
    from agents_workspace import Host, Workspace

    needle = (ref or "").strip()
    if not valid_id(needle):
        raise ValueError(f"invalid workspace id: {needle!r}")
    host = Host.from_env()
    if not host.workspace_exists(needle):
        raise ValueError(f"unknown workspace: {needle}")
    return host.workspace(needle)


def workspace_env(
    ref: str,
    workdir: str = "",
    *,
    provider: str = "",
    approver: str = "",
    extra_allow: Iterable[str] = (),
) -> dict[str, str] | None:
    """Env for a turn that must run inside a named workspace.

    Empty ref means the process default, so the subprocess inherits the env.
    ``workdir`` sets the session cwd (``AGENTS_WORKDIR``) for that child only.
    Passes ``AGENTS_APPROVAL_TOKEN`` and ``AGENTS_RELAY_URL`` via ``extra_allow``.
    """
    del approver
    needle = (ref or "").strip()
    if not needle:
        return None
    ws = resolve_workspace_id(needle)
    try:
        from agents_workspace.runtime import isolation_of

        isolation = isolation_of(ws)
    except ImportError:
        isolation = "container" if (ws.isolation or "").strip().lower() == "container" else "local"
    role = "container" if isolation == "container" else "turn"
    key_env = _provider_key_env(provider, ws)
    allow: list[str] = ["AGENTS_APPROVAL_TOKEN", "AGENTS_RELAY_URL"]
    for item in extra_allow:
        key = str(item).strip()
        if key and key not in allow:
            allow.append(key)
    kwargs: dict[str, Any] = {
        "role": role,
        "provider_key_env": key_env,
        "extra_allow": tuple(allow),
    }
    base = dict(os.environ)
    if workdir.strip():
        kwargs["workdir"] = workdir
    else:
        # The relay daemon's ambient AGENTS_WORKDIR/LOCK (e.g. pinned by its
        # entry workspace) must not leak into another workspace's turn —
        # workspace.json's own workdir/workdir_lock applies instead.
        base.pop("AGENTS_WORKDIR", None)
        base.pop("AGENTS_WORKDIR_LOCK", None)
    return ws.env(base, **kwargs)


def _resolve_relay_url(config: RelayConfig | None = None) -> str:
    raw = os.environ.get("AGENTS_RELAY_URL", "").strip()
    if raw:
        return raw.rstrip("/")
    if config is not None:
        cfg_url = str(getattr(config, "relay_url", "") or "").strip()
        if cfg_url:
            return cfg_url.rstrip("/")
    return ""


@contextmanager
def turn_approval_env(
    workspace: str = "",
    *,
    config: RelayConfig | None = None,
    ttl_sec: float = 3600.0,
) -> Iterator[dict[str, str]]:
    """Mint an ephemeral per-turn AGENTS_APPROVAL_TOKEN and revoke it on exit."""
    from .approvals import mint_approval_token, revoke_approval_token

    ws_id = ""
    if (workspace or "").strip():
        try:
            ws_id = resolve_workspace_id(workspace).id
        except Exception:
            ws_id = workspace.strip()
    else:
        ws_id = os.environ.get("AGENTS_WORKSPACE", "").strip()
    token, token_hash = mint_approval_token(ws_id, ttl_sec=ttl_sec)
    extra: dict[str, str] = {"AGENTS_APPROVAL_TOKEN": token}
    relay_url = _resolve_relay_url(config)
    if relay_url:
        extra["AGENTS_RELAY_URL"] = relay_url
    try:
        yield extra
    finally:
        revoke_approval_token(token_hash, is_hash=True)


def run_loop_turn(
    *,
    channel: str,
    user: str,
    message: str,
    config: RelayConfig | None = None,
    session: str = "",
    user_id: str = "",
    new_session: bool = False,
    provider: str = "",
    model: str = "",
    persona: str = "",
    project: str = "",
    workspace: str = "",
    workdir: str = "",
    timeout_sec: int = 600,
    interrupt_file: str = "",
) -> LoopTurnResult:
    params = TurnParams(
        channel=channel,
        user=user,
        message=message,
        session=session,
        user_id=user_id,
        new_session=new_session,
        provider=provider,
        model=model,
        persona=persona,
        project=project,
        workspace=workspace,
        workdir=workdir,
        deliver="buffered",
    )
    cfg = config or RelayConfig.from_env()
    params, notes, extra = prepare_loop(params, cfg)
    extra = dict(extra or {})
    if interrupt_file:
        extra["AGENTS_INTERRUPT_FILE"] = interrupt_file
    argv = build_loop_argv(params, cfg)
    ttl_sec = max(float(timeout_sec or 0), 300.0) + 60.0 if timeout_sec and timeout_sec > 0 else 3600.0
    with turn_approval_env(params.workspace, config=cfg, ttl_sec=ttl_sec) as approval_extra:
        extra.update(approval_extra)
        argv, env, ws = _container_rewrite(argv, params.workspace, params.workdir, extra)
        if ws is not None:
            from agents_workspace.runtime import run as ws_run

            proc = ws_run(ws, argv, env, capture=True, timeout=timeout_sec)
        else:
            proc = subprocess.run(
                argv,
                capture_output=True,
                encoding="utf-8", errors="replace",
                timeout=timeout_sec,
                shell=False,
                env=env,
            )
    parsed = parse_loop_stdout(proc.stdout or "")
    from .attachments import apply_notes

    return LoopTurnResult(
        reply=apply_notes(parsed["reply"], notes),
        session=parsed["session"],
        user_id=parsed["user_id"],
        alias=parsed["alias"],
        returncode=int(proc.returncode),
        stderr=proc.stderr or "",
    )


def _spawn_stream(argv: list[str], env: dict[str, str] | None, workspace: str, workdir: str, extra: dict[str, str]):
    """Shared spawn for both stream iterators: binary pipes for chunk reads."""
    argv2, env2, ws = _container_rewrite(list(argv), workspace, workdir, extra)
    if ws is not None:
        from agents_workspace.runtime import spawn as ws_spawn

        return ws_spawn(ws, argv2, env2, pipes=True), ws
    popen_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        # Own process group so cancel kills the loop and its tool children.
        popen_kwargs["start_new_session"] = True
    return subprocess.Popen(
        argv2,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env=env2,
        **popen_kwargs,
    ), ws


def _finish_stream(
    pending: str,
    emitted: int,
    notes: list[str],
    meta: dict[str, Any] | None,
    returncode: int,
) -> Iterator[StreamEvent]:
    """Tail delta + attachment notes + trailer — shared by both iterators."""
    marker = LOOP_TRAILER_MARKER
    body = pending.partition(marker)[0] if marker in pending else pending
    tail = body[emitted:]
    if tail:
        yield StreamEvent(type="delta", text=tail)
    if meta is None:
        meta = _parse_trailer(pending.partition(marker)[2]) if marker in pending else None
    if meta is None:
        parsed = parse_loop_stdout(pending)
        meta = {
            "session": parsed["session"],
            "user_id": parsed["user_id"],
            "alias": parsed["alias"],
        }
    yield from _note_deltas(notes, body)
    yield StreamEvent(
        type="trailer",
        session=str(meta.get("session") or ""),
        user_id=str(meta.get("user_id") or ""),
        alias=str(meta.get("alias") or ""),
        returncode=returncode,
    )


def iter_loop_stream(
    params: TurnParams,
    config: RelayConfig | None = None,
    timeout_sec: int = 600,
) -> Iterator[StreamEvent]:
    """Yield SSE-shaped events from runner.loop --deliver stream."""
    cfg = config or RelayConfig.from_env()
    params, notes, extra = prepare_loop(params, cfg)
    stream_params = replace(params, deliver="stream")
    argv = build_loop_argv(stream_params, cfg)
    ttl_sec = max(float(timeout_sec or 0), 300.0) + 60.0 if timeout_sec and timeout_sec > 0 else 3600.0
    with turn_approval_env(params.workspace, config=cfg, ttl_sec=ttl_sec) as approval_extra:
        merged_extra = {**dict(extra or {}), **approval_extra}
        proc, _ws = _spawn_stream(
            argv, env=None, workspace=params.workspace, workdir=params.workdir, extra=merged_extra
        )
        assert proc.stdout is not None
        read = _pipe_reader(proc.stdout)
        dec = _utf8_decoder()
        marker = LOOP_TRAILER_MARKER
        pending = ""
        emitted = 0
        try:
            while True:
                block = read()
                if block:
                    pending += _decode(dec, block)
                else:
                    pending += dec.decode(b"", final=True)
                    break
                if marker in pending:
                    body, _, trailer_part = pending.partition(marker)
                    meta = _parse_trailer(trailer_part) or None
                    if meta is None and proc.poll() is None:
                        continue
                    tail = body[emitted:]
                    if tail:
                        yield StreamEvent(type="delta", text=tail)
                    emitted = len(body)
                    rc = int(proc.wait(timeout=timeout_sec))
                    yield from _finish_stream(pending, emitted, notes, meta, rc)
                    return
                safe = len(pending) - len(marker)
                if safe > emitted:
                    piece = pending[emitted:safe]
                    emitted = safe
                    yield StreamEvent(type="delta", text=piece)
            rc = int(proc.wait(timeout=timeout_sec))
            yield from _finish_stream(pending, emitted, notes, None, rc)
        finally:
            if proc.poll() is None:
                proc.kill()


def _note_deltas(notes: list[str], body: str) -> Iterator[StreamEvent]:
    """Surface attachment notes the model reply did not already include."""
    from .attachments import apply_notes

    missing = [note for note in notes if note and note not in (body or "")]
    extra = apply_notes("", missing)
    if not extra:
        return
    prefix = "\n\n" if (body or "").strip() else ""
    yield StreamEvent(type="delta", text=prefix + extra)


def _parse_trailer(trailer_part: str) -> dict[str, Any]:
    lines = [ln for ln in (trailer_part or "").strip().splitlines() if ln.strip()]
    if not lines:
        return {}
    try:
        meta = json.loads(lines[0])
    except json.JSONDecodeError:
        return {}
    return meta if isinstance(meta, dict) else {}


def iter_loop_stream_live(
    params: TurnParams,
    config: RelayConfig | None = None,
    *,
    timeout_sec: int = 900,
    cancel: threading.Event | None = None,
    on_pid: Callable[[int], None] | None = None,
) -> Iterator[StreamEvent]:
    """Stream one turn, interleaving stderr progress with stdout answer deltas.

    ``iter_loop_stream`` only forwards stdout, so a web client sees nothing while
    tools run. Here both pipes feed one queue: stderr lines become ``status``
    events, stdout characters become ``delta`` events. ``cancel`` kills the whole
    process tree, which is what a browser stop button needs.

    The wall-clock cap is the whole turn (spawn to trailer), not one tool call.
    ``AGENTS_RELAY_TURN_TIMEOUT`` overrides the 900s default.
    """
    try:
        timeout_sec = int(os.environ.get("AGENTS_RELAY_TURN_TIMEOUT", "") or timeout_sec)
    except ValueError:
        pass
    cfg = config or RelayConfig.from_env()
    params, notes, extra = prepare_loop(params, cfg)
    argv = build_loop_argv(replace(params, deliver="stream"), cfg)
    ttl_sec = max(float(timeout_sec or 0), 300.0) + 60.0 if timeout_sec and timeout_sec > 0 else 3600.0
    with turn_approval_env(params.workspace, config=cfg, ttl_sec=ttl_sec) as approval_extra:
        merged_extra = {**dict(extra or {}), **approval_extra}
        proc, _ws = _spawn_stream(
            argv, env=None, workspace=params.workspace, workdir=params.workdir, extra=merged_extra
        )
        if on_pid is not None and proc.pid:
            try:
                on_pid(int(proc.pid))
            except Exception:
                pass

        events: "queue.Queue[tuple[str, str]]" = queue.Queue()

        def _pump(stream, tag: str) -> None:
            """OS-chunk pipe reads + incremental UTF-8 decode.

            stdout events are chunk deltas (the consumer batches them); stderr
            events are complete lines, so a partial last line stays buffered.
            """
            try:
                if stream is None:
                    return
                read = _pipe_reader(stream)
                dec = _utf8_decoder()
                buf = ""
                while True:
                    try:
                        block = read()
                    except OSError:
                        break
                    if not block:
                        break
                    text = _decode(dec, block)
                    if tag == "err":
                        buf += text
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            events.put((tag, line + "\n"))
                    else:
                        events.put((tag, text))
                buf += dec.decode(b"", final=True)
                if buf:
                    events.put((tag, buf))
            except Exception:
                pass
            finally:
                events.put((tag + "-eof", ""))

        threading.Thread(target=_pump, args=(proc.stdout, "out"), daemon=True).start()
        threading.Thread(target=_pump, args=(proc.stderr, "err"), daemon=True).start()

        marker = LOOP_TRAILER_MARKER
        pending = ""
        emitted = 0
        out_eof = False
        err_eof = False
        deadline = time.monotonic() + max(1, int(timeout_sec))
        outbuf = ""
        last_emit = time.monotonic()

        def _kill() -> None:
            from .jobs import kill_process_tree

            if proc.poll() is None:
                kill_process_tree(proc.pid)

        try:
            while True:
                if cancel is not None and cancel.is_set():
                    _kill()
                    return
                if time.monotonic() > deadline:
                    _kill()
                    from .attachments import apply_notes

                    yield StreamEvent(type="error", text=apply_notes("Turn timed out.", notes))
                    return
                try:
                    tag, data = events.get(timeout=_BATCH_WAIT)
                except queue.Empty:
                    # >=30ms since the last event: flush the delta batch, keep the
                    # connection alive during silent tool phases, or end on EOF.
                    if outbuf:
                        piece, outbuf = outbuf, ""
                        last_emit = time.monotonic()
                        yield StreamEvent(type="delta", text=piece)
                    elif time.monotonic() - last_emit > _KEEPALIVE_SEC and not (out_eof and err_eof):
                        last_emit = time.monotonic()
                        yield StreamEvent(type="keepalive")
                    if out_eof and err_eof:
                        break
                    continue

                if tag == "err":
                    if outbuf:
                        piece, outbuf = outbuf, ""
                        yield StreamEvent(type="delta", text=piece)
                    line = data.strip()
                    if line:
                        last_emit = time.monotonic()
                        yield StreamEvent(type="status", text=line)
                    continue
                if tag == "err-eof":
                    err_eof = True
                    continue
                if tag == "out-eof":
                    out_eof = True
                    if outbuf:
                        piece, outbuf = outbuf, ""
                        yield StreamEvent(type="delta", text=piece)
                    continue

                pending += data
                if marker in pending:
                    if not out_eof and proc.poll() is None:
                        continue
                    body, _, trailer_part = pending.partition(marker)
                    # emitted already counts the safe region parked in outbuf —
                    # the tail is the batch plus whatever came after it.
                    tail = outbuf + body[emitted:]
                    outbuf = ""
                    if tail:
                        yield StreamEvent(type="delta", text=tail)
                    emitted = len(body)
                    meta = _parse_trailer(trailer_part)
                    rc = proc.wait(timeout=5) if proc.poll() is None else int(proc.returncode or 0)
                    yield from _finish_stream(pending, emitted, notes, meta, rc)
                    return
                safe = len(pending) - len(marker)
                if safe > emitted:
                    outbuf += pending[emitted:safe]
                    emitted = safe
                if outbuf and (len(outbuf) >= _BATCH_MAX or outbuf.endswith("\n")):
                    piece, outbuf = outbuf, ""
                    last_emit = time.monotonic()
                    yield StreamEvent(type="delta", text=piece)

            rc = proc.wait(timeout=5) if proc.poll() is None else int(proc.returncode or 0)
            yield from _finish_stream(pending, emitted, notes, None, rc)
        finally:
            if proc.poll() is None:
                _kill()
