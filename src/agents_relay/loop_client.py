"""Spawn runner.loop and parse buffered trailer."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from .config import RelayConfig
from .turn import resolve_workspace_id, workspace_env

LOOP_TRAILER_MARKER = "---agents-loop-trailer---"


@dataclass
class LoopTurnResult:
    reply: str
    session: str
    user_id: str
    alias: str
    returncode: int
    stderr: str


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


_TIMEOUT_REPLY = "Turn stopped before a final answer."


def _stopped(stderr: str) -> LoopTurnResult:
    return LoopTurnResult(
        reply=_TIMEOUT_REPLY,
        session="",
        user_id="",
        alias="",
        returncode=1,
        stderr=stderr,
    )


def run_loop_turn(
    *,
    channel: str,
    user: str,
    message: str,
    config: RelayConfig | None = None,
    session: str = "",
    user_id: str = "",
    new_session: bool = False,
    timeout_sec: int = 0,
    on_status: Any = None,
    attachments: list[str] | None = None,
    on_pid: Callable[[int], None] | None = None,
    provider: str = "",
    model: str = "",
    persona: str = "",
    project: str = "",
    workspace: str = "",
    workdir: str = "",
) -> LoopTurnResult:
    cfg = config or RelayConfig.from_env()
    argv = list(cfg.loop_cmd)
    argv.extend(
        [
            "--channel",
            channel,
            "--user",
            user,
            "--message",
            message,
            "--complete",
            "--provider",
            provider or cfg.loop_provider,
            "--deliver",
            "buffered",
        ]
    )
    if model:
        argv.extend(["--model", model])
    if session:
        argv.extend(["--session", session])
    if user_id:
        argv.extend(["--user-id", user_id])
    if new_session:
        argv.append("--new-session")
    if persona:
        argv.extend(["--persona", persona])
    if project:
        argv.extend(["--project", project])
    for path in attachments or ():
        if path:
            argv.extend(["--attach", str(path)])

    wait = timeout_sec if timeout_sec and timeout_sec > 0 else None

    ws = None
    if workspace:
        try:
            ws = resolve_workspace_id(workspace)
        except Exception:
            ws = None
    container = False
    child_env = workspace_env(workspace, workdir)
    if ws is not None:
        try:
            from agents_workspace.runtime import (
                isolation_of, to_container_argv, to_container_env,
            )
            if isolation_of(ws) == "container":
                child_env = to_container_env(ws, child_env)
                argv = to_container_argv(ws, argv)
                container = True
        except ImportError:
            pass

    def _run_blocking() -> LoopTurnResult:
        if container:
            from agents_workspace.runtime import run as ws_run

            assert ws is not None
            proc = ws_run(ws, argv, child_env, capture=True, timeout=wait or 0)
        else:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=wait,
                shell=False,
                env=child_env,
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

    if on_status is None and on_pid is None:
        try:
            return _run_blocking()
        except subprocess.TimeoutExpired as exc:
            err = exc.stderr or ""
            if isinstance(err, bytes):
                err = err.decode("utf-8", errors="replace")
            return _stopped(err)
        except RuntimeError as exc:
            return LoopTurnResult(
                reply=f"Turn failed: {exc}", session="", user_id="",
                alias="", returncode=1, stderr=str(exc),
            )
    import threading

    popen_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        # Own process group so /stop can kill the loop and its tool children
        # without signaling the relay.
        popen_kwargs["start_new_session"] = True
    if container:
        from agents_workspace.runtime import spawn as ws_spawn

        assert ws is not None
        proc = ws_spawn(ws, argv, child_env, pipes=True)
    else:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=child_env,
            **popen_kwargs,
        )
    if on_pid is not None and proc.pid:
        try:
            on_pid(int(proc.pid))
        except Exception:
            pass
    stderr_lines: list[str] = []

    def _read_err() -> None:
        if proc.stderr:
            for line in proc.stderr:
                stderr_lines.append(line)
                s = line.strip()
                if s and on_status:
                    try:
                        on_status(s)
                    except Exception:
                        pass

    stdout_lines: list[str] = []

    def _read_out() -> None:
        if proc.stdout:
            stdout_lines.append(proc.stdout.read())

    out_t = threading.Thread(target=_read_out, daemon=True)
    out_t.start()
    t = threading.Thread(target=_read_err, daemon=True)
    t.start()
    try:
        proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        from .jobs import kill_process_tree

        kill_process_tree(proc.pid)
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
            proc.wait()
        t.join(timeout=1)
        out_t.join(timeout=1)
        return _stopped("".join(stderr_lines))
    finally:
        t.join(timeout=2)
        out_t.join(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    stdout = "".join(stdout_lines)

    parsed = parse_loop_stdout(stdout or "")
    return LoopTurnResult(
        reply=parsed["reply"],
        session=parsed["session"],
        user_id=parsed["user_id"],
        alias=parsed["alias"],
        returncode=int(proc.returncode),
        stderr="".join(stderr_lines),
    )
