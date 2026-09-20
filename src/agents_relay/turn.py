"""Turn request model and loop subprocess helpers."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from typing import Any, Iterator

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
    persona: str = ""
    project: str = ""
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
        persona=str(body.get("persona") or ""),
        project=str(body.get("project") or ""),
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
    persona: str = "",
    project: str = "",
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
        persona=persona,
        project=project,
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
