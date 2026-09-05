"""Telegram HTML formatting (stdlib only)."""

from __future__ import annotations

import html
import json
import re

TG_LIMIT = 4096
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)")
_FENCE_RE = re.compile(
    r"```(?:json|xml|javascript|tool[\w_-]*)?\s*\n[\s\S]*?```",
    re.IGNORECASE,
)
_TOOL_TAG_RE = re.compile(
    r"<(tool_use|tool_call|tool_result|function_call|invoke|ts|clock|timestamp)\b[^>]*>[\s\S]*?</\1>",
    re.IGNORECASE,
)
_BARE_TAGS_RE = re.compile(r"</?(?:ts|clock|timestamp)\b[^>]*>", re.IGNORECASE)
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
    return any(key in low for key in _BLOB_KEYS)


def extract_traces(text: str) -> tuple[str, tuple[str, ...]]:
    """Separate Cordis/runner loop trace lines from the user reply."""
    traces: list[str] = []
    clean_lines: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if (
            s.startswith("[*] Running")
            or s.startswith("[+]")
            or s.startswith("[-]")
            or s.startswith("CMD:")
            or s == "thinking..."
        ):
            traces.append(s)
        elif s.startswith("<ts>") and s.endswith("</ts>"):
            continue
        else:
            clean_lines.append(line)
    return "\n".join(clean_lines).strip(), tuple(traces)


def strip_model_dumps(text: str) -> str:
    if not text:
        return ""
    out = _FENCE_RE.sub(lambda m: "" if _looks_like_blob(m.group(0)) else m.group(0), text)
    out = _TOOL_TAG_RE.sub("", out)
    out = _BARE_TAGS_RE.sub("", out)
    stripped = out.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    ):
        try:
            val = json.loads(stripped)
            if isinstance(val, dict) and any(
                k in val for k in ("name", "tool", "tool_call", "function", "arguments")
            ):
                return ""
            if (
                isinstance(val, list)
                and val
                and isinstance(val[0], dict)
                and any(k in val[0] for k in ("name", "tool", "tool_call", "function", "arguments"))
            ):
                return ""
        except Exception:
            pass
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def visible_reply(text: str, traces: tuple[str, ...] = ()) -> str:
    cleaned = strip_model_dumps(text)
    if cleaned:
        return cleaned
    if traces:
        return "Fertig."
    return "(leere Antwort)"


def status_html(text: str) -> str:
    plain = re.sub(r"[*_`]", "", text or "").strip() or "..."
    return f"<i>{html.escape(plain)}</i>"


def format_telegram_html(answer: str, traces: tuple[str, ...] = ()) -> str:
    cleaned_body, extracted_traces = extract_traces(answer)
    all_traces = traces if traces else extracted_traces
    body = _light_md_html(visible_reply(cleaned_body, all_traces))
    extra = _traces_block(all_traces)
    out = body + extra
    if len(out) <= TG_LIMIT:
        return out
    budget = TG_LIMIT - len(extra) - 3
    if budget < 80:
        return body[: TG_LIMIT - 3] + "..."
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
    escaped = _LINK_RE.sub(r'<a href="\2">\1</a>', escaped)
    return escaped
