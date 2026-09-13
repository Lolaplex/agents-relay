"""Telegram HTML formatting (stdlib only)."""

from __future__ import annotations

import html
import json
import re

TG_LIMIT = 4096
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^\*\n]+?)\*(?!\*)")
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)")
_TABLE_SEP_CELL_RE = re.compile(r"^:?-+:?$")
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


def _is_separator_row(row_str: str) -> bool:
    cells = [c.strip() for c in row_str.strip().strip("|").split("|")]
    if not cells:
        return False
    return all(_TABLE_SEP_CELL_RE.match(c) for c in cells if c)


def _split_table_row(row_str: str) -> list[str]:
    return [c.strip() for c in row_str.strip().strip("|").split("|")]


def _format_table_block(headers: list[str], rows: list[list[str]]) -> list[str]:
    formatted: list[str] = []
    num_cols = len(headers)
    for row in rows:
        if len(row) < num_cols:
            row.extend([""] * (num_cols - len(row)))
        if not any(row):
            continue
        c1 = row[0]
        if num_cols == 1:
            formatted.append(f"• {c1}")
        elif num_cols == 2:
            c2 = row[1]
            formatted.append(f"• **{c1}** — {c2}" if c2 else f"• **{c1}**")
        elif num_cols == 3:
            c2 = row[1]
            c3 = row[2]
            if c2 and c3:
                formatted.append(f"• **{c1}** (*{c2}*) — {c3}")
            elif c3:
                formatted.append(f"• **{c1}** — {c3}")
            elif c2:
                formatted.append(f"• **{c1}** (*{c2}*)")
            else:
                formatted.append(f"• **{c1}**")
        else:
            extra_parts = [f"*{h}*: {v}" for h, v in zip(headers[1:], row[1:]) if v]
            extra_str = " · ".join(extra_parts)
            formatted.append(f"• **{c1}**: {extra_str}" if extra_str else f"• **{c1}**")
    return formatted


def _transform_markdown_tables(text: str) -> str:
    if "|" not in text:
        return text
    lines = text.splitlines()
    out_lines: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if (
            "|" in line
            and i + 1 < n
            and "|" in lines[i + 1]
            and _is_separator_row(lines[i + 1])
        ):
            headers = _split_table_row(line)
            i += 2
            table_rows: list[list[str]] = []
            while i < n and "|" in lines[i] and not _is_separator_row(lines[i]):
                row_cells = _split_table_row(lines[i])
                if any(row_cells):
                    table_rows.append(row_cells)
                i += 1
            transformed = _format_table_block(headers, table_rows)
            out_lines.extend(transformed)
        else:
            out_lines.append(line)
            i += 1
    return "\n".join(out_lines)


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
    reply = visible_reply(cleaned_body, all_traces)
    reply = _transform_markdown_tables(reply)
    body = _light_md_html(reply)
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
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _ITALIC_STAR_RE.sub(r"<i>\1</i>", escaped)
    escaped = _LINK_RE.sub(r'<a href="\2">\1</a>', escaped)
    return escaped

