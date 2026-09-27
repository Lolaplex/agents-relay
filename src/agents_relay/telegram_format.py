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
_JOB_LABELS = {
    "mcp.calendar.list": "Kalender",
    "mcp.calendar.add": "Kalender",
    "mcp.calendar.update": "Kalender",
    "mcp.calendar.delete": "Kalender",
    "mcp.calendar.calendars": "Kalender",
    "mcp.memory.search": "Memory",
    "mcp.memory.add": "Memory",
    "mcp.docs.search": "Docs",
    "mcp.docs.write": "Docs",
    "mcp.terminal": "Terminal",
    "mcp.schedule.add": "Erinnerung",
    "mcp.schedule.list": "Erinnerungen",
    "mcp.schedule.remove": "Erinnerung",
    "mcp.browser": "Browser",
    "list_catalog": "Katalog",
    "load_schema": "Schema",
    "call_job": "Job",
}
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
    return name or "Schritt"


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
        return "Einen Moment …"
    fail = _FAIL_RE.match(s)
    if fail:
        job, kind, detail = fail.group(1), fail.group(2).upper(), fail.group(3) or ""
        lab = _job_label(job)
        extra = ""
        if kind == "FAILED":
            got = _EXIT_RE.search(detail)
            extra = f" (exit {got.group(1)})" if got else ""
        elif kind == "TIMEOUT":
            extra = " (Timeout)"
        elif detail.strip():
            extra = f" ({detail.strip()[:80]})"
        return f"{lab} fehlgeschlagen{extra}."
    ok = _OK_RE.match(s)
    if ok:
        job = ok.group(1)
        return f"{_job_label(job)} fertig."
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
        return "Keine sichtbare Antwort."
    return "\n".join(lines[-8:])


def visible_reply(text: str, traces: tuple[str, ...] = ()) -> str:
    cleaned = strip_model_dumps(text).strip()
    if cleaned:
        return cleaned
    if traces:
        return fallback_from_traces(traces)
    return "(leere Antwort)"


def status_html(text: str) -> str:
    plain = humanize_status(text) or "Einen Moment …"
    plain = re.sub(r"[*_`]", "", plain).strip() or "Einen Moment …"
    return f"<i>{html.escape(plain)}</i>"


def format_telegram_html(answer: str, traces: tuple[str, ...] = ()) -> str:
    cleaned_body, extracted_traces = extract_traces(answer)
    all_traces = traces if traces else extracted_traces
    had_body = bool(strip_model_dumps(cleaned_body).strip())
    reply = visible_reply(cleaned_body, all_traces)
    if not reply.strip():
        reply = fallback_from_traces(all_traces) if all_traces else "(leere Antwort)"
    reply = _transform_markdown_tables(reply)
    body = _light_md_html(reply)
    extra = _failure_notes(all_traces) if had_body else ""
    out = body + extra
    if len(out) <= TG_LIMIT:
        return out
    if extra and len(body) <= TG_LIMIT:
        return body
    return body[: TG_LIMIT - 3] + "..."


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

