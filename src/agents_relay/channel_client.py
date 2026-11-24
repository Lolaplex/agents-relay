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


def redeem_device(hub_url: str, credential: str, *, label: str = "",
                  bearer: str = "") -> dict:
    """POST /v1/pair/redeem on the hub -> device credentials (agents-relay#40).

    ``bearer`` re-auths an already-paired device (grant reuse); ``label``
    is forwarded so the minted device row is not anonymous
    (agents-relay#44). A reuse response carries ``token == ""`` — the
    existing vault entry stays authoritative (`reused: True`)."""
    base = hub_url.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ValueError("hub_url must be an http(s) origin")
    headers = {"Content-Type": "application/json",
               "User-Agent": "agents-relay/device"}
    if bearer.strip():
        headers["Authorization"] = f"Bearer {bearer.strip()}"
    req = urllib.request.Request(
        f"{base}/v1/pair/redeem",
        data=json.dumps({"token": credential.strip(),
                         "code": credential.strip(),
                         "label": label}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        try:
            body = json.loads(resp.read())
        finally:
            resp.close()
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        e.close()
        raise RuntimeError(f"redeem failed: HTTP {e.code} {detail}") from e
    if not body.get("ok") or body.get("mode") not in ("remote", "ws"):
        raise RuntimeError(f"redeem refused: {body.get('error') or body}")
    if not body.get("device_id"):
        raise RuntimeError("hub returned no device credentials")
    if not body.get("token"):
        # Grant reuse for an already-paired device — no fresh credential
        # is issued (agents-relay#44).
        body["reused"] = True
    return body


def connect_hub(hub_url: str, credential: str, *, label: str = "") -> dict:
    """Redeem + persist the hub record; returns the stored row.

    Reconnecting a hub already linked in ``hubs.json`` re-sends the
    existing Bearer token so the hub extends the grant in place —
    ``token == ""`` reuse keeps the stored device_id/vault entry
    instead of minting a second device (agents-relay#44)."""
    from agents_workspace import write_secret

    base = hub_url.strip().rstrip("/")
    rows = _load_hubs()
    prev_id, prev_row = "", {}
    for hid, row in rows.items():
        if str(row.get("hub_url") or "").rstrip("/") == base:
            prev_id, prev_row = hid, row
            break
    res = redeem_device(base, credential, label=label,
                        bearer=hub_token(prev_id) if prev_id else "")
    hub_id = str(res.get("host_id") or "").strip() or urllib.parse.urlparse(
        base).hostname or "hub"
    if prev_id and prev_id != hub_id:
        rows.pop(prev_id, None)  # migrate the stored link under the new id
    device_id = str(res.get("device_id") or "") or str(
        prev_row.get("device_id") or "")
    rows[hub_id] = {
        "hub_url": base,
        "device_id": device_id,
        "label": label or str(prev_row.get("label") or "")
                 or res.get("host_id") or hub_id,
        "last_seq": int(prev_row.get("last_seq") or 0),
        "connected_at": int(time.time()),
    }
    _store_hubs(rows)
    token = str(res.get("token") or "")
    if token:
        write_secret(_secret_key(hub_id), token, scope="host")
    return {"hub_id": hub_id, "ws": res.get("ws") or "",
            "reused": not token, **rows[hub_id]}


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
# could mint credentials, manage devices, rewrite policy, or pivot.
_RPC_DENY_ALWAYS = (
    "/v1/pair", "/v1/hub", "/v1/remotes", "/v1/remote", "/v1/discover",
    "/v1/clients", "/v1/secrets", "/v1/vault", "/v1/ws/invite",
    "/v1/approvals", "/v1/model", "/v1/auth", "/v1/klanker/routines",
    "/v1/klanker/skills", "/v1/klanker/memory", "/v1/jobs",
    "/v1/send", "/v1/inject", "/v1/favorites", "/v1/push",
    "/v1/a2a", "/v1/webhook", "/v1/alert",
)
_RPC_DENY_WRITE = (
    "/v1/providers", "/v1/settings", "/v1/workspaces",
)


def _rpc_denied(method: str, path: str) -> bool:
    if any(path.startswith(p) for p in _RPC_DENY_ALWAYS):
        return True
    # Provider probes spawn arbitrary manifest commands (ACP RCE) —
    # denied on read verbs too (agents-relay#44).
    if path.startswith("/v1/providers/") and path.endswith("/probe"):
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
    raw_path = str(req.get("path") or "/")
    # Deny+normalize decide on the path only — the query is reattached
    # unchanged so it can never smuggle a denied prefix or be eaten.
    path_only, sep, query = raw_path.partition("?")
    try:
        path = _normalize_proxy_rest(path_only)
    except ValueError as exc:
        yield {"error": str(exc)}
        return
    method = str(req.get("method") or "GET").upper()
    if _rpc_denied(method, path):
        yield {"error": f"channel http rpc denied: {method} {path}",
               "status": 403, "done": True}
        return
    from .http_adapter import MAX_HTTP_BODY_BYTES

    data: bytes | None = None
    if req.get("body_b64"):
        import base64

        try:
            data = base64.b64decode(str(req["body_b64"]), validate=True)
        except ValueError:
            yield {"error": "invalid body_b64", "status": 400, "done": True}
            return
    elif isinstance(req.get("body"), str) and req["body"]:
        data = str(req["body"]).encode("utf-8")
    if data is not None and len(data) > MAX_HTTP_BODY_BYTES:
        yield {"error": "request body too large", "status": 413,
               "done": True}
        return
    headers = {"User-Agent": "agents-relay/channel-rpc"}
    token = _resolve_loopback_secret()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if req.get("content_type"):
        headers["Content-Type"] = str(req["content_type"])
    if req.get("accept"):
        headers["Accept"] = str(req["accept"])
    if req.get("x_klanker_hop"):
        # hop-count survives the channel hop so a bounced dispatch on the
        # target hits its loop guard instead of proxying again (klanker#148)
        try:
            headers["X-Klanker-Hop"] = str(int(req["x_klanker_hop"]))
        except (TypeError, ValueError):
            pass

    url = f"http://127.0.0.1:{cfg.relay_port}{path}{sep + query if sep else ''}"
    hreq = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        resp = urllib.request.urlopen(hreq, timeout=600)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", "replace")
        finally:
            e.close()
        yield {"status": e.code, "content_type": "application/json",
               "body": err_body, "done": True}
        return
    except (urllib.error.URLError, OSError) as exc:
        yield {"error": f"local relay unreachable: {exc}"}
        return

    try:
        ctype = str(resp.headers.get("Content-Type") or "")
        if path.startswith(_STREAM_PATHS):
            yield {"status": resp.status, "content_type": ctype}
            import codecs

            # Incremental decode: a multi-byte UTF-8 char may straddle a
            # read1 boundary (agents-relay#44).
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            try:
                while True:
                    # read1 returns what the socket has NOW — read(4096)
                    # stalls SSE frames under the buffer size
                    # (klanker#148 / agents-relay#44).
                    chunk = resp.read1(8192)
                    if not chunk:
                        break
                    text = decoder.decode(chunk)
                    if text:
                        yield {"chunk": text}
                tail = decoder.decode(b"", final=True)
                if tail:
                    yield {"chunk": tail}
            except OSError as exc:
                yield {"error": str(exc)}
                return
            yield {"done": True}
            return
        raw = resp.read(MAX_HTTP_BODY_BYTES + 1)
    finally:
        resp.close()
    if len(raw) > MAX_HTTP_BODY_BYTES:
        yield {"error": "response body too large", "status": 502,
               "done": True}
        return
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
        self.revoked = False  # set on HTTP 401 — permanent, no reconnect
        self._seq_dirty = 0
        self.on_op = None    # fn(payload) — wired by the sync layer
        self.on_ack = None   # fn(payload) — wired by the sync layer
        self.on_ws = None    # fn(kind, payload) — ws-share control frames
        self.on_hello = None  # fn(payload) — reconnect catch-up trigger
        self.on_approval = None  # fn(kind, payload) — approval mirror/decision
        self.on_revoked = None  # fn() — fires once on hub-side revocation

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

    def _mark_revoked(self) -> None:
        """Hub said 401: the credential is dead — stop permanently and
        notify the wiring layer once (agents-relay#44)."""
        self.revoked = True
        self._stop.set()
        fn = getattr(self, "on_revoked", None)
        if fn is not None:
            try:
                fn()
            except Exception:  # noqa: BLE001 - hook never kills the stop
                log.exception("on_revoked hook failed")

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
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                # Revoked device: retrying can never succeed — stop the
                # client for good instead of hammering the hub
                # (agents-relay#42).
                log.error("hub send 401 — device revoked, stopping channel")
                self._mark_revoked()
                return False
            log.warning("hub send failed: %s", exc)
            return False
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
        try:
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
        finally:
            # Always release the socket — on _stop, on generator close,
            # on reader exit (agents-relay#44).
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

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
                    elif kind in ("approval-req", "approval-res"):
                        if self.on_approval is not None:
                            self.on_approval(kind, msg.get("payload") or {})
                    elif kind == "resync":
                        self.last_seq = 0  # full resync handled by op-log layer
                    # hello/presence are informational only
                self._persist_seq(force=True)
                backoff = _RECONNECT_MIN
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    log.error("stream 401 — device revoked, stopping channel")
                    self._mark_revoked()
                    break
                log.warning("channel disconnected: %s", exc)
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
