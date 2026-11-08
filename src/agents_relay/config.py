"""Relay configuration from environment."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass


def _clean(val: str) -> str:
    return val.strip().strip("'\"").strip()


def _split_loop_cmd(raw: str) -> list[str]:
    """Split ``LOOP_CMD`` the way the platform quotes.

    ``posix=True`` eats the backslashes in a Windows path, and ``posix=False``
    keeps the quotes. Split by platform, then drop the quotes the non-posix
    branch leaves behind, so a quoted program path survives on Windows.
    """
    argv = shlex.split(raw, posix=os.name != "nt")
    if os.name == "nt":
        argv = [
            item[1:-1] if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'" else item
            for item in argv
        ]
    return argv


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


def _flag(val: str) -> bool:
    return _clean(val).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RelayConfig:
    loop_cmd: tuple[str, ...]
    loop_provider: str
    relay_secret: str
    telegram_bot_token: str
    telegram_allowed_chat_ids: tuple[int, ...]
    relay_host: str
    relay_port: int
    telegram_poll_timeout: int
    webhook_secret: str = ""
    board_url: str = ""
    board_project: str = ""
    board_key_slug: str = ""
    allow_anyone: bool = False
    state_dir: str = ""
    max_jobs: int = 8
    max_jobs_per_chat: int = 3
    approver: str = ""
    attach_dir: str = ""

    @classmethod
    def from_env(cls) -> "RelayConfig":
        # Not ``_clean``: it strips quotes from both ends of the whole string,
        # which unbalances a command whose program path is quoted.
        raw_loop = os.environ.get("LOOP_CMD", "python -m runner.loop").strip()
        loop_cmd = tuple(_split_loop_cmd(raw_loop))
        if not loop_cmd:
            loop_cmd = ("python", "-m", "runner.loop")
        return cls(
            loop_cmd=loop_cmd,
            loop_provider=_clean(os.environ.get("LOOP_PROVIDER", "echo")) or "echo",
            relay_secret=_clean(os.environ.get("RELAY_SECRET", os.environ.get("GATEWAY_SECRET", ""))),
            telegram_bot_token=_clean(os.environ.get("TELEGRAM_BOT_TOKEN", "")),
            telegram_allowed_chat_ids=_int_list(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
            relay_host=_clean(os.environ.get("RELAY_HOST", os.environ.get("GATEWAY_HOST", "127.0.0.1"))) or "127.0.0.1",
            relay_port=_int(os.environ.get("RELAY_PORT", os.environ.get("GATEWAY_PORT", "8787")), 8787),
            telegram_poll_timeout=max(1, min(50, _int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "50"), 50))),
            webhook_secret=_clean(os.environ.get("WEBHOOK_SECRET", "")),
            board_url=_clean(os.environ.get("BOARD_URL", "")).rstrip("/"),
            board_project=_clean(os.environ.get("BOARD_PROJECT", "")),
            board_key_slug=_clean(os.environ.get("BOARD_KEY_SLUG", "gateway")),
            allow_anyone=_flag(os.environ.get("AGENTS_RELAY_ALLOW_ANYONE", "")),
            state_dir=_clean(os.environ.get("AGENTS_RELAY_STATE", "")),
            max_jobs=max(1, _int(os.environ.get("AGENTS_RELAY_MAX_JOBS", "8"), 8)),
            max_jobs_per_chat=max(1, _int(os.environ.get("AGENTS_RELAY_MAX_JOBS_PER_CHAT", "3"), 3)),
            approver=_clean(os.environ.get("AGENTS_RELAY_APPROVER", "")),
            attach_dir=_clean(os.environ.get("AGENTS_RELAY_ATTACH_DIR", "")),
        )


GatewayConfig = RelayConfig
