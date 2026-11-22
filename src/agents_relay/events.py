"""In-memory Server-Sent Events pub/sub bus for live cross-client updates."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Any

log = logging.getLogger("agents_relay.events")


@dataclass
class Subscriber:
    queue: queue.Queue[dict[str, Any] | None]
    workspace: str = ""


class EventBus:
    def __init__(self, *, max_queue: int = 256) -> None:
        self._lock = threading.Lock()
        self._max_queue = max_queue
        self._seq = 0
        self._closed = False
        self._subs: list[Subscriber] = []

    def reset(self) -> None:
        with self._lock:
            self._closed = False

    def subscribe(self, workspace: str = "") -> Subscriber:
        sub = Subscriber(queue=queue.Queue(maxsize=self._max_queue), workspace=workspace.strip())
        with self._lock:
            if self._closed:
                sub.queue.put_nowait(None)
                return sub
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    def emit(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        workspace: str = "",
    ) -> dict[str, Any]:
        ws_clean = workspace.strip()
        with self._lock:
            self._seq += 1
            evt: dict[str, Any] = {
                "id": self._seq,
                "kind": kind,
                "workspace": ws_clean,
                "ts": time.time(),
                "payload": dict(payload or {}),
            }
            if self._closed:
                return evt
            subs = list(self._subs)
        for sub in subs:
            if sub.workspace and ws_clean and sub.workspace != ws_clean:
                continue
            try:
                sub.queue.put_nowait(evt)
            except queue.Full:
                try:
                    sub.queue.get_nowait()
                    log.warning(
                        "sse subscriber queue full; dropped oldest event (id=%s kind=%s ws=%s)",
                        evt["id"],
                        kind,
                        ws_clean,
                    )
                except queue.Empty:
                    pass
                try:
                    sub.queue.put_nowait(evt)
                except queue.Full:
                    pass
        return evt

    def shutdown_all(self) -> None:
        with self._lock:
            self._closed = True
            subs = list(self._subs)
            self._subs.clear()
        for sub in subs:
            try:
                sub.queue.put_nowait(None)
            except queue.Full:
                try:
                    sub.queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    sub.queue.put_nowait(None)
                except queue.Full:
                    pass


_BUS = EventBus()


def subscribe(workspace: str = "") -> Subscriber:
    return _BUS.subscribe(workspace=workspace)


def unsubscribe(sub: Subscriber) -> None:
    _BUS.unsubscribe(sub)


def emit_event(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    workspace: str = "",
) -> dict[str, Any]:
    return _BUS.emit(kind, payload, workspace=workspace)


def shutdown_all() -> None:
    _BUS.shutdown_all()


def reset_bus() -> None:
    _BUS.reset()


def format_sse_event(evt: dict[str, Any]) -> bytes:
    eid = evt.get("id", 0)
    kind = str(evt.get("kind") or "message").replace("\n", " ").replace("\r", " ")
    data = json.dumps(evt, ensure_ascii=False)
    return f"id: {eid}\nevent: {kind}\ndata: {data}\n\n".encode("utf-8")


def handle_events_sse(
    handler: BaseHTTPRequestHandler,
    *,
    workspace: str = "",
    heartbeat_sec: float = 25.0,
) -> None:
    sub = subscribe(workspace=workspace)
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        handler.wfile.write(b": connected\n\n")
        handler.wfile.flush()
        while True:
            try:
                item = sub.queue.get(timeout=heartbeat_sec)
            except queue.Empty:
                handler.wfile.write(b": ping\n\n")
                handler.wfile.flush()
                continue
            if item is None:
                break
            handler.wfile.write(format_sse_event(item))
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        unsubscribe(sub)
