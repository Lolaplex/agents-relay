"""Hub-side channel manager (agents-relay#40).

One connected device = one channel = one seq space. Downstream is a
per-device SSE stream (`GET /v1/hub/stream`), upstream is batched
envelopes (`POST /v1/hub/send`). The hub assigns a monotonic ``seq`` to
every outbound envelope, keeps a replay ring per device for ``?since=``
resume, and multiplexes ``op`` frames, ``rpc-req``/``rpc-res`` frames,
``presence`` and control markers in a single envelope space.

v0 scope: transport + presence + RPC plane. ``op`` payloads are opaque
blobs (op-log schema is the agents-store/agents-sync track); they are
broadcast to every other connected device — allowlist filtering lands
with the op layer (devices.allowlist field is stored, not yet enforced).
"""
from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any, Iterator

log = logging.getLogger(__name__)

ENVELOPE_V = 1
KINDS = frozenset({"hello", "presence", "op", "rpc-req", "rpc-res", "ack", "resync"})
REPLAY_LIMIT = 2000
RPC_TIMEOUT_SEC = 30.0
RPC_STREAM_TIMEOUT_SEC = 600.0


def envelope(kind: str, payload: dict[str, Any], *, ch: str = "", seq: int = 0,
             msg_id: str = "") -> dict[str, Any]:
    return {"v": ENVELOPE_V, "ch": ch, "seq": seq, "kind": kind,
            "id": msg_id, "ts": time.time(), "payload": dict(payload)}


@dataclass
class _Channel:
    """Persistent per-device seq space + replay ring — survives reconnects."""
    seq: int = 0
    replay: "deque[tuple[int, dict[str, Any]]]" = field(
        default_factory=lambda: deque(maxlen=REPLAY_LIMIT))


@dataclass
class DeviceLink:
    device_id: str
    chan: _Channel
    queue: "queue.Queue[dict[str, Any] | None]" = field(
        default_factory=lambda: queue.Queue(maxsize=512))
    connected_at: float = field(default_factory=time.time)
    generation: int = 0

    def push(self, msg: dict[str, Any]) -> None:
        self.chan.seq += 1
        msg = dict(msg, seq=self.chan.seq)
        self.chan.replay.append((self.chan.seq, msg))
        try:
            self.queue.put_nowait(msg)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(msg)
            except queue.Full:
                pass

    @property
    def seq(self) -> int:
        return self.chan.seq

    @property
    def replay(self) -> "deque[tuple[int, dict[str, Any]]]":
        return self.chan.replay


class ChannelHub:
    """Presence table + per-device out-queues + pending RPC calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._links: dict[str, DeviceLink] = {}
        self._channels: dict[str, _Channel] = {}
        self._pending: dict[str, "queue.Queue[dict[str, Any]]"] = {}
        self._gen: dict[str, int] = {}
        self._op_handler = None
        self._ack_handler = None

    # -- link lifecycle -------------------------------------------------

    def connect(self, device_id: str) -> DeviceLink:
        """Attach a new SSE stream for a device; kicks the previous one.
        The seq space + replay ring persist across generations."""
        with self._lock:
            gen = self._gen.get(device_id, 0) + 1
            self._gen[device_id] = gen
            old = self._links.get(device_id)
            if old is not None:
                try:
                    old.queue.put_nowait(None)  # drop the stale stream
                except queue.Full:
                    pass
            chan = self._channels.setdefault(device_id, _Channel())
            link = DeviceLink(device_id=device_id, chan=chan, generation=gen)
            self._links[device_id] = link
        return link

    def disconnect(self, device_id: str, generation: int = 0) -> None:
        with self._lock:
            link = self._links.get(device_id)
            if link is None:
                return
            if generation and link.generation != generation:
                return  # a newer stream already replaced us
            del self._links[device_id]
            try:
                link.queue.put_nowait(None)  # release the SSE generator
            except queue.Full:
                pass

    def link(self, device_id: str) -> DeviceLink | None:
        with self._lock:
            return self._links.get(device_id)

    def is_connected(self, device_id: str) -> bool:
        return self.link(device_id) is not None

    def connected_devices(self) -> list[str]:
        with self._lock:
            return sorted(self._links)

    # -- downstream -------------------------------------------------------

    def publish(self, device_id: str, kind: str, payload: dict[str, Any],
                msg_id: str = "") -> int:
        """Assign seq + queue one envelope. Returns seq or -1 when offline."""
        link = self.link(device_id)
        if link is None:
            return -1
        msg = envelope(kind, payload, ch=device_id, msg_id=msg_id)
        link.push(msg)
        return link.seq

    def broadcast(self, kind: str, payload: dict[str, Any],
                  skip: set[str] | None = None) -> None:
        for did in self.connected_devices():
            if skip and did in skip:
                continue
            self.publish(did, kind, payload)

    def replay_since(self, link: DeviceLink, since: int) -> list[dict[str, Any]]:
        """Envelopes newer than ``since``. Empty list + ``resync`` marker
        upstream when the cursor fell behind the ring."""
        return [m for s, m in link.replay if s > since]

    def oldest_seq(self, link: DeviceLink) -> int:
        return link.replay[0][0] - 1 if link.replay else link.seq

    # -- upstream ---------------------------------------------------------

    def set_op_handler(self, fn) -> None:
        """fn(device_id, op_payload) replaces default fan-out for upstream ops."""
        self._op_handler = fn

    def set_ack_handler(self, fn) -> None:
        """fn(device_id, ack_payload) receives upstream op-acks."""
        self._ack_handler = fn

    def handle_upstream(self, device_id: str, msgs: list[dict[str, Any]]) -> dict[str, Any]:
        """Device POSTed envelopes: route rpc-res to pending calls, ack ops,
        forward ops to other devices (allowlist-filtered upstream)."""
        acked = 0
        for msg in msgs:
            if not isinstance(msg, dict) or msg.get("kind") not in KINDS:
                continue
            kind = msg["kind"]
            payload = msg.get("payload") or {}
            if kind == "rpc-res":
                q = self._pending.get(str(msg.get("id") or ""))
                if q is not None:
                    try:
                        q.put_nowait(payload)
                    except queue.Full:
                        pass
            elif kind == "op":
                acked += 1
                if self._op_handler is not None:
                    self._op_handler(device_id, payload)
                else:
                    self.broadcast("op", payload, skip={device_id})
            elif kind == "ack":
                if self._ack_handler is not None:
                    self._ack_handler(device_id, payload)
            elif kind == "presence":
                pass
        return {"acked": acked}

    # -- rpc plane --------------------------------------------------------

    def rpc_call(self, device_id: str, method: str, params: dict[str, Any],
                 *, timeout_sec: float = RPC_TIMEOUT_SEC) -> Iterator[dict[str, Any]]:
        """Publish rpc-req and yield rpc-res frames until done/error/timeout."""
        rpc_id = f"rpc-{secrets.token_hex(8)}"
        q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=512)
        self._pending[rpc_id] = q
        try:
            if self.publish(device_id, "rpc-req",
                            {"method": method, "params": params},
                            msg_id=rpc_id) < 0:
                yield {"error": "device offline"}
                return
            deadline = time.monotonic() + timeout_sec
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    yield {"error": "rpc timeout"}
                    return
                try:
                    frame = q.get(timeout=min(left, 5.0))
                except queue.Empty:
                    if not self.is_connected(device_id):
                        yield {"error": "device disconnected"}
                        return
                    continue
                yield frame
                if frame.get("done") or frame.get("error"):
                    return
        finally:
            self._pending.pop(rpc_id, None)


_HUB = ChannelHub()


def hub() -> ChannelHub:
    return _HUB


def reset_hub() -> None:
    global _HUB
    _HUB = ChannelHub()


def _fmt_sse(msg: dict[str, Any]) -> bytes:
    seq = msg.get("seq", 0)
    body = json.dumps(msg, ensure_ascii=False)
    return f"id: {seq}\ndata: {body}\n\n".encode("utf-8")


def handle_hub_stream(handler: BaseHTTPRequestHandler, device_id: str,
                      *, since: int = 0, heartbeat_sec: float = 25.0) -> None:
    """Per-device SSE stream: replay `?since=` then live envelopes."""
    mgr = hub()
    link = mgr.connect(device_id)
    try:
        from .devices import list_public, touch_device

        touch_device(device_id)
        # Auto-register a channel remote row so /v1/remote/dev-<id> routes
        # over this channel without manual UI wiring.
        from .remotes import upsert_remote

        label = next(
            (r["label"] for r in list_public() if r["id"] == device_id), "")
        upsert_remote(handler._cfg(), {  # type: ignore[attr-defined]
            "id": f"dev-{device_id}", "via": "channel",
            "device_id": device_id, "label": label or device_id,
        })
    except Exception:  # noqa: BLE001 - registration never blocks the stream
        log.exception("channel remote auto-register failed")
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        handler.wfile.write(b": connected\n\n")
        handler.wfile.flush()
        # replay window: cursor older than the ring -> resync marker first
        if since and since < mgr.oldest_seq(link):
            handler.wfile.write(_fmt_sse(envelope("resync", {"since": since})))
            handler.wfile.flush()
        elif since:
            for msg in mgr.replay_since(link, since):
                handler.wfile.write(_fmt_sse(msg))
            handler.wfile.flush()
        handler.wfile.write(_fmt_sse(envelope(
            "hello", {"device_id": device_id, "generation": link.generation,
                      "seq": link.seq})))
        handler.wfile.flush()
        while True:
            try:
                item = link.queue.get(timeout=heartbeat_sec)
            except queue.Empty:
                handler.wfile.write(b": ping\n\n")
                handler.wfile.flush()
                continue
            if item is None:
                break
            handler.wfile.write(_fmt_sse(item))
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        mgr.disconnect(device_id, generation=link.generation)


def handle_hub_send(handler: BaseHTTPRequestHandler, device_id: str,
                    body: dict[str, Any]) -> dict[str, Any]:
    msgs = body.get("msgs")
    if not isinstance(msgs, list) or len(msgs) > 512:
        return {"ok": False, "error": "msgs must be a list (<=512)"}
    return {"ok": True, **hub().handle_upstream(device_id, msgs)}


def device_id_from_headers(handler: BaseHTTPRequestHandler) -> str:
    return (handler.headers.get("X-Device-Id") or
            handler.headers.get("Device-Id") or "").strip()

