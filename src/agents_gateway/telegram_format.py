"""Telegram HTML formatting (stdlib only)."""

from __future__ import annotations

import html
import re

TG_LIMIT = 4096
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_FENCE_RE = re.compile(
    r"```(?:json|xml|javascript|tool[\w_-]*)?\s*\n[\s\S]*?```",
    re.IGNORECASE,
)
_TOOL_TAG_RE = re.compile(
    r"<(tool_use|tool_call|tool_result|function_call|invoke)\b[^>]*>[\s\S]*?</\1>",
    re.IGNORECASE,
)
_BLOB_KEYS = (
    "tool_call",
    "tool_calls",
    "tool_use",
    "function_call",
    "tool_result",
    '"arguments"',
)


def _looks_like_blob(text: str) -> bool:
    low = text.lower()
    return any(key in low for key in _BLOB_KEYS) or len(text) > 400


def strip_model_dumps(text: str) -> str:
    if not text:
        return ""
    out = _FENCE_RE.sub(lambda m: "" if _looks_like_blob(m.group(0)) else m.group(0), text)
    out = _TOOL_TAG_RE.sub("", out)
    stripped = out.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        if _looks_like_blob(stripped):
            return ""
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def visible_reply(text: str, traces: tuple[str, ...] = ()) -> str:
    cleaned = strip_model_dumps(text)
    if cleaned:
        return cleaned
    if traces:
        return "Fertig."
    return (text or "").strip() or "(leere Antwort)"


def status_html(text: str) -> str:
    plain = re.sub(r"[*_`]", "", text or "").strip() or "..."
    return f"<i>{html.escape(plain)}</i>"


def format_telegram_html(answer: str, traces: tuple[str, ...] = ()) -> str:
    body = _light_md_html(visible_reply(answer, traces))
    extra = _traces_block(traces)
    out = body + extra
    if len(out) <= TG_LIMIT:
        return out
    budget = TG_LIMIT - len(extra) - 1
    if budget < 80:
        return body[: TG_LIMIT - 1] + "..."
    return body[:budget] + "..." + extra


def _traces_block(traces: tuple[str, ...]) -> str:
    lines = [html.escape(line) for line in traces if str(line).strip()]
    if not lines:
        return ""
    inner = "\n".join(lines)
    return f"\n\n<blockquote expandable><b>tools</b>\n{inner}</blockquote>"


def _light_md_html(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    return escaped
