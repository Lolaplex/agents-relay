"""Relay configuration from environment."""

from __future__ import annotations

import os
import warnings

from agents_workspace import resolve_relay_token, resolve_secret
from agents_workspace.compat import read_alias
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


def _allow_anyone() -> bool:
    if _flag(os.environ.get("AGENTS_RELAY_ALLOW_ANYONE", "")):
        return True
    if _flag(os.environ.get("KLANKER_TELEGRAM_OPEN", "")):
        warnings.warn(
            "KLANKER_TELEGRAM_OPEN is deprecated; use AGENTS_RELAY_ALLOW_ANYONE",
            DeprecationWarning,
            stacklevel=3,
        )
        return True
    return False


def _auth_tokens() -> tuple[str, str]:
    """Symmetric ``(relay_secret, web_token)`` resolution.

    Setting only ``RELAY_WEB_TOKEN`` still authorizes Bearer callers and
    setting only ``RELAY_SECRET`` still enables web cookie login; legacy
    ``GATEWAY_SECRET`` covers both with a deprecation warning.
    ``RELAY_NO_AUTH=1`` keeps both empty unless explicitly set in env.
    """
    if _flag(os.environ.get("RELAY_NO_AUTH", "")):
        return (
            _clean(os.environ.get("RELAY_SECRET", "")),
            _clean(os.environ.get("RELAY_WEB_TOKEN", "")),
        )
    # Explicit env always wins over stored vault tokens.
    primary = _clean(os.environ.get("RELAY_SECRET", ""))
    web = _clean(os.environ.get("RELAY_WEB_TOKEN", ""))
    legacy = _clean(read_alias(None, "RELAY_SECRET", "GATEWAY_SECRET"))
    if primary or web or legacy:
        return (primary or web or legacy, web or primary or legacy)
    try:
        stored = _clean(resolve_relay_token())
    except Exception:
        stored = ""
    return (stored, stored)


def _secret(key: str) -> str:
    """ws/host secrets.json > env. Falls back to env alone when the
    workspace resolver cannot run (config load must not depend on it)."""
    try:
        return resolve_secret(key)
    except Exception:
        return os.environ.get(key, "")


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
    vision: str = ""
    web_token: str = ""
    web_user: str = "web"
    static_dir: str = ""
    web_secure: bool = True
    cors_origins: tuple[str, ...] = ()
    groq_api_key: str = ""
    tts: bool = False
    tts_voice: str = ""
    tts_model: str = ""
    relay_url: str = ""

    @classmethod
    def from_env(cls) -> "RelayConfig":
        # Not ``_clean``: it strips quotes from both ends of the whole string,
        # which unbalances a command whose program path is quoted.
        raw_loop = os.environ.get("LOOP_CMD", "python -m runner.loop").strip()
        loop_cmd = tuple(_split_loop_cmd(raw_loop))
        if not loop_cmd:
            loop_cmd = ("python", "-m", "runner.loop")
        relay_secret, web_token = _auth_tokens()
        return cls(
            loop_cmd=loop_cmd,
            loop_provider=_clean(os.environ.get("LOOP_PROVIDER", "openai.default"))
            or "openai.default",
            relay_secret=relay_secret,
            telegram_bot_token=_clean(_secret("TELEGRAM_BOT_TOKEN")),
            telegram_allowed_chat_ids=_int_list(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
            relay_host=_clean(read_alias(None, "RELAY_HOST", "GATEWAY_HOST")) or "127.0.0.1",
            relay_port=_int(read_alias(None, "RELAY_PORT", "GATEWAY_PORT"), 8787),
            telegram_poll_timeout=max(1, min(50, _int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "50"), 50))),
            webhook_secret=_clean(_secret("WEBHOOK_SECRET")),
            board_url=_clean(os.environ.get("BOARD_URL", "")).rstrip("/"),
            board_project=_clean(os.environ.get("BOARD_PROJECT", "")),
            board_key_slug=_clean(os.environ.get("BOARD_KEY_SLUG", "gateway")),
            allow_anyone=_allow_anyone(),
            state_dir=_clean(read_alias(None, "AGENTS_RELAY_DIR", "AGENTS_RELAY_STATE")),
            max_jobs=max(1, _int(os.environ.get("AGENTS_RELAY_MAX_JOBS", "8"), 8)),
            max_jobs_per_chat=max(1, _int(os.environ.get("AGENTS_RELAY_MAX_JOBS_PER_CHAT", "3"), 3)),
            approver=_clean(os.environ.get("AGENTS_RELAY_APPROVER", "")),
            attach_dir=_clean(os.environ.get("AGENTS_RELAY_ATTACH_DIR", "")),
            vision=_clean(os.environ.get("AGENTS_RELAY_VISION", "")).lower(),
            web_token=web_token,
            web_user=_clean(os.environ.get("RELAY_WEB_USER", "web")) or "web",
            static_dir=_clean(read_alias(None, "AGENTS_RELAY_WEB_DIR", "RELAY_STATIC_DIR")),
            web_secure=_flag(os.environ.get("RELAY_WEB_SECURE", "1")),
            cors_origins=tuple(
                origin
                for origin in (
                    _clean(part)
                    for part in os.environ.get("RELAY_CORS_ORIGINS", "").split(",")
                )
                if origin
            ),
            groq_api_key=_clean(_secret("GROQ_API_KEY")),
            tts=_flag(os.environ.get("AGENTS_RELAY_TTS", "")),
            tts_voice=_clean(os.environ.get("AGENTS_RELAY_TTS_VOICE", "")),
            tts_model=_clean(os.environ.get("AGENTS_RELAY_TTS_MODEL", "")),
            relay_url=_clean(os.environ.get("AGENTS_RELAY_URL", "")).rstrip("/"),
        )


def __getattr__(name: str):
    if name == "GatewayConfig":
        import warnings
        warnings.warn(
            "GatewayConfig is deprecated; use RelayConfig (alias removed after one release)",
            DeprecationWarning,
            stacklevel=2,
        )
        return RelayConfig
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
