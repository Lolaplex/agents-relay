"""Gateway configuration from environment."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass


def _clean(val: str) -> str:
    return val.strip().strip("'\"").strip()


def _int(val: str, default: int) -> int:
    cleaned = _clean(val)
    if not cleaned:
        return default
    try:
        return int(cleaned)
    except ValueError:
        return default


def _int_list(value: str) -> tuple[int, ...]:
    cleaned = _clean(value)
    if not cleaned:
        return ()
    out: list[int] = []
    for part in cleaned.split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return tuple(out)


@dataclass(frozen=True)
class GatewayConfig:
    loop_cmd: tuple[str, ...]
    loop_provider: str
    gateway_secret: str
    telegram_bot_token: str
    telegram_allowed_chat_ids: tuple[int, ...]
    gateway_host: str
    gateway_port: int
    telegram_poll_timeout: int

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        raw_loop = _clean(os.environ.get("LOOP_CMD", "python -m runner.loop"))
        loop_cmd = tuple(shlex.split(raw_loop, posix=os.name != "nt"))
        if not loop_cmd:
            loop_cmd = ("python", "-m", "runner.loop")
        return cls(
            loop_cmd=loop_cmd,
            loop_provider=_clean(os.environ.get("LOOP_PROVIDER", "echo")) or "echo",
            gateway_secret=_clean(os.environ.get("GATEWAY_SECRET", "")),
            telegram_bot_token=_clean(os.environ.get("TELEGRAM_BOT_TOKEN", "")),
            telegram_allowed_chat_ids=_int_list(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
            gateway_host=_clean(os.environ.get("GATEWAY_HOST", "127.0.0.1")) or "127.0.0.1",
            gateway_port=_int(os.environ.get("GATEWAY_PORT", "8787"), 8787),
            telegram_poll_timeout=max(1, min(50, _int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "50"), 50))),
        )
