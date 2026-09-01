"""Spawn runner.loop and parse buffered trailer."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from .config import GatewayConfig

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


def run_loop_turn(
    *,
    channel: str,
    user: str,
    message: str,
    config: GatewayConfig | None = None,
    session: str = "",
    user_id: str = "",
    new_session: bool = False,
    timeout_sec: int = 600,
) -> LoopTurnResult:
    cfg = config or GatewayConfig.from_env()
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
            cfg.loop_provider,
            "--deliver",
            "buffered",
        ]
    )
    if session:
        argv.extend(["--session", session])
    if user_id:
        argv.extend(["--user-id", user_id])
    if new_session:
        argv.append("--new-session")

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
