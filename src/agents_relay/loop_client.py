"""Spawn runner.loop and parse buffered trailer."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from .config import RelayConfig
from .turn import TurnParams, _container_rewrite, build_loop_argv, prepare_loop, turn_approval_env

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
    interrupt_file: str = "",
    extra_env: dict[str, str] | None = None,
) -> LoopTurnResult:
    cfg = config or RelayConfig.from_env()
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
        attachments=tuple(attachments or ()),
    )
    params, notes, extra = prepare_loop(params, cfg)
    extra = dict(extra or {})
    if extra_env:
        extra.update({k: str(v) for k, v in extra_env.items()})
    if interrupt_file:
        extra["AGENTS_INTERRUPT_FILE"] = interrupt_file
    argv = build_loop_argv(params, cfg)
    wait = timeout_sec if timeout_sec and timeout_sec > 0 else None
    ttl_sec = max(float(wait or 0), 300.0) + 60.0 if wait else 3600.0

    with turn_approval_env(params.workspace, config=cfg, ttl_sec=ttl_sec) as approval_extra:
        extra.update(approval_extra)
        argv, child_env, ws = _container_rewrite(argv, params.workspace, params.workdir, extra)
        container = ws is not None

        from .attachments import apply_notes

        def _finish(result: LoopTurnResult) -> LoopTurnResult:
            result.reply = apply_notes(result.reply, notes)
            return result

        def _run_blocking() -> LoopTurnResult:
            if container:
                from agents_workspace.runtime import run as ws_run

                assert ws is not None
                proc = ws_run(ws, argv, child_env, capture=True, timeout=wait or 0)
            else:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    encoding="utf-8", errors="replace",
                    timeout=wait,
                    shell=False,
                    env=child_env,
                )
            parsed = parse_loop_stdout(proc.stdout or "")
            return _finish(LoopTurnResult(
                reply=parsed["reply"],
                session=parsed["session"],
                user_id=parsed["user_id"],
                alias=parsed["alias"],
                returncode=int(proc.returncode),
                stderr=proc.stderr or "",
            ))

        if on_status is None and on_pid is None:
            try:
                return _run_blocking()
            except subprocess.TimeoutExpired as exc:
                err = exc.stderr or ""
                if isinstance(err, bytes):
                    err = err.decode("utf-8", errors="replace")
                return _finish(_stopped(err))
            except RuntimeError as exc:
                return _finish(LoopTurnResult(
                    reply=f"Turn failed: {exc}", session="", user_id="",
                    alias="", returncode=1, stderr=str(exc),
                ))
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
                encoding="utf-8", errors="replace",
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
            return _finish(_stopped("".join(stderr_lines)))
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
        return _finish(LoopTurnResult(
            reply=parsed["reply"],
            session=parsed["session"],
            user_id=parsed["user_id"],
            alias=parsed["alias"],
            returncode=int(proc.returncode),
            stderr="".join(stderr_lines),
        ))
