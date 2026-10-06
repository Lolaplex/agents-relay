"""Telegram HTML formatting (stdlib only)."""

from __future__ import annotations

import html
import json
import os
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
_JOB_LABELS = {
    "mcp.calendar.list": "Calendar",
    "mcp.calendar.add": "Calendar",
    "mcp.calendar.update": "Calendar",
    "mcp.calendar.delete": "Calendar",
    "mcp.calendar.calendars": "Calendar",
    "mcp.memory.search": "Memory",
    "mcp.memory.add": "Memory",
    "mcp.docs.search": "Docs",
    "mcp.docs.write": "Docs",
    "mcp.terminal": "Terminal",
    "mcp.schedule.add": "Reminder",
    "mcp.schedule.list": "Reminders",
    "mcp.schedule.remove": "Reminder",
    "mcp.browser": "Browser",
    "list_catalog": "Catalog",
    "load_schema": "Schema",
    "call_job": "Job",
}
DEFAULT_WAIT_TEXT = "One moment …"
_JOB_RE = re.compile(r"'([^']+)'")
_RUNNING_RE = re.compile(r"^running\s+(.+?)(?:\.{3}|\u2026)$", re.I)
_OK_RE = re.compile(r"^\[\+\]\s+'([^']+)'\s+OK\b", re.I)
_FAIL_RE = re.compile(
    r"^\[\-\]\s+'([^']+)'\s+(FAILED|TIMEOUT|ERROR)(?::\s*(.*))?$",
    re.I,
)
_EXIT_RE = re.compile(r"got\s+(\d+)", re.I)
_MACHINE_ONLY_RE = re.compile(r"^[\s\-_=*~.]{3,}$")
_MARK_RE = re.compile("[\u2705\u2713\u2714\u2717\u2718\u274c]")


def wait_text() -> str:
    """Placeholder while a turn runs. ``AGENTS_RELAY_WAIT_TEXT`` overrides the English default."""
    return os.environ.get("AGENTS_RELAY_WAIT_TEXT", "").strip() or DEFAULT_WAIT_TEXT


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


def _job_label(job: str) -> str:
    name = (job or "").strip().strip("'\"")
    if name in _JOB_LABELS:
        return _JOB_LABELS[name]
    if name.startswith("mcp.") and "." in name:
        return name.rsplit(".", 1)[-1]
    return name or "Step"


def _job_from_running(payload: str) -> str:
    raw = (payload or "").strip().strip("'\"").rstrip(".").rstrip("\u2026").strip()
    if raw.startswith("call_job"):
        inner = raw[len("call_job") :].strip()
        inner = inner[1:-1] if inner.startswith("(") and inner.endswith(")") else inner
        inner = inner.strip().strip("'\"")
        if inner:
            return inner.split()[0]
    return raw.split()[0] if raw else ""


def humanize_status(text: str) -> str:
    """Turn executor/loop stderr into a short person-mid-work line. Empty = skip."""
    s = (text or "").strip()
    if not s:
        return ""
    if s.startswith("CMD:") or s.startswith("STDERR:"):
        return ""
    if s in ("thinking...", "tools"):
        return wait_text()
    fail = _FAIL_RE.match(s)
    if fail:
        job, kind, detail = fail.group(1), fail.group(2).upper(), fail.group(3) or ""
        lab = _job_label(job)
        extra = ""
        if kind == "FAILED":
            got = _EXIT_RE.search(detail)
            extra = f" (exit {got.group(1)})" if got else ""
        elif kind == "TIMEOUT":
            extra = " (timeout)"
        elif detail.strip():
            extra = f" ({detail.strip()[:80]})"
        return f"{lab} failed{extra}."
    ok = _OK_RE.match(s)
    if ok:
        job = ok.group(1)
        return f"{_job_label(job)} done."
    if s.startswith("[*] Running"):
        m = _JOB_RE.search(s)
        job = m.group(1) if m else ""
        lab = _job_label(job)
        return f"{lab} ({job}) …" if job and lab != job else f"{lab} …"
    run = _RUNNING_RE.match(s)
    if run:
        job = _job_from_running(run.group(1))
        lab = _job_label(job)
        return f"{lab} ({job}) …" if job and lab != job else f"{lab} …"
    return re.sub(r"[*_`]", "", s)


def _is_trace_line(s: str) -> bool:
    if (
        s.startswith("[*] Running")
        or s.startswith("[+]")
        or s.startswith("[-]")
        or s.startswith("CMD:")
        or s.startswith("STDERR:")
        or s in ("thinking...", "tools")
    ):
        return True
    if s.lower().startswith("running ") and s.endswith("..."):
        return True
    return False


def _is_failure_trace(s: str) -> bool:
    return s.startswith("[-]") or " FAILED" in s or s.endswith("TIMEOUT") or " ERROR:" in s


def extract_traces(text: str) -> tuple[str, tuple[str, ...]]:
    """Separate Cordis/runner loop trace lines from the user reply."""
    traces: list[str] = []
    clean_lines: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if _is_trace_line(s):
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
    return _scrub_model_glue(out)


def _scrub_model_glue(text: str) -> str:
    kept: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            kept.append("")
            continue
        if _MACHINE_ONLY_RE.match(s) or s.startswith("CMD:"):
            continue
        kept.append(_MARK_RE.sub("", line).rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def fallback_from_traces(traces: tuple[str, ...]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for raw in traces:
        h = humanize_status(str(raw))
        if not h or h in seen:
            continue
        seen.add(h)
        lines.append(h)
    if not lines:
        return "No visible reply."
    return "\n".join(lines[-8:])


def visible_reply(text: str, traces: tuple[str, ...] = ()) -> str:
    cleaned = strip_model_dumps(text).strip()
    if cleaned:
        return cleaned
    if traces:
        return fallback_from_traces(traces)
    return "(empty reply)"


def status_html(text: str) -> str:
    plain = humanize_status(text) or wait_text()
    plain = re.sub(r"[*_`]", "", plain).strip() or wait_text()
    return f"<i>{html.escape(plain)}</i>"


def format_telegram_html(answer: str, traces: tuple[str, ...] = ()) -> str:
    cleaned_body, extracted_traces = extract_traces(answer)
    all_traces = traces if traces else extracted_traces
    had_body = bool(strip_model_dumps(cleaned_body).strip())
    reply = visible_reply(cleaned_body, all_traces)
    if not reply.strip():
        reply = fallback_from_traces(all_traces) if all_traces else "(empty reply)"
    reply = _transform_markdown_tables(reply)
    body = _light_md_html(reply)
    extra = _failure_notes(all_traces) if not had_body else ""
    return body + extra


def _failure_notes(traces: tuple[str, ...]) -> str:
    notes: list[str] = []
    seen: set[str] = set()
    for raw in traces:
        if not _is_failure_trace(str(raw)):
            continue
        h = humanize_status(str(raw))
        if not h or h in seen:
            continue
        seen.add(h)
        notes.append(html.escape(h))
    if not notes:
        return ""
    return "\n\n<i>" + "\n".join(notes) + "</i>"


def _light_md_html(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _ITALIC_STAR_RE.sub(r"<i>\1</i>", escaped)
    escaped = _LINK_RE.sub(r'<a href="\2">\1</a>', escaped)
    return escaped


_HTML_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*>")


def utf16_len(text: str) -> int:
    """Telegram counts message length in UTF-16 code units."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _close_tags(stack: list[tuple[str, str]]) -> str:
    return "".join(f"</{name}>" for name, _full in reversed(stack))


def _open_tags(stack: list[tuple[str, str]]) -> str:
    return "".join(full for _name, full in stack)


def _apply_tag(stack: list[tuple[str, str]], full: str, name: str) -> list[tuple[str, str]]:
    updated = list(stack)
    if full.startswith("</"):
        for idx in range(len(updated) - 1, -1, -1):
            if updated[idx][0].lower() == name.lower():
                updated.pop(idx)
                break
        return updated
    if full.endswith("/>"):
        return updated
    updated.append((name, full))
    return updated


def _stack_at(raw: str, start: int, end: int, initial: list[tuple[str, str]]) -> list[tuple[str, str]]:
    stack = list(initial)
    index = start
    while index < end:
        if raw[index] == "<":
            match = _HTML_TAG_RE.match(raw, index)
            if match and match.end() <= end:
                full = raw[index : match.end()]
                stack = _apply_tag(stack, full, match.group(1))
                index = match.end()
                continue
        index += 1
    return stack


def _inside_tag(raw: str, start: int, cut: int) -> bool:
    last_lt = raw.rfind("<", start, cut)
    if last_lt == -1:
        return False
    last_gt = raw.rfind(">", start, cut)
    return last_lt > last_gt


def _rewind_entity(raw: str, start: int, cut: int) -> int:
    if cut <= start:
        return cut
    amp = raw.rfind("&", max(start, cut - 16), cut)
    if amp == -1:
        return cut
    semi = raw.find(";", amp, amp + 16)
    if semi == -1 or not (amp < cut <= semi):
        return cut
    if _inside_tag(raw, start, amp):
        return cut
    if amp > start:
        return amp
    return min(len(raw), semi + 1)


def _fit_chunk(
    raw: str,
    pos: int,
    limit: int,
    carry: list[tuple[str, str]],
) -> tuple[int, list[tuple[str, str]]]:
    """Furthest tag-safe cut at or before `limit`, preferring paragraph breaks."""
    prefix_len = utf16_len(_open_tags(carry))
    stack = list(carry)
    close_len = utf16_len(_close_tags(stack))
    running = 0
    best_end = pos
    best_pri = 0
    last_end = pos
    index = pos
    limit_n = len(raw)

    def fits(extra: int, close: int) -> bool:
        return prefix_len + running + extra + close <= limit

    while index < limit_n:
        if raw[index] == "<":
            match = _HTML_TAG_RE.match(raw, index)
            if match:
                full = raw[index : match.end()]
                new_stack = _apply_tag(stack, full, match.group(1))
                new_close = utf16_len(_close_tags(new_stack))
                extra = utf16_len(full)
                if not fits(extra, new_close):
                    break
                running += extra
                stack = new_stack
                close_len = new_close
                index = match.end()
                last_end = index
                continue
        step = 2 if ord(raw[index]) > 0xFFFF else 1
        if not fits(step, close_len):
            break
        running += step
        index += 1
        last_end = index
        ch = raw[index - 1]
        pri = 0
        if ch == "\n" and index >= 2 and raw[index - 2] == "\n":
            pri = 3
        elif ch == "\n":
            pri = 2
        elif ch.isspace():
            pri = 1
        if pri > 0 and pri >= best_pri:
            best_pri = pri
            best_end = index

    if best_pri <= 0:
        best_end = last_end
    best_end = _rewind_entity(raw, pos, best_end)
    if best_end <= pos:
        return pos, list(carry)
    return best_end, _stack_at(raw, pos, best_end, carry)


def split_telegram_html(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split Telegram HTML into valid chunks of at most `limit` UTF-16 units.

    Breaks on paragraph, then line, then whitespace. Tags still open at a cut
    are closed on that chunk and reopened on the next one.
    """
    raw = text or ""
    if not raw:
        return []
    if utf16_len(raw) <= limit:
        return [raw]

    chunks: list[str] = []
    pos = 0
    carry: list[tuple[str, str]] = []
    total = len(raw)
    guard = 0
    while pos < total:
        guard += 1
        if guard > total + 8:
            chunks.append(_open_tags(carry) + raw[pos:] + _close_tags(_stack_at(raw, pos, total, carry)))
            break
        end, carry_after = _fit_chunk(raw, pos, limit, carry)
        if end <= pos:
            match = _HTML_TAG_RE.match(raw, pos)
            end = match.end() if match else min(total, pos + 1)
            carry_after = _stack_at(raw, pos, end, carry)
        piece = _open_tags(carry) + raw[pos:end] + _close_tags(carry_after)
        if piece:
            chunks.append(piece)
        carry = carry_after
        pos = end
    return chunks

