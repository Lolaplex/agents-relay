"""Coding jobs: a session with a goal, run to completion in the background.

A job is a SessionRecord carrying ``goal``, ``status`` and ``result``. The
relay launches one buffered turn per job through the normal loop path; the
harness iterates tool calls internally, so one turn IS a full agent run.
Status transitions happen on the session file under the workspace.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("agents_relay.coding_jobs")

_JOB_USER = "jobs"
_VALID_STATUS = ("queued", "running", "done", "failed", "stopped")


def _store(ws):
    from .workspaces import _session_store

    return _session_store(ws)


def list_jobs(ws, persona: str = "", user: str = "") -> list[dict[str, Any]]:
    rows = _store(ws).list(persona=persona, user=user)
    return [r.to_dict() for r in rows if getattr(r, "goal", "")]


def get_job(ws, session_id: str, persona: str = "") -> dict | None:
    rec = _store(ws).get(session_id, persona=persona)
    if rec is None or not getattr(rec, "goal", ""):
        return None
    return rec.to_dict()


def _set_status(ws, persona: str, session_id: str,
                status: str, result: str = "") -> None:
    store = _store(ws)
    rec = store.get(session_id, persona=persona)
    if rec is None:
        return
    rec.status = status
    if result:
        rec.result = result[:4000]
    store.put(rec, persona=persona)


def create_job(
    ws,
    *,
    persona: str,
    goal: str,
    workdir: str = "",
    provider: str = "",
    model: str = "",
    user: str = "",
    run_turn,
    timeout_sec: int = 3600,
) -> dict[str, Any]:
    """Mint a goal-session and run it in a background thread.

    ``run_turn`` is the relay turn callable (``TurnHook`` compatible):
    keyword args matching ``_OPTIONAL_TURN_KWARGS`` plus ``message``.
    Returns the job dict (status=queued).
    """
    import threading

    from .workspaces import create_session

    rec = create_session(ws, persona, user=user or _JOB_USER)
    store = _store(ws)
    s = store.get(rec["id"], persona=persona)
    if s is None:
        raise RuntimeError(f"session {rec['id']} not persisted")
    s.goal = goal
    s.status = "queued"
    s.workspace = ws.id
    if workdir:
        s.workdir = workdir
    store.put(s, persona=persona)

    def _run() -> None:
        try:
            _set_status(ws, persona, s.id, "running")
            res = run_turn(
                message=goal,
                user=user or _JOB_USER,
                channel="jobs",
                session=s.id,
                workspace=ws.id,
                persona=persona,
                provider=provider,
                model=model,
                workdir=workdir,
                timeout_sec=timeout_sec,
            )
            reply = getattr(res, "reply", "") or ""
            rc = getattr(res, "returncode", 1)
            _set_status(
                ws, persona, s.id,
                "done" if rc == 0 else "failed",
                reply or f"exit {rc}",
            )
        except Exception as exc:  # noqa: BLE001 - job must not kill the relay
            log.exception("job %s failed", s.id)
            _set_status(ws, persona, s.id, "failed", str(exc)[:4000])

    threading.Thread(target=_run, daemon=True, name=f"job-{s.id}").start()
    return s.to_dict()
