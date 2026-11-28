"""Discord Markdown splitting helpers."""

from __future__ import annotations

DISCORD_MESSAGE_LIMIT = 2000


def _fence_match(line: str) -> tuple[int, str, str] | None:
    stripped = line.strip()
    if not stripped.startswith("```"):
        return None
    ticks = len(stripped) - len(stripped.lstrip("`"))
    if ticks < 3:
        return None
    return ticks, stripped[ticks:].strip(), stripped


def _fence_state(text: str) -> bool:
    in_fence = False
    opening_ticks = 0
    for line in text.splitlines():
        match = _fence_match(line)
        if match is None:
            continue
        ticks, info, _ = match
        if not in_fence:
            in_fence = True
            opening_ticks = ticks
        elif not info and ticks >= opening_ticks:
            in_fence = False
            opening_ticks = 0
    return in_fence


def split_discord_markdown(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split Markdown at line boundaries, closing and reopening code fences.

    Lines longer than the limit are hard-split; shorter lines are kept intact
    whenever they can fit in one message. Discord accepts Markdown directly,
    so the input text is not transformed. The only inserted text is fence
    close/open pairs needed to keep every chunk renderable on its own.
    """
    body = str(text or "")
    if not body:
        return []
    if limit < 32:
        raise ValueError("limit must be at least 32")
    if len(body) <= limit:
        return [body]

    parts: list[str] = []
    buf = ""
    in_fence = False
    fence_header = "```"
    opening_ticks = 3

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        out = buf
        if in_fence:
            close = "`" * opening_ticks
            out += "\n" + close if not out.endswith("\n") else close
        parts.append(out)
        buf = (fence_header + "\n") if in_fence else ""

    for line in body.splitlines(keepends=True):
        match = _fence_match(line)
        opens = False
        closes = False
        ticks = opening_ticks
        header = fence_header
        if match is not None:
            ticks, info, _ = match
            if not in_fence:
                opens = True
                language = info.split(None, 1)[0] if info else ""
                header = (("`" * ticks) + language)[:24] or ("`" * ticks)
            elif not info and ticks >= opening_ticks:
                closes = True
        after_fence = (in_fence or opens) and not closes
        after_ticks = ticks if opens else opening_ticks

        def commit_fence_state() -> None:
            nonlocal in_fence, opening_ticks, fence_header
            in_fence = after_fence
            if opens:
                opening_ticks = ticks
                fence_header = header
            elif closes:
                opening_ticks = 3

        reserve = after_ticks + 1 if after_fence else 0
        fresh_prefix = len(fence_header) + 1 if in_fence else 0
        if len(buf) + len(line) + reserve <= limit:
            buf += line
            commit_fence_state()
        elif len(line) + fresh_prefix + reserve <= limit:
            flush()
            buf += line
            commit_fence_state()
        else:
            pos = 0
            while pos < len(line):
                capacity = limit - len(buf) - reserve
                if capacity <= 0:
                    flush()
                    continue
                take = min(capacity, len(line) - pos)
                buf += line[pos : pos + take]
                pos += take
                commit_fence_state()
                if pos < len(line):
                    flush()
    if buf:
        flush()
    return [part for part in parts if part]


def fences_balanced(text: str) -> bool:
    return not _fence_state(text)
