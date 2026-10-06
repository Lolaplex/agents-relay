"""Telegram approval gate. Decision files live under the relay state dir.

`agents-relay approve` writes `$AGENTS_RELAY_STATE/approvals/<id>.json` and
blocks. The serve poll loop records the inline-keyboard decision into that
file. Both processes must share the state dir (same host). No HTTP endpoint.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from .config import RelayConfig
from .state import approval_dir, exclusive_lock, read_json, write_json_atomic

log = logging.getLogger("agents_relay.approvals")

# UUID is 36 chars. Callback data is `ap:<id>:1` and must stay <= 64 bytes.
APPROVAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_PENDING = "pending"
_APPROVED = "approved"
_DENIED = "denied"
_TIMEOUT = "timeout"


def approval_path(config: RelayConfig, ident: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", ident)
    return approval_dir(config) / f"{safe}.json"


def parse_callback_data(data: str) -> tuple[str, str] | None:
    """Return `(id, 'approved'|'denied')` for `ap:<id>:1` / `ap:<id>:0`."""
    raw = (data or "").strip()
    if not raw.startswith("ap:"):
        return None
    if raw.endswith(":1"):
        decision = _APPROVED
        ident = raw[3:-2]
    elif raw.endswith(":0"):
        decision = _DENIED
        ident = raw[3:-2]
    else:
        return None
    if not APPROVAL_ID_RE.match(ident):
        return None
    return ident, decision


def approval_keyboard(ident: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "Approve", "callback_data": f"ap:{ident}:1"},
            {"text": "Deny", "callback_data": f"ap:{ident}:0"},
        ]]
    }


def format_approval_html(request: dict) -> str:
    tool = html.escape(str(request.get("tool") or "?"))
    summary = html.escape(str(request.get("summary") or "")).strip()
    ident = html.escape(str(request.get("id") or ""))
    session = html.escape(str(request.get("session") or ""))
    lines = ["<b>Approval needed</b>", f"Tool: <code>{tool}</code>"]
    if summary:
        lines.append(summary)
    if session:
        lines.append(f"Session: <code>{session}</code>")
    args = request.get("args")
    if isinstance(args, dict) and args:
        blob = json.dumps(args, ensure_ascii=False, default=str)
        if len(blob) > 400:
            blob = blob[:400] + "…"
        lines.append(f"<pre>{html.escape(blob)}</pre>")
    lines.append(f"ID: <code>{ident}</code>")
    return "\n".join(lines)


def _mutate(config: RelayConfig, ident: str, fn) -> dict | None:
    path = approval_path(config, ident)
    with exclusive_lock(path.with_suffix(".lock")):
        current = read_json(path) or {}
        updated = fn(current)
        if updated is None:
            return current or None
        write_json_atomic(path, updated)
        return updated


def create_pending(config: RelayConfig, chat_id: int, request: dict) -> dict:
    ident = str(request.get("id") or "")

    def _apply(current: dict) -> dict | None:
        status = str(current.get("status") or "")
        if status in (_APPROVED, _DENIED) and current.get("id") == ident:
            return None
        return {
            "id": ident,
            "status": _PENDING,
            "chat_id": int(chat_id),
            "request": request,
            "note": "",
            "decided_by": None,
            "created_at": time.time(),
            "decided_at": None,
        }

    updated = _mutate(config, ident, _apply)
    return updated or {}


def record_decision(
    config: RelayConfig,
    ident: str,
    *,
    status: str,
    note: str,
    decided_by: int | None,
    expected_chat_id: int | None = None,
) -> dict | None:
    """Write a terminal status if the record is still pending. None if rejected."""
    changed = False

    def _apply(current: dict) -> dict | None:
        nonlocal changed
        if not current or current.get("status") != _PENDING:
            return None
        if current.get("id") != ident:
            return None
        if expected_chat_id is not None and int(current.get("chat_id") or 0) != int(expected_chat_id):
            return None
        current["status"] = status
        current["note"] = note
        current["decided_by"] = decided_by
        current["decided_at"] = time.time()
        changed = True
        return current

    path = approval_path(config, ident)
    if not path.is_file():
        return None
    updated = _mutate(config, ident, _apply)
    if not changed:
        return None
    return updated


def read_approval(config: RelayConfig, ident: str) -> dict | None:
    if not APPROVAL_ID_RE.match(ident):
        return None
    return read_json(approval_path(config, ident))


def wait_for_decision(
    config: RelayConfig,
    ident: str,
    timeout: float,
    *,
    poll_interval: float = 0.2,
) -> tuple[str, str]:
    """Block until approved/denied, or mark timeout. Returns `(status, note)`."""
    deadline = time.monotonic() + max(0.0, timeout)
    interval = poll_interval if poll_interval and poll_interval > 0 else 0.05
    path = approval_path(config, ident)
    while True:
        data = read_json(path) or {}
        status = str(data.get("status") or "")
        if status in (_APPROVED, _DENIED):
            return status, str(data.get("note") or status)
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)

    def _apply(current: dict) -> dict | None:
        status = str(current.get("status") or "")
        if status in (_APPROVED, _DENIED):
            return None
        if not current:
            current = {"id": ident, "chat_id": None, "request": {}}
        current["status"] = _TIMEOUT
        current["note"] = "timeout"
        current["decided_at"] = time.time()
        return current

    try:
        updated = _mutate(config, ident, _apply) or {}
    except TimeoutError:
        updated = read_json(path) or {}
    status = str(updated.get("status") or _TIMEOUT)
    if status in (_APPROVED, _DENIED):
        return status, str(updated.get("note") or status)
    return _TIMEOUT, "timeout"


def status_exit_code(status: str) -> int:
    if status == _APPROVED:
        return 0
    if status == _DENIED:
        return 1
    return 2
