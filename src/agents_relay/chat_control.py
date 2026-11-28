"""Shared chat control-command parsing for conversational adapters."""

from __future__ import annotations

from .jobs import Job

CONTROL_COMMANDS = {"jobs", "stop", "persona", "workspace", "model"}


def parse_control_command(text: str) -> tuple[str, str] | None:
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None
    head, _, rest = raw.partition(" ")
    name = head[1:]
    if "@" in name:
        name = name.split("@", 1)[0]
    name = name.lower()
    if name not in CONTROL_COMMANDS:
        return None
    return name, rest.strip()


def jobs_text(jobs: list[Job]) -> str:
    if not jobs:
        return "No running jobs."
    lines = [f"Jobs ({len(jobs)}):"]
    for job in jobs:
        state = "queued" if not job.running else f"pid {job.pid or '?'}"
        preview = job.preview or ""
        lines.append(f"• {job.id} {state} — {preview}")
    return "\n".join(lines)
