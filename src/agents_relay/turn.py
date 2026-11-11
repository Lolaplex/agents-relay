"""Turn request model and loop subprocess helpers."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterator

from .config import GatewayConfig

LOOP_TRAILER_MARKER = "---agents-loop-trailer---"


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


def build_loop_argv(params: TurnParams, config: GatewayConfig) -> list[str]:
    provider = (params.provider or config.loop_provider).strip() or config.loop_provider
    argv = list(config.loop_cmd)
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
    if params.model:
        argv.extend(["--model", params.model])
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
    return argv


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


def workspace_env(ref: str, workdir: str = "") -> dict[str, str] | None:
    """Env for a turn that must run inside a named workspace.

    Empty ref means the process default, so the subprocess inherits the env.
    ``workdir`` sets the session cwd (``AGENTS_WORKDIR``) for that child only.
    """
    needle = (ref or "").strip()
    if not needle:
        return None
    ws = resolve_workspace_id(needle)
    return ws.env(workdir=workdir) if workdir.strip() else ws.env()


def run_loop_turn(
    *,
    channel: str,
    user: str,
    message: str,
    config: GatewayConfig | None = None,
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
    cfg = config or GatewayConfig.from_env()
    argv = build_loop_argv(params, cfg)
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        shell=False,
        env=workspace_env(workspace, workdir),
    )
    parsed = parse_loop_stdout(proc.stdout or "")
    return LoopTurnResult(
        reply=parsed["reply"],
        session=parsed["session"],
        user_id=parsed["user_id"],
        alias=parsed["alias"],
        returncode=int(proc.returncode),
        stderr=proc.stderr or "",
    )


def iter_loop_stream(
    params: TurnParams,
    config: GatewayConfig | None = None,
    timeout_sec: int = 600,
) -> Iterator[StreamEvent]:
    """Yield SSE-shaped events from runner.loop --deliver stream."""
    cfg = config or GatewayConfig.from_env()
    stream_params = replace(params, deliver="stream")
    argv = build_loop_argv(stream_params, cfg)
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=workspace_env(params.workspace, params.workdir),
    )
    assert proc.stdout is not None
    marker = LOOP_TRAILER_MARKER
    pending = ""
    emitted = 0
    try:
        while True:
            chunk = proc.stdout.read(1)
            if chunk:
                pending += chunk
                if marker in pending:
                    body, _, trailer_part = pending.partition(marker)
                    lines = [ln for ln in trailer_part.strip().splitlines() if ln.strip()]
                    meta: dict[str, Any] | None = None
                    if lines:
                        try:
                            meta = json.loads(lines[0])
                        except json.JSONDecodeError:
                            meta = None
                    if meta is None and proc.poll() is None:
                        continue
                    new_text = body[emitted:]
                    if new_text:
                        yield StreamEvent(type="delta", text=new_text)
                    emitted = len(body)
                    meta = meta or {}
                    rc = int(proc.wait(timeout=timeout_sec))
                    yield StreamEvent(
                        type="trailer",
                        session=str(meta.get("session") or ""),
                        user_id=str(meta.get("user_id") or ""),
                        alias=str(meta.get("alias") or ""),
                        returncode=rc,
                    )
                    return
                safe = len(pending) - len(marker)
                if safe > emitted:
                    piece = pending[emitted:safe]
                    emitted = safe
                    yield StreamEvent(type="delta", text=piece)
            elif proc.poll() is not None:
                break
        if marker in pending:
            body, _, trailer_part = pending.partition(marker)
            lines = [ln for ln in trailer_part.strip().splitlines() if ln.strip()]
            meta: dict[str, Any] | None = None
            if lines:
                try:
                    meta = json.loads(lines[0])
                except json.JSONDecodeError:
                    meta = None
            new_text = body[emitted:]
            if new_text:
                yield StreamEvent(type="delta", text=new_text)
            meta = meta or {}
            rc = int(proc.wait(timeout=timeout_sec))
            yield StreamEvent(
                type="trailer",
                session=str(meta.get("session") or ""),
                user_id=str(meta.get("user_id") or ""),
                alias=str(meta.get("alias") or ""),
                returncode=rc,
            )
            return
        rc = int(proc.wait(timeout=timeout_sec))
        if pending and marker not in pending:
            tail = pending[emitted:]
            if tail:
                yield StreamEvent(type="delta", text=tail)
            parsed = parse_loop_stdout(pending)
            yield StreamEvent(
                type="trailer",
                session=parsed["session"],
                user_id=parsed["user_id"],
                alias=parsed["alias"],
                returncode=rc,
            )
    finally:
        if proc.poll() is None:
            proc.kill()


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
    config: GatewayConfig | None = None,
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
    """
    cfg = config or GatewayConfig.from_env()
    argv = build_loop_argv(replace(params, deliver="stream"), cfg)
    popen_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        # Own process group so cancel kills the loop and its tool children.
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=workspace_env(params.workspace, params.workdir),
        **popen_kwargs,
    )
    if on_pid is not None and proc.pid:
        try:
            on_pid(int(proc.pid))
        except Exception:
            pass

    events: "queue.Queue[tuple[str, str]]" = queue.Queue()

    def _pump(stream, tag: str, *, by_line: bool) -> None:
        try:
            if stream is None:
                return
            if by_line:
                for line in stream:
                    events.put((tag, line))
            else:
                while True:
                    piece = stream.read(1)
                    if not piece:
                        break
                    events.put((tag, piece))
        except Exception:
            pass
        finally:
            events.put((tag + "-eof", ""))

    threading.Thread(target=_pump, args=(proc.stdout, "out"), kwargs={"by_line": False}, daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr, "err"), kwargs={"by_line": True}, daemon=True).start()

    marker = LOOP_TRAILER_MARKER
    pending = ""
    emitted = 0
    out_eof = False
    err_eof = False
    deadline = time.monotonic() + max(1, int(timeout_sec))

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
                yield StreamEvent(type="error", text="Turn timed out.")
                return
            try:
                tag, data = events.get(timeout=0.25)
            except queue.Empty:
                if out_eof and err_eof:
                    break
                continue

            if tag == "err":
                line = data.strip()
                if line:
                    yield StreamEvent(type="status", text=line)
                continue
            if tag == "err-eof":
                err_eof = True
                continue
            if tag == "out-eof":
                out_eof = True
                continue

            pending += data
            if marker in pending:
                if not out_eof and proc.poll() is None:
                    continue
                body, _, trailer_part = pending.partition(marker)
                tail = body[emitted:]
                if tail:
                    yield StreamEvent(type="delta", text=tail)
                emitted = len(body)
                meta = _parse_trailer(trailer_part)
                rc = proc.wait(timeout=5) if proc.poll() is None else int(proc.returncode or 0)
                yield StreamEvent(
                    type="trailer",
                    session=str(meta.get("session") or ""),
                    user_id=str(meta.get("user_id") or ""),
                    alias=str(meta.get("alias") or ""),
                    returncode=rc,
                )
                return
            safe = len(pending) - len(marker)
            if safe > emitted:
                piece = pending[emitted:safe]
                emitted = safe
                yield StreamEvent(type="delta", text=piece)

        rc = proc.wait(timeout=5) if proc.poll() is None else int(proc.returncode or 0)
        # The marker may have arrived in the same chunk that ended the stream, so
        # the loop above skipped it while waiting for EOF. Strip it here.
        if marker in pending:
            body, _, trailer_part = pending.partition(marker)
            tail = body[emitted:]
            if tail:
                yield StreamEvent(type="delta", text=tail)
            meta = _parse_trailer(trailer_part)
        else:
            tail = pending[emitted:]
            if tail:
                yield StreamEvent(type="delta", text=tail)
            parsed = parse_loop_stdout(pending)
            meta = {
                "session": parsed["session"],
                "user_id": parsed["user_id"],
                "alias": parsed["alias"],
            }
        yield StreamEvent(
            type="trailer",
            session=str(meta.get("session") or ""),
            user_id=str(meta.get("user_id") or ""),
            alias=str(meta.get("alias") or ""),
            returncode=rc,
        )
    finally:
        if proc.poll() is None:
            _kill()
