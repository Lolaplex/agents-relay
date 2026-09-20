"""Spawn runner.loop and parse buffered trailer (compat shim)."""

from __future__ import annotations

from .turn import (
    LOOP_TRAILER_MARKER,
    LoopTurnResult,
    TurnParams,
    build_loop_argv,
    iter_loop_stream,
    parse_loop_stdout,
    run_loop_turn,
    turn_params_from_body,
)

__all__ = [
    "LOOP_TRAILER_MARKER",
    "LoopTurnResult",
    "TurnParams",
    "StreamEvent",
    "build_loop_argv",
    "iter_loop_stream",
    "parse_loop_stdout",
    "run_loop_turn",
    "turn_params_from_body",
]

from .turn import StreamEvent  # noqa: E402
