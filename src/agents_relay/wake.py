"""Active handoff wake: run an inbox task as a detached background turn (#18).

A ``POST .../inbox/<task>/handoff?wake=1`` moves the task to the target
persona (agents-workspace enforces ``accepts_messages``/``allow_peers``
and the hop window), then this module marks it ``running`` and spawns a
daemon turn under ``routine:handoff:<task_id>`` so the user's interactive
stream stays clean. Completion writes ``done``/``failed`` plus a reply
summary back onto the task; tasks asking for a callback (``mode=ask`` or
a ``reply_to`` field) are handed back to the origin persona.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger("agents_relay.wake")

_RECENT_LIMIT = 20

# Live wake threads; lets shutdown/tests join instead of leaking daemons
# that lose their result write (or race a removed state dir) mid-flight.
_THREADS: set[threading.Thread] = set()


def drain(timeout: float = 5.0) -> int:
    """Join live wake threads; returns how many are still running."""
    deadline = time.monotonic() + timeout
    for t in [t for t in _THREADS if t.is_alive()]:
        t.join(max(0.0, deadline - time.monotonic()))
    _THREADS.difference_update(t for t in _THREADS if not t.is_alive())
    return sum(1 for t in _THREADS if t.is_alive())


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_status(
    ws,
    task_id: str,
    status: str,
    *,
    result: str = "",
    reply_summary: str = "",
    note: str = "",
) -> dict[str, Any] | None:
    """In-place task write on the current owner's inbox file."""
    from agents_workspace.inbox import (
        _task_path,
        _write_task,
        find_task,
        persona_inbox,
    )

    hit = find_task(ws, task_id)
    if hit is None:
        return None
    owner, task = hit
    task["status"] = status
    if result:
        task["result"] = result[:4000]
    if reply_summary:
        task["reply_summary"] = reply_summary[:1000]
    task.setdefault("events", []).append(
        {"at": _ts(), "event": f"wake-{status}", "note": note}
    )
    _write_task(_task_path(persona_inbox(ws, owner), task_id), task)
    task["owner"] = owner
    return task


def _prompt(task: dict[str, Any]) -> str:
    src = task.get("from_persona") or "inbox"
    head = f"Handoff task {task.get('id')} from {src}: {task.get('title') or ''}".rstrip()
    note = str(task.get("note") or "").strip()
    return f"{head}\n\n{note}" if note else head


def _finish(ws, task_id: str, status: str, result: str, summary: str) -> None:
    """Persist the outcome; hand the task back when a reply was requested."""
    from agents_workspace.inbox import find_task, reply_task

    hit = find_task(ws, task_id)
    if hit is None:
        return
    _owner, task = hit
    wants_reply = task.get("mode") == "ask" or bool(task.get("reply_to"))
    if wants_reply:
        try:
            reply_task(
                ws,
                task_id,
                result=result[:4000],
                reply_summary=summary[:1000],
                status=status,
                note="wake result",
            )
            return
        except ValueError as exc:
            # Origin refuses or hop limit hit: keep the result where it is.
            log.warning("handoff reply for %s rejected: %s", task_id, exc)
    _write_status(
        ws, task_id, status, result=result, reply_summary=summary,
        note="wake result",
    )


def wake_task(
    ws,
    task: dict[str, Any],
    *,
    run_turn: Callable[..., Any],
    timeout_sec: int = 900,
) -> None:
    """Mark ``task`` running and execute it detached in a daemon thread.

    ``run_turn`` is the relay turn callable (``loop_client.run_loop_turn``
    signature). The child env carries ``AGENTS_PERSONA``/``AGENTS_PERSONA_HOP``
    so the loop knows who it runs as and how many hops are spent; the hop
    window itself is enforced by ``agents_workspace.inbox``.
    """
    task_id = str(task.get("id") or "")
    owner = str(task.get("owner") or task.get("to_persona") or "")
    _write_status(ws, task_id, "running", note="wake spawned")

    def _run() -> None:
        status, result, summary = "failed", "", ""
        try:
            res = run_turn(
                channel="handoff",
                user=str(task.get("user") or task.get("from_persona") or "handoff"),
                message=_prompt(task),
                persona=owner,
                workspace=ws.id,
                session=f"routine:handoff:{task_id}",
                timeout_sec=timeout_sec,
                extra_env={
                    "AGENTS_PERSONA": owner,
                    "AGENTS_PERSONA_HOP": str(int(task.get("hop") or 0)),
                    "AGENTS_THREAD_ID": str(task.get("thread_id") or ""),
                },
            )
            rc = int(getattr(res, "returncode", 1) or 0)
            reply = str(getattr(res, "reply", "") or "").strip()
            status = "done" if rc == 0 else "failed"
            result = reply or f"turn exit {rc}"
            summary = reply
        except Exception as exc:  # noqa: BLE001 - wake must not kill the relay
            log.exception("handoff wake %s failed", task_id)
            result = str(exc)
        try:
            _finish(ws, task_id, status, result, summary)
        except Exception:  # noqa: BLE001 - same rule: log, never propagate
            log.exception("handoff wake %s result write failed", task_id)
        finally:
            _THREADS.discard(threading.current_thread())

    t = threading.Thread(target=_run, daemon=True, name=f"wake-{task_id}")
    _THREADS.add(t)
    t.start()


def team_inbox(ws, *, limit: int = _RECENT_LIMIT) -> dict[str, Any]:
    """Workspace-wide inbox feed: per-persona counts + recent handoffs."""
    from agents_workspace.inbox import list_inbox

    personas: dict[str, dict[str, int]] = {}
    recent: list[dict[str, Any]] = []
    root = ws.personas
    if root.is_dir():
        for pdir in sorted(root.iterdir()):
            if not pdir.is_dir() or pdir.name.startswith("."):
                continue
            tasks = list_inbox(ws, pdir.name)
            if not tasks and not (pdir / "persona.json").is_file():
                continue
            counts = {"open": 0, "running": 0, "done": 0, "failed": 0}
            for task in tasks:
                status = str(task.get("status") or "open")
                counts[status] = counts.get(status, 0) + 1
            counts["unread"] = counts["open"]
            counts["total"] = len(tasks)
            personas[pdir.name] = counts
            for task in tasks:
                for ev in task.get("events") or []:
                    if ev.get("event") in ("handoff", "reply"):
                        recent.append({
                            "at": str(ev.get("at") or ""),
                            "task": task.get("id"),
                            "title": task.get("title") or "",
                            "status": task.get("status") or "",
                            "from": ev.get("from") or "",
                            "to": ev.get("to") or "",
                        })
    recent.sort(key=lambda row: row["at"], reverse=True)
    return {"personas": personas, "recent": recent[: max(1, int(limit))]}
