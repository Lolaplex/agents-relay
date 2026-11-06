"""In-memory per-chat job registry for the Telegram serve process.

PIDs belong to live `runner.loop` children. The registry is not written to
disk and is not stored under ~/.agents. Shutdown kills each process tree.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from typing import Callable

log = logging.getLogger("agents_relay.jobs")

_REGISTRY: "JobRegistry | None" = None
_REG_LOCK = threading.Lock()


def kill_process_tree(pid: int) -> None:
    """Kill `pid` and its children. Never signals the relay's own process group."""
    if pid is None or int(pid) <= 0:
        return
    pid = int(pid)
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    if pgid == os.getpgrp():
        _signal_pid(pid)
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _signal_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(0.3)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class Job:
    def __init__(self, job_id: str, chat_id: int, preview: str) -> None:
        self.id = job_id
        self.chat_id = chat_id
        self.preview = preview
        self.pid: int | None = None
        self.running = False
        self.created = time.time()
        self.cancel = threading.Event()
        self.done = threading.Event()

    def bind_pid(self, pid: int) -> None:
        self.pid = int(pid)
        if self.cancel.is_set() and self.pid > 0:
            kill_process_tree(self.pid)


class JobRegistry:
    def __init__(self, max_per_chat: int = 3, max_global: int = 8, max_queue: int = 20) -> None:
        self.max_per_chat = max(1, int(max_per_chat))
        self.max_global = max(1, int(max_global))
        self.max_queue = max(1, int(max_queue))
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._queues: dict[int, deque[tuple[Job, Callable[[Job], None]]]] = {}
        self._running = 0
        self._rr = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def list_chat(self, chat_id: int) -> list[Job]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.chat_id == chat_id]
        jobs.sort(key=lambda j: (j.created, j.id))
        return jobs

    def submit(self, chat_id: int, preview: str, fn: Callable[[Job], None]) -> Job | None:
        """Start `fn(job)` or queue it. None when the per-chat queue is full or closed."""
        with self._lock:
            if self._closed:
                return None
            job = Job(self._new_id_locked(), chat_id, (preview or "")[:80])
            self._jobs[job.id] = job
            if self._can_start_locked(chat_id):
                self._mark_running_locked(job)
                start_now = True
            else:
                queue = self._queues.setdefault(chat_id, deque())
                if len(queue) >= self.max_queue:
                    self._jobs.pop(job.id, None)
                    return None
                queue.append((job, fn))
                start_now = False
        if start_now:
            self._start(job, fn)
        return job

    def stop(self, chat_id: int, job_id: str | None = None) -> list[str]:
        """Cancel one job or every job for `chat_id`. Kills live process trees."""
        to_kill: list[int] = []
        stopped: list[str] = []
        with self._lock:
            queue = self._queues.get(chat_id)
            if queue is not None:
                kept: deque[tuple[Job, Callable[[Job], None]]] = deque()
                for job, fn in queue:
                    if job_id is None or job.id == job_id:
                        job.cancel.set()
                        self._jobs.pop(job.id, None)
                        job.done.set()
                        stopped.append(job.id)
                    else:
                        kept.append((job, fn))
                if kept:
                    self._queues[chat_id] = kept
                else:
                    self._queues.pop(chat_id, None)
            for job in list(self._jobs.values()):
                if job.chat_id != chat_id:
                    continue
                if job_id is not None and job.id != job_id:
                    continue
                if job.id in stopped:
                    continue
                job.cancel.set()
                stopped.append(job.id)
                if job.pid:
                    to_kill.append(job.pid)
        for pid in to_kill:
            kill_process_tree(pid)
        return stopped

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            chat_ids = {job.chat_id for job in self._jobs.values()}
        for chat_id in chat_ids:
            self.stop(chat_id, None)

    def finish(self, job: Job) -> None:
        nxt: tuple[Job, Callable[[Job], None]] | None = None
        with self._lock:
            if job.running:
                job.running = False
                self._running = max(0, self._running - 1)
            self._jobs.pop(job.id, None)
            nxt = self._pop_next_locked()
        job.done.set()
        if nxt is not None:
            self._start(nxt[0], nxt[1])

    def _start(self, job: Job, fn: Callable[[Job], None]) -> None:
        def wrap() -> None:
            try:
                if not job.cancel.is_set():
                    fn(job)
            except Exception:
                log.exception("job %s failed", job.id)
            finally:
                self.finish(job)

        threading.Thread(target=wrap, name=f"relay-job-{job.id}", daemon=True).start()

    def _new_id_locked(self) -> str:
        for _ in range(6):
            ident = uuid.uuid4().hex[:6]
            if ident not in self._jobs:
                return ident
        return uuid.uuid4().hex[:8]

    def _can_start_locked(self, chat_id: int) -> bool:
        running_chat = sum(1 for job in self._jobs.values() if job.chat_id == chat_id and job.running)
        return running_chat < self.max_per_chat and self._running < self.max_global

    def _mark_running_locked(self, job: Job) -> None:
        job.running = True
        self._running += 1

    def _pop_next_locked(self) -> tuple[Job, Callable[[Job], None]] | None:
        if self._closed or not self._queues:
            return None
        chats = [cid for cid, queue in self._queues.items() if queue]
        if not chats:
            return None
        for offset in range(len(chats)):
            idx = (self._rr + offset) % len(chats)
            chat_id = chats[idx]
            queue = self._queues.get(chat_id)
            if not queue or not self._can_start_locked(chat_id):
                continue
            while queue:
                job, fn = queue.popleft()
                if not queue:
                    self._queues.pop(chat_id, None)
                if job.cancel.is_set():
                    self._jobs.pop(job.id, None)
                    job.done.set()
                    continue
                self._rr = (idx + 1) % max(len(chats), 1)
                self._mark_running_locked(job)
                return job, fn
        return None


def get_registry(config) -> JobRegistry:
    global _REGISTRY
    with _REG_LOCK:
        if _REGISTRY is None:
            _REGISTRY = JobRegistry(
                max_per_chat=getattr(config, "max_jobs_per_chat", 3),
                max_global=getattr(config, "max_jobs", 8),
            )
        return _REGISTRY


def reset_registry(config=None) -> JobRegistry:
    """Test helper. Closes the previous registry so it cannot start more work."""
    global _REGISTRY
    with _REG_LOCK:
        if _REGISTRY is not None:
            _REGISTRY._closed = True
        per = getattr(config, "max_jobs_per_chat", 3) if config is not None else 3
        glob = getattr(config, "max_jobs", 8) if config is not None else 8
        _REGISTRY = JobRegistry(max_per_chat=per, max_global=glob)
        return _REGISTRY


def shutdown_jobs() -> None:
    with _REG_LOCK:
        registry = _REGISTRY
    if registry is not None:
        registry.shutdown()
