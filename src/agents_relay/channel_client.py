"""Device-side hub channel client (agents-relay#40).

Redeems a remote pair into device credentials, then runs the channel:
SSE reader (downstream) + batched POST sender (upstream) + reconnect with
``?since=`` resume. ``rpc-req`` frames are dispatched to local methods —
the device pulls work; the hub never calls it unauthenticated.

v0 methods: ``ping``, ``status``, ``turn.stream``.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import RelayConfig

log = logging.getLogger(__name__)

_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 60.0


def _secret_key(hub_id: str) -> str:
    import re

    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", hub_id.strip()).upper()
    return f"HUB_{clean}_TOKEN"


def _hubs_path() -> Path:
    from agents_workspace import Host

    return Host.from_env().clients / "hubs.json"


def _load_hubs() -> dict:
    from .state import read_json

    data = read_json(_hubs_path()) or {}
    return data.get("hubs") if isinstance(data.get("hubs"), dict) else {}


def _store_hubs(rows: dict) -> None:
    from .state import write_json_atomic

    write_json_atomic(_hubs_path(), {"hubs": rows})


def redeem_device(hub_url: str, credential: str, *, label: str = "") -> dict:
    """POST /v1/pair/redeem on the hub -> device credentials (agents-relay#40)."""
    base = hub_url.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ValueError("hub_url must be an http(s) origin")
    req = urllib.request.Request(
        f"{base}/v1/pair/redeem",
        data=json.dumps({"token": credential.strip(), "code": credential.strip()}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "agents-relay/device"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        raise RuntimeError(f"redeem failed: HTTP {e.code} {detail}") from e
    if not body.get("ok") or body.get("mode") != "remote":
        raise RuntimeError(f"redeem refused: {body.get('error') or body}")
    if not body.get("device_id") or not body.get("token"):
        raise RuntimeError("hub returned no device credentials")
    return body


def connect_hub(hub_url: str, credential: str, *, label: str = "") -> dict:
    """Redeem + persist the hub record; returns the stored row."""
    from agents_workspace import write_secret

    res = redeem_device(hub_url, credential, label=label)
    hub_id = str(res.get("host_id") or "").strip() or urllib.parse.urlparse(
        hub_url).hostname or "hub"
    rows = _load_hubs()
    rows[hub_id] = {
        "hub_url": hub_url.strip().rstrip("/"),
        "device_id": res["device_id"],
        "label": label or res.get("host_id") or hub_id,
        "last_seq": 0,
        "connected_at": int(time.time()),
    }
    _store_hubs(rows)
    write_secret(_secret_key(hub_id), res["token"], scope="host")
    return {"hub_id": hub_id, **rows[hub_id]}


def disconnect_hub(hub_id: str) -> bool:
    from agents_workspace import delete_secret

    rows = _load_hubs()
    if hub_id not in rows:
        return False
    del rows[hub_id]
    _store_hubs(rows)
    try:
        delete_secret(_secret_key(hub_id), scope="host")
    except Exception:
        pass
    return True


def hub_token(hub_id: str) -> str:
    from agents_workspace import resolve_secret

    try:
        return str(resolve_secret(_secret_key(hub_id), scope="host") or "")
    except Exception:
        return ""


# --- rpc methods (run on the device) -------------------------------------------


def _rpc_ping(params: dict, cfg: RelayConfig) -> Iterator[dict]:
    yield {"pong": True, "done": True}


def _rpc_status(params: dict, cfg: RelayConfig) -> Iterator[dict]:
    from . import __version__

    yield {
        "ok": True,
        "version": __version__,
        "host": cfg.relay_host,
        "port": cfg.relay_port,
        "provider": cfg.loop_provider,
        "ts": time.time(),
        "done": True,
    }


_STREAM_PATHS = ("/v1/chat/stream", "/v1/runs/stream", "/v1/stream", "/v1/events")

# Target-side denylist for the generic http RPC: this device executes the
# request against its own loopback WITH ITS MASTER SECRET, so admin and
# credential-bearing surfaces are never reachable through the channel —
# regardless of who asked the hub to proxy. Anything under these prefixes
# could mint credentials, manage devices, or rewrite policy.
_RPC_DENY_ALWAYS = (
    "/v1/pair", "/v1/hub", "/v1/remotes", "/v1/discover",
    "/v1/clients", "/v1/secrets", "/v1/vault",
)
_RPC_DENY_WRITE = (
    "/v1/providers", "/v1/settings", "/v1/workspaces",
)


def _rpc_denied(method: str, path: str) -> bool:
    if any(path.startswith(p) for p in _RPC_DENY_ALWAYS):
        return True
    if method not in ("GET", "HEAD") and any(
            path.startswith(p) for p in _RPC_DENY_WRITE):
        return True
    return False


def _rpc_http(params: dict, cfg: RelayConfig) -> Iterator[dict]:
    """Universal upstream proxy: run the request against our own loopback
    relay and stream the response back as rpc frames. The hub never gets a
    credential — the device pulls work, it is never called unauthenticated.
    """
    from .client import _resolve_loopback_secret
    from .remotes import _normalize_proxy_rest

    req = dict(params.get("req") or {})
    try:
        path = _normalize_proxy_rest(str(req.get("path") or "/"))
    except ValueError as exc:
        yield {"error": str(exc)}
        return
    method = str(req.get("method") or "GET").upper()
    if _rpc_denied(method, path):
        yield {"error": f"channel http rpc denied: {method} {path}",
               "status": 403, "done": True}
        return
    data: bytes | None = None
    if req.get("body_b64"):
        import base64

        data = base64.b64decode(str(req["body_b64"]))
    elif isinstance(req.get("body"), str) and req["body"]:
        data = str(req["body"]).encode("utf-8")
    headers = {"User-Agent": "agents-relay/channel-rpc"}
    token = _resolve_loopback_secret()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if req.get("content_type"):
        headers["Content-Type"] = str(req["content_type"])
    if req.get("accept"):
        headers["Accept"] = str(req["accept"])

    url = f"http://127.0.0.1:{cfg.relay_port}{path}"
    hreq = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        resp = urllib.request.urlopen(hreq, timeout=600)
    except urllib.error.HTTPError as e:
        yield {"status": e.code, "content_type": "application/json",
               "body": e.read().decode("utf-8", "replace"), "done": True}
        return
    except (urllib.error.URLError, OSError) as exc:
        yield {"error": f"local relay unreachable: {exc}"}
        return

    ctype = str(resp.headers.get("Content-Type") or "")
    if path.startswith(_STREAM_PATHS):
        yield {"status": resp.status, "content_type": ctype}
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                yield {"chunk": chunk.decode("utf-8", "replace")}
        except OSError as exc:
            yield {"error": str(exc)}
            return
        yield {"done": True}
        return
    raw = resp.read()
    import base64

    yield {"status": resp.status, "content_type": ctype,
           "body_b64": base64.b64encode(raw).decode("ascii"), "done": True}


def _rpc_turn_stream(params: dict, cfg: RelayConfig) -> Iterator[dict]:
    from .turn import TurnParams, iter_loop_stream_live, turn_params_from_body

    body = dict(params)
    tp = turn_params_from_body(body) if isinstance(body, dict) else TurnParams(
        channel="channel", user="hub", message=str(params.get("message") or ""))
    for evt in iter_loop_stream_live(tp, cfg):
        if evt.type == "delta":
            yield {"delta": evt.text}
        elif evt.type == "status":
            yield {"status": evt.text}
        elif evt.type == "trailer":
            yield {"done": True, "session": evt.session,
                   "returncode": evt.returncode}
            return
        elif evt.type == "error":
            yield {"error": evt.text}
            return
    yield {"done": True}


_RPC_METHODS: dict[str, Callable[[dict, RelayConfig], Iterator[dict]]] = {
    "ping": _rpc_ping,
    "status": _rpc_status,
    "http": _rpc_http,
    "turn.stream": _rpc_turn_stream,
}


class ChannelClient:
    """One device -> one hub: SSE reader + batch sender + reconnect loop."""

    def __init__(self, hub_id: str, *, config: RelayConfig | None = None) -> None:
        rows = _load_hubs()
        row = rows.get(hub_id)
        if row is None:
            raise ValueError(f"unknown hub: {hub_id!r}")
        self.hub_id = hub_id
        self.hub_url = row["hub_url"]
        self.device_id = row["device_id"]
        self.last_seq = int(row.get("last_seq") or 0)
        self.cfg = config or RelayConfig.from_env()
        self._outbox: "queue.Queue[dict]" = queue.Queue(maxsize=512)
        self._stop = threading.Event()
        self._seq_dirty = 0
        self.on_op = None    # fn(payload) — wired by the sync layer
        self.on_ack = None   # fn(payload) — wired by the sync layer
        self.on_ws = None    # fn(kind, payload) — ws-share control frames
        self.on_hello = None  # fn(payload) — reconnect catch-up trigger

    def _persist_seq(self, force: bool = False) -> None:
        """Flush the resume cursor so a restart replays only the tail."""
        self._seq_dirty = 0 if force else self._seq_dirty + 1
        if not force and self._seq_dirty < 64:
            return
        self._seq_dirty = 0
        try:
            rows = _load_hubs()
            if self.hub_id in rows:
                rows[self.hub_id]["last_seq"] = self.last_seq
                _store_hubs(rows)
        except Exception:  # noqa: BLE001 - cursor loss only replays the ring
            pass

    # -- wire -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        tok = hub_token(self.hub_id)
        return {
            "Authorization": f"Bearer {tok}",
            "X-Device-Id": self.device_id,
            "User-Agent": "agents-relay/device",
        }

    def _post_send(self, msgs: list[dict]) -> bool:
        req = urllib.request.Request(
            f"{self.hub_url}/v1/hub/send",
            data=json.dumps({"ch": self.device_id, "msgs": msgs}).encode(),
            headers={**self._headers(), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15).read()
            return True
        except (urllib.error.URLError, OSError) as exc:
            log.warning("hub send failed: %s", exc)
            return False

    def send(self, kind: str, payload: dict, msg_id: str = "") -> None:
        try:
            self._outbox.put_nowait(
                {"v": 1, "ch": self.device_id, "kind": kind, "id": msg_id,
                 "ts": time.time(), "payload": payload})
        except queue.Full:
            log.warning("outbox full, dropped %s", kind)

    def _sender(self) -> None:
        while not self._stop.is_set():
            batch: list[dict] = []
            try:
                batch.append(self._outbox.get(timeout=1.0))
            except queue.Empty:
                continue
            while len(batch) < 64:
                try:
                    batch.append(self._outbox.get_nowait())
                except queue.Empty:
                    break
            while batch and not self._post_send(batch):
                if self._stop.wait(2.0):  # keep retrying — messages stay queued
                    return

    # -- rpc dispatch ----------------------------------------------------

    def _dispatch_rpc(self, msg: dict) -> None:
        rpc_id = str(msg.get("id") or "")
        payload = msg.get("payload") or {}
        method = str(payload.get("method") or "")
        params = payload.get("params") or {}
        fn = _RPC_METHODS.get(method)
        if fn is None:
            self.send("rpc-res", {"error": f"unknown method: {method}"}, msg_id=rpc_id)
            return

        def work() -> None:
            try:
                for frame in fn(params, self.cfg):
                    self.send("rpc-res", frame, msg_id=rpc_id)
                    if frame.get("done") or frame.get("error"):
                        return
            except Exception as exc:  # noqa: BLE001 - report, don't die
                self.send("rpc-res", {"error": str(exc)}, msg_id=rpc_id)

        threading.Thread(target=work, daemon=True).start()

    # -- reader loop ------------------------------------------------------

    def _read_stream(self) -> Iterator[dict]:
        url = f"{self.hub_url}/v1/hub/stream?since={self.last_seq}"
        req = urllib.request.Request(url, headers=self._headers())
        # Read timeout > server heartbeat (25s): a half-open TCP would
        # otherwise block read1 forever and look "connected" while dead.
        resp = urllib.request.urlopen(req, timeout=90)
        buf = b""
        while not self._stop.is_set():
            chunk = resp.read1(4096)  # incremental — read() would wait for a full buffer
            if not chunk:
                return
            buf += chunk
            while b"\n\n" in buf:
                block, buf = buf.split(b"\n\n", 1)
                if block.startswith(b":"):
                    continue
                data = b""
                for line in block.split(b"\n"):
                    if line.startswith(b"data: "):
                        data += line[6:]
                if data:
                    try:
                        yield json.loads(data.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue

    def run_forever(self) -> None:
        sender = threading.Thread(target=self._sender, daemon=True)
        sender.start()
        backoff = _RECONNECT_MIN
        while not self._stop.is_set():
            try:
                for msg in self._read_stream():
                    kind = msg.get("kind")
                    seq = int(msg.get("seq") or 0)
                    if seq:
                        self.last_seq = seq
                        self._persist_seq()
                    if kind == "rpc-req":
                        self._dispatch_rpc(msg)
                    elif kind == "hello":
                        if self.on_hello is not None:
                            self.on_hello(msg.get("payload") or {})
                    elif kind == "op":
                        if self.on_op is not None:
                            self.on_op(msg.get("payload") or {})
                    elif kind == "ack":
                        if self.on_ack is not None:
                            self.on_ack(msg.get("payload") or {})
                    elif kind in ("ws:announce", "ws:revoke"):
                        if self.on_ws is not None:
                            self.on_ws(kind, msg.get("payload") or {})
                    elif kind == "resync":
                        self.last_seq = 0  # full resync handled by op-log layer
                    # hello/presence are informational only
                self._persist_seq(force=True)
                backoff = _RECONNECT_MIN
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                log.warning("channel disconnected: %s", exc)
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX)

    def stop(self) -> None:
        self._stop.set()


CLIENT_HOOKS: list = []


def on_client(fn) -> None:
    """fn(client) fires for every booted hub link — klanker wires sync here."""
    CLIENT_HOOKS.append(fn)


def run_hub_channels(config: RelayConfig | None = None) -> list[ChannelClient]:
    """Boot all stored hub links — called by `klanker serve` when hub.enabled."""
    clients = []
    for hub_id in _load_hubs():
        try:
            c = ChannelClient(hub_id, config=config)
            for hook in CLIENT_HOOKS:
                hook(c)
            threading.Thread(target=c.run_forever, daemon=True).start()
            clients.append(c)
            log.info("hub channel started: %s -> %s", hub_id, c.hub_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("hub channel %s failed: %s", hub_id, exc)
    return clients
