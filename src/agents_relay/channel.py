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
KINDS = frozenset({"hello", "presence", "op", "rpc-req", "rpc-res", "ack",
                   "resync", "ws:announce", "ws:revoke",
                   "approval-req", "approval-res"})
REPLAY_LIMIT = 2000
RPC_TIMEOUT_SEC = 30.0
RPC_STREAM_TIMEOUT_SEC = 600.0


def envelope(kind: str, payload: dict[str, Any], *, ch: str = "", seq: int = 0,
             msg_id: str = "") -> dict[str, Any]:
    return {"v": ENVELOPE_V, "ch": ch, "seq": seq, "kind": kind,
            "id": msg_id, "ts": time.time(), "payload": dict(payload)}


# Ephemeral kinds never enter the replay ring (agents-relay#42): a
# reconnecting device must not re-execute rpc requests; rpc-res is
# upstream-only, resync is a marker. approval-* DO replay — both sides
# apply them idempotently by approval id, and a device offline at
# decision time must still learn the terminal state on reconnect or its
# wait_for_decision blocks until timeout (klanker#148).
_REPLAYED_KINDS = frozenset({"hello", "presence", "op", "ack",
                             "ws:announce", "ws:revoke",
                             "approval-req", "approval-res"})

# Presence frame bounds (agents-relay#45): a device must not stuff
# unbounded caps/status blobs into the hub's presence table.
_PRESENCE_MAX_CAPS = 32
_PRESENCE_MAX_FIELD = 64


def _device_workspaces(device_id: str) -> list[str]:
    """Workspaces that admit ``device_id`` — the SSE fan-out scope for
    device lifecycle events (agents-relay#45). Union of
    ``ws.allows_device`` rows and the device's own allowlist; every
    lookup failure fails closed for that workspace only."""
    scopes: set[str] = set()
    try:
        from .workspaces import list_workspaces

        for ws in list_workspaces():
            try:
                if ws.allows_device(device_id):
                    scopes.add(str(ws.id))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001 - workspace layer optional
        pass
    try:
        from .devices import load_devices

        meta = load_devices().get(device_id) or {}
        scopes.update(str(w) for w in (meta.get("allowlist") or ())
                      if str(w or ""))
    except Exception:  # noqa: BLE001 - registry read never blocks events
        pass
    return sorted(scopes)


def _emit_device_event(kind: str, device_id: str,
                       payload: dict[str, Any]) -> None:
    """Emit one device event per admitted workspace so ws-scoped SSE
    subscribers (``?workspace=w1``) actually see presence churn; falls
    back to a single global emit when no workspace admits the device
    (agents-relay#45)."""
    try:
        from .events import emit_event

        scopes = _device_workspaces(device_id) or [""]
        for ws in scopes:
            emit_event(kind, payload, workspace=ws)
    except Exception:  # noqa: BLE001 - events never block transport
        pass


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

    def push(self, msg: dict[str, Any], lock: threading.Lock) -> None:
        """Seq + replay + enqueue under the hub lock (agents-relay#42).

        Only replayed kinds consume ``chan.seq``: an ephemeral rpc-req
        that bumped the cursor would poison the ``?since=`` resume
        check (a client cursor behind it looks "ahead" of every ring
        entry) without ever being replayable (agents-relay#44)."""
        with lock:
            if msg.get("kind") in _REPLAYED_KINDS:
                self.chan.seq += 1
                msg = dict(msg, seq=self.chan.seq)
                self.chan.replay.append((self.chan.seq, msg))
            else:
                msg = dict(msg, seq=self.chan.seq)
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
        self._pending: dict[tuple[str, str], "queue.Queue[dict[str, Any]]"] = {}
        self._gen: dict[str, int] = {}
        self._op_handler = None
        self._ack_handler = None
        self._ws_handler = None
        self._connect_handler = None
        self._approval_handler = None
        self._presence: dict[str, dict[str, Any]] = {}

    # -- link lifecycle -------------------------------------------------

    @staticmethod
    def _poison(q: "queue.Queue") -> None:
        """Enqueue the close sentinel even when the queue is full — drop
        the oldest item rather than leak a stream (agents-relay#42)."""
        try:
            q.put_nowait(None)
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

    def connect(self, device_id: str,
                since: int = 0) -> tuple[DeviceLink, list[dict], bool]:
        """Attach a new SSE stream; kick the previous generation.

        Returns (link, replay_msgs, needs_resync) computed inside the
        lock BEFORE the link is visible to publishers — a frame can be
        replayed or queued, never both (agents-relay#42). The seq space
        + replay ring persist across generations.
        """
        with self._lock:
            gen = self._gen.get(device_id, 0) + 1
            self._gen[device_id] = gen
            old = self._links.get(device_id)
            if old is not None:
                self._poison(old.queue)  # drop the stale stream
            chan = self._channels.setdefault(device_id, _Channel())
            link = DeviceLink(device_id=device_id, chan=chan, generation=gen)
            oldest = (chan.replay[0][0] - 1) if chan.replay else chan.seq
            if since > 0 and (since < oldest or since > chan.seq):
                replay: list[dict] = []
                resync = True
            elif since:
                replay = [m for s, m in chan.replay if s > since]
                resync = False
            else:
                replay = []
                resync = False
            self._links[device_id] = link
        return link, replay, resync

    def link_for(self, device_id: str, since: int = 0) -> DeviceLink:
        """Back-compat attach for callers that only want the link —
        tests and non-SSE producers (agents-relay#42)."""
        return self.connect(device_id, since=since)[0]

    def disconnect(self, device_id: str, generation: int = 0) -> None:
        with self._lock:
            link = self._links.get(device_id)
            if link is None:
                return
            if generation and link.generation != generation:
                return  # a newer stream already replaced us
            del self._links[device_id]
        self._poison(link.queue)  # release the SSE generator
        _emit_device_event("device.disconnected", device_id,
                           {"device_id": device_id})

    # -- presence ----------------------------------------------------------

    def note_presence(self, device_id: str, payload: dict[str, Any]) -> None:
        """Inbound ``presence`` frame: last_seen + status/caps, rate-
        bounded — a chatty device cannot flood the events bus
        (agents-relay#43). Field sizes are bounded and the emit decision
        is taken under ``_lock`` (agents-relay#45)."""
        now = time.time()
        status = str(payload.get("status") or "online")[:_PRESENCE_MAX_FIELD]
        raw_caps = payload.get("caps")
        caps = ([str(c)[:_PRESENCE_MAX_FIELD]
                 for c in list(raw_caps)[:_PRESENCE_MAX_CAPS]]
                if isinstance(raw_caps, (list, tuple)) else [])
        emit = False
        with self._lock:
            prev = self._presence.get(device_id) or {}
            # one presence event per device per second max
            emit = now - float(prev.get("_emitted") or 0.0) >= 1.0
            self._presence[device_id] = {
                "ts": now,
                "_emitted": now if emit else prev.get("_emitted") or 0.0,
                "status": status,
                "caps": caps}
        if emit:
            _emit_device_event("device.presence", device_id, {
                "device_id": device_id,
                "status": status,
                "ts": now})

    def drop_presence(self, device_id: str) -> None:
        """Revocation cleanup — a dead device must not keep a presence
        row forever (agents-relay#45)."""
        with self._lock:
            self._presence.pop(device_id, None)

    def presence(self, device_id: str) -> dict[str, Any]:
        with self._lock:
            row = dict(self._presence.get(device_id) or {})
        row.pop("_emitted", None)
        row["connected"] = self.is_connected(device_id)
        return row

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
        link.push(msg, self._lock)
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
        with self._lock:
            return [m for s, m in link.replay if s > since]

    def oldest_seq(self, link: DeviceLink) -> int:
        with self._lock:
            return link.replay[0][0] - 1 if link.replay else link.seq

    # -- upstream ---------------------------------------------------------

    def set_op_handler(self, fn) -> None:
        """fn(device_id, op_payload) replaces default fan-out for upstream ops."""
        self._op_handler = fn

    def set_ack_handler(self, fn) -> None:
        """fn(device_id, ack_payload) receives upstream op-acks."""
        self._ack_handler = fn

    def set_ws_handler(self, fn) -> None:
        """fn(device_id, kind, payload) receives ws-share control frames
        (``ws:announce``/``ws:revoke``, klanker#145)."""
        self._ws_handler = fn

    def set_connect_handler(self, fn) -> None:
        """fn(device_id) fires when a device (re)connects its stream —
        the delivery opportunity for frames that dropped while offline
        (ws:announce is fire-once-per-member-set, klanker#145)."""
        self._connect_handler = fn

    def set_approval_handler(self, fn) -> None:
        """fn(device_id, kind, payload) receives upstream approval-req
        mirrors (klanker#148); approval-res frames are forwarded to the
        device named in ``payload["device"]`` — the hub just routes."""
        self._approval_handler = fn

    def handle_upstream(self, device_id: str, msgs: list[dict[str, Any]]) -> dict[str, Any]:
        """Device POSTed envelopes: route rpc-res to pending calls, ack ops,
        forward ops to other devices (allowlist-filtered upstream)."""
        acked = 0
        malformed = 0
        for msg in msgs:
            if not isinstance(msg, dict) or msg.get("kind") not in KINDS:
                malformed += 1
                continue
            kind = msg["kind"]
            payload = msg.get("payload")
            if not isinstance(payload, dict):
                malformed += 1  # never crash a batch on one bad frame
                continue
            if kind == "rpc-res":
                # pending is keyed (target_device, rpc_id): a device can
                # only answer calls addressed to it (agents-relay#42).
                q = self._pending.get(
                    (device_id, str(msg.get("id") or "")))
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
            elif kind in ("ws:announce", "ws:revoke"):
                if not isinstance(payload.get("ws"), str) or \
                        not str(payload.get("ws") or "").strip():
                    malformed += 1
                    continue
                # SSE only when the ws handler's ACL admits the frame —
                # an unprivileged device must not spoof ws.announced/
                # ws.revoked events (agents-relay#45).
                accepted = bool(
                    self._ws_handler is not None
                    and self._ws_handler(device_id, kind, payload))
                if accepted:
                    try:
                        from .workspaces import invalidate_workspace_read_cache

                        invalidate_workspace_read_cache()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        from .events import emit_event

                        emit_event(
                            "ws.announced" if kind == "ws:announce"
                            else "ws.revoked",
                            {"device_id": device_id,
                             "ws": str(payload.get("ws") or "")},
                            workspace=str(payload.get("ws") or ""))
                    except Exception:  # noqa: BLE001
                        pass
            elif kind in ("approval-req", "approval-res"):
                if not isinstance(payload.get("id"), str) or \
                        not str(payload.get("id") or "").strip():
                    malformed += 1
                    continue
                if kind == "approval-res":
                    # Forgery guard (agents-relay#44): only a client
                    # device the approval's workspace actually allows may
                    # push a terminal decision — anything else is a
                    # self-approval/cross-ws spoof and is dropped.
                    if not _approval_res_allowed(device_id, payload):
                        malformed += 1
                        continue
                else:
                    # The authenticated sender is authoritative — a
                    # caller can never mirror an ask under another
                    # device id (agents-relay#44).
                    payload["__device"] = device_id
                # handler owns semantics: req mirrors pending state, res
                # applies the mirror decision + forwards to payload.device
                if self._approval_handler is not None:
                    self._approval_handler(device_id, kind, payload)
            elif kind == "presence":
                self.note_presence(device_id, payload)
        return {"acked": acked, "malformed": malformed}

    # -- rpc plane --------------------------------------------------------

    def rpc_call(self, device_id: str, method: str, params: dict[str, Any],
                 *, timeout_sec: float = RPC_TIMEOUT_SEC) -> Iterator[dict[str, Any]]:
        """Publish rpc-req and yield rpc-res frames until done/error/timeout."""
        rpc_id = f"rpc-{secrets.token_hex(8)}"
        q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=512)
        with self._lock:
            self._pending[(device_id, rpc_id)] = q
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
            with self._lock:
                self._pending.pop((device_id, rpc_id), None)


def _approval_res_allowed(device_id: str, payload: dict[str, Any]) -> bool:
    """Terminal-decision gate for upstream ``approval-res`` frames.

    All four must hold (agents-relay#44): the caller is a paired
    ``role="client"`` device, the approval id is well-formed, the status
    is a real terminal decision, and the frame's workspace actually
    allows the caller (``ws.allows_device``). Anything else is a forged
    self-approval or a cross-workspace spoof — refuse it.
    """
    from .approvals import APPROVAL_ID_RE
    from .devices import load_devices
    from .workspaces import get_workspace

    aid = str(payload.get("id") or "")
    status = str(payload.get("status") or "")
    if not APPROVAL_ID_RE.match(aid) or status not in ("approved", "denied"):
        return False
    meta = load_devices().get(device_id) or {}
    if str(meta.get("role") or "") != "client":
        return False
    ws_id = str(payload.get("ws") or payload.get("workspace") or "")
    try:
        ws = get_workspace(ws_id)
    except (KeyError, ValueError):
        return False
    try:
        return bool(ws.allows_device(device_id))
    except Exception:  # noqa: BLE001 - policy evaluation fails closed
        return False


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
    link, replay, resync = mgr.connect(device_id, since=since)
    # TOCTOU: a device revoked between auth and link registration must
    # not end up streaming (agents-relay#42). REVOCATION_LOCK serializes
    # the (registry check + remote auto-register) block against
    # DELETE /v1/hub/devices/<id> so revocation can never resurrect a
    # dev-<id> remotes.json row (agents-relay#44).
    from .devices import REVOCATION_LOCK

    with REVOCATION_LOCK:
        try:
            from .devices import load_devices

            if device_id not in load_devices():
                mgr.disconnect(device_id, generation=link.generation)
                handler.send_error(401, "unauthorized")
                return
        except Exception:  # noqa: BLE001 - lookup failure must not open stream
            mgr.disconnect(device_id, generation=link.generation)
            handler.send_error(503, "device registry unavailable")
            return
        try:
            from .devices import list_public, touch_device

            touch_device(device_id)
            # Auto-register a channel remote row so /v1/remote/dev-<id>
            # routes over this channel without manual UI wiring.
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
        # replay snapshot was taken atomically at connect (agents-relay#42)
        if resync:
            handler.wfile.write(_fmt_sse(envelope("resync", {"since": since})))
            handler.wfile.flush()
        elif replay:
            for msg in replay:
                handler.wfile.write(_fmt_sse(msg))
            handler.wfile.flush()
        handler.wfile.write(_fmt_sse(envelope(
            "hello", {"device_id": device_id, "generation": link.generation,
                      "seq": link.seq})))
        handler.wfile.flush()
        _emit_device_event("device.connected", device_id,
                           {"device_id": device_id})
        try:
            fn = getattr(mgr, "_connect_handler", None)
            if fn is not None:
                fn(device_id)
        except Exception:  # noqa: BLE001
            log.exception("channel connect handler failed")
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

