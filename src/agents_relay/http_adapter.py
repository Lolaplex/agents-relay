"""stdlib HTTP server: /v1/turn, /v1/stream, /v1/inject, /v1/alert,
/webhook/alert, /v1/webhook/*, /v1/a2a/*, /v1/traces, /v1/approvals."""

from __future__ import annotations

import hmac
import json
import logging
import re
import time
import urllib.parse
from collections import deque
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from . import __version__
from .a2a import handle_a2a_mail, list_peers
from .config import RelayConfig
from .loop_client import LoopTurnResult, run_loop_turn
from .turn import iter_loop_stream, resolve_workspace_id, turn_params_from_body, valid_id
from .web import (
    cookie_from_header,
    handle_chat_stop,
    handle_chat_stream,
    handle_login,
    handle_logout,
    handle_runs_attach,
    handle_status,
    handle_token_query,
    serve_static,
    verify_cookie,
    web_enabled,
)
from .push import handle_push_send, handle_push_subscribe, handle_vapid_key
from .webhooks import handle_webhook

log = logging.getLogger("agents_relay.http")

_ENVELOPE_KEYS = frozenset({
    "channel",
    "user",
    "chat_id",
    "text",
    "message",
    "session",
    "user_id",
    "new_session",
    "notify",
    "broadcast",
    "attachments",
    "async",
    "prefix",
    "prompt",
    "secret",
    "token",
    "provider",
    "model",
    "persona",
    "project",
})


def _parse_query(raw_path: str) -> tuple[str, dict[str, str]]:
    parsed = urllib.parse.urlsplit(raw_path)
    path = parsed.path.rstrip("/") or "/"
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
    params = {k: v[-1] for k, v in qs.items() if v}
    return path, params


def _check_secret(
    handler: BaseHTTPRequestHandler,
    expected: str,
    query_params: dict[str, str] | None = None,
    *,
    allow_device: bool = True,
) -> bool:
    del query_params
    cfg = getattr(handler, "config", None)
    if not expected:
        # A web surface without a relay secret would expose every endpoint to
        # anyone who can reach the port. The cookie is then the only gate.
        if isinstance(cfg, RelayConfig) and web_enabled(cfg):
            raw = cookie_from_header(handler.headers.get("Cookie") or "")
            return bool(raw and verify_cookie(cfg, raw))
        return True
    auth_header = (handler.headers.get("Authorization") or "").strip()
    bearer = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    got = (
        handler.headers.get("X-Relay-Secret")
        or handler.headers.get("Relay-Secret")
        or handler.headers.get("X-Gateway-Secret")
        or handler.headers.get("Gateway-Secret")
        or bearer
        or ""
    ).strip()
    if got and hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8")):
        return True
    # Scoped device credentials (agents-relay#40): a paired device token
    # authorizes the agent-facing API surface like the master secret but
    # is revocable per device via DELETE /v1/hub/devices/<id>. It must
    # never authorize credential minting or credential management — that
    # would let one stolen token mint unlimited further credentials.
    if got and allow_device:
        try:
            from .devices import device_id_for_token

            if device_id_for_token(got):
                return True
        except Exception:
            pass
    # A browser client authenticates with an HttpOnly cookie: the token stays
    # out of JS storage and EventSource, which cannot set headers, still works.
    if isinstance(cfg, RelayConfig) and web_enabled(cfg):
        raw = cookie_from_header(handler.headers.get("Cookie") or "")
        if raw and verify_cookie(cfg, raw):
            return True
    return False


def _auth_context(handler: BaseHTTPRequestHandler) -> dict:
    """Structured caller identity (agents-relay#41).

    kind: "admin" (master secret) | "cookie" (web session) |
          "device" (paired device token) | "" (unauthenticated).
    Device callers carry device_id, role (host|client|ws_member) and
    the ws allowlist — ws_member can only touch workspaces in it.
    """
    ctx = {"kind": "", "device_id": "", "role": "",
           "allowlist": frozenset()}
    expected = getattr(handler, "relay_secret", "") or ""
    auth_header = (handler.headers.get("Authorization") or "").strip()
    bearer = (auth_header[7:].strip()
              if auth_header.lower().startswith("bearer ") else "")
    got = (handler.headers.get("X-Relay-Secret")
           or handler.headers.get("Relay-Secret")
           or handler.headers.get("X-Gateway-Secret")
           or handler.headers.get("Gateway-Secret")
           or bearer or "").strip()
    if expected and got and hmac.compare_digest(
            got.encode("utf-8"), expected.encode("utf-8")):
        ctx["kind"] = "admin"
        return ctx
    cfg = getattr(handler, "config", None)
    if not expected:
        # No master secret: the web cookie is the only gate — without a
        # web surface this is loopback admin, matching _check_secret.
        if not (isinstance(cfg, RelayConfig) and web_enabled(cfg)):
            ctx["kind"] = "admin"
            return ctx
    if got:
        try:
            from .devices import device_id_for_token, load_devices

            did = device_id_for_token(got)
            if did:
                meta = (load_devices().get(did) or {})
                ctx.update(kind="device", device_id=did,
                           role=str(meta.get("role") or "host"),
                           allowlist=frozenset(
                               str(w) for w in (meta.get("allowlist")
                                                or [])))
                return ctx
        except Exception:  # noqa: BLE001
            pass
    if isinstance(cfg, RelayConfig) and web_enabled(cfg):
        raw = cookie_from_header(handler.headers.get("Cookie") or "")
        if raw and verify_cookie(cfg, raw):
            ctx["kind"] = "cookie"
    return ctx


def _ws_allowed(ctx: dict, ws_id: str) -> bool:
    """ws_member allowlist gate; every other caller passes."""
    if ctx.get("role") == "ws_member":
        return bool(ws_id) and ws_id in (ctx.get("allowlist") or ())
    return True


def _body_ws(body: dict) -> str:
    return str((body or {}).get("workspace") or "").strip()


# ws_member may proxy ONLY these surfaces to a channel device in the
# same allowlisted workspace (agents-relay#44).
_MEMBER_PROXY_PATHS = frozenset({
    "/v1/turn", "/v1/chat/stream", "/v1/stream", "/v1/chat/stop",
})


def _proxy_allowed(ctx: dict, rid: str = "", rest: str = "",
                   cfg: Any = None, body: dict | None = None) -> bool:
    """`/v1/remote/*` pivots are operator/host-device surfaces —
    client devices never reach them.

    ws_member exception (agents-relay#44): Hub→Spoke turn/stream/stop
    proxy only when the body is scoped to an allowlisted ws that admits
    the caller AND the target device is in that same ws's sync_hosts."""
    kind = ctx.get("kind")
    if kind in ("admin", "cookie"):
        return True
    if kind != "device":
        return False
    role = ctx.get("role")
    if role == "host":
        return True
    if role != "ws_member":
        return False
    upstream = (rest or "").split("?", 1)[0].rstrip("/")
    if upstream not in _MEMBER_PROXY_PATHS:
        return False
    target = ""
    try:
        from .remotes import load_remotes

        if cfg is not None:
            target = str((load_remotes(cfg).get(rid) or {})
                         .get("device_id") or "")
    except Exception:  # noqa: BLE001 - registry read fails closed
        return False
    if not target:
        return False
    if body is None or _ws_member_body_denied(ctx, body):
        return False
    ws_id = _body_ws(body)
    if not _ws_allowed(ctx, ws_id):
        return False
    did = str(ctx.get("device_id") or "")
    try:
        from .workspaces import get_workspace
    except ImportError:
        return False
    try:
        ws = get_workspace(ws_id)
    except (KeyError, ValueError, OSError):
        return False
    return bool(ws.allows_device(did)
                and target in set(ws.sync_hosts or ()))


# ws_member path whitelist (agents-relay#41): channel frames + ws-scoped
# app reads/writes inside the allowlist — everything else is denied.
_WS_MEMBER_ALWAYS = frozenset({
    "/v1/hub/stream", "/v1/hub/send", "/v1/hub/lease", "/v1/hub/leases",
    "/v1/auth/status", "/health", "/v1/pair/redeem",
})
_WS_MEMBER_TURN = frozenset({
    "/v1/turn", "/v1/chat/stream", "/v1/stream",
    "/v1/runs/stream", "/v1/chat/stop", "/v1/inject",
})
# Body keys a ws_member caller may never set — exec/policy escapes
# (workdir, browser_profile, force-driven host switch) are host trust
# boundaries, not guest surface (agents-relay#44).
_WS_MEMBER_DENY_KEYS = frozenset({
    "sync_hosts", "approval_mode", "workdir", "workdir_lock",
    "browser_profile", "force", "new_host",
    "sync_policy", "default_host", "repos", "execution",
})


def _ws_member_body_denied(ctx: dict, body: Any) -> bool:
    """True when a ws_member body is non-object or carries policy/exec keys."""
    if ctx.get("role") != "ws_member":
        return False
    if not isinstance(body, dict):
        return True
    return bool(set(body or {}) & _WS_MEMBER_DENY_KEYS)


def _device_admin_denied(ctx: dict) -> bool:
    """Workspace/admin mutations are operator surface — host/client
    device tokens never reach them (agents-relay#44)."""
    return ctx.get("kind") == "device" and ctx.get("role") != "ws_member"


def _workspace_owner_id(ws_id: str) -> str:
    """execution.owner for ``ws_id``; unknown/corrupt workspaces fail closed."""
    try:
        from .workspaces import get_workspace

        execution = getattr(get_workspace(ws_id), "execution", None) or {}
    except (ImportError, KeyError, ValueError, OSError, TypeError):
        return ""
    if not isinstance(execution, dict):
        return ""
    return str(execution.get("owner") or "").strip()


def _force_host_switch_allowed(ctx: dict, ws_id: str) -> bool:
    """force=True is operator/workspace-owner only; everyone else fails closed."""
    if ctx.get("kind") in ("admin", "cookie"):
        return True
    if ctx.get("kind") != "device":
        return False
    owner = _workspace_owner_id(ws_id)
    did = str(ctx.get("device_id") or "")
    return bool(owner and did) and hmac.compare_digest(owner, did)


def _ws_member_gate(ctx: dict, method: str, path: str,
                    parts: list, query: dict) -> str:
    """Rejection reason for ws_member callers, else "".

    Fail-closed: a ws guest may use channel frames, turn/stream entry
    points (body workspace is verified per-route), own approvals, and
    /v1/workspaces/<member-ws>/* — but never ws deletion, policy keys,
    or admin surfaces. POST remote turn/stream/stop proxying passes this
    path gate only; `_proxy_allowed` applies the ws/body/target ACL.
    """
    if path in _WS_MEMBER_ALWAYS or path in _WS_MEMBER_TURN:
        return ""
    if path.startswith("/v1/remote/"):
        if method == "POST":
            rparts = path.split("/", 4)
            upstream = "/" + rparts[4] if len(rparts) > 4 else "/"
            if upstream.rstrip("/") in _MEMBER_PROXY_PATHS:
                return ""
        return "remote proxy is denied to workspace guests"
    if path.startswith("/v1/remotes"):
        return "remote proxy is denied to workspace guests"
    if path.startswith("/v1/approvals"):
        return ""
    if path == "/v1/events":
        ws = str((query or {}).get("workspace") or "")
        if _ws_allowed(ctx, ws):
            return ""
        return "workspace not in allowlist"
    if len(parts) >= 4 and parts[1] == "v1" \
            and parts[2] == "workspaces":
        ws_id = parts[3]
        if not _ws_allowed(ctx, ws_id):
            return "workspace not in allowlist"
        if method == "DELETE":
            if len(parts) == 4:
                return "ws_member cannot delete workspaces"
            # A guest may archive a persona row but never hard-delete
            # it (agents-relay#44).
            if str((query or {}).get("hard") or "") == "1":
                return "ws_member cannot hard-delete"
        return ""
    return "denied to workspace guests"


def _check_device(handler: BaseHTTPRequestHandler) -> str:
    """Device-channel auth: Bearer device_token + X-Device-Id (agents-relay#40).

    Returns the authenticated device_id or ''. The relay master secret also
    passes (operator/admin path)."""
    auth_header = (handler.headers.get("Authorization") or "").strip()
    bearer = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    if bearer:
        expected = getattr(handler, "relay_secret", "") or ""
        if expected and hmac.compare_digest(bearer.encode("utf-8"), expected.encode("utf-8")):
            return "__admin__"
        from .channel import device_id_from_headers
        from .devices import device_id_for_token, verify_device

        did = device_id_from_headers(handler)
        if did and verify_device(did, bearer):
            return did
        return device_id_for_token(bearer)
    return ""


def _extract_payload_text(
    body: dict,
    query_params: dict[str, str] | None = None,
    *,
    include_extra: bool = False,
) -> str:
    """Extract turn text from a relay request or generic structured webhook JSON."""
    qp = query_params or {}
    raw_primary = str(body.get("text") or body.get("message") or "")
    if not include_extra:
        return raw_primary

    primary = raw_primary.strip()
    prefix = str(
        body.get("prefix")
        or body.get("prompt")
        or qp.get("prefix")
        or qp.get("prompt")
        or ""
    ).strip()

    extra_lines: list[str] = []
    for key, val in body.items():
        if key in _ENVELOPE_KEYS or val is None or val == "":
            continue
        if isinstance(val, (dict, list)):
            rendered = json.dumps(val, ensure_ascii=False)
        else:
            rendered = str(val).strip()
        if rendered:
            extra_lines.append(f"{key}: {rendered}")

    if not extra_lines and not prefix:
        return raw_primary

    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    if primary:
        parts.append(primary)
    if extra_lines:
        parts.append("\n".join(extra_lines))
    return "\n\n".join(parts)


class _TurnHook:
    """Wrap a turn callable so BaseHTTPRequestHandler does not bind it as a method."""

    __slots__ = ("fn",)

    def __init__(self, fn: Callable[..., LoopTurnResult]) -> None:
        self.fn = fn

    def __call__(self, **kwargs) -> LoopTurnResult:
        return self.fn(**kwargs)


MAX_HTTP_BODY_BYTES = 16 * 1024 * 1024


def _read_json_body(handler: BaseHTTPRequestHandler) -> tuple[dict, bytes]:
    raw_len = (handler.headers.get("Content-Length") or "0").strip()
    try:
        length = int(raw_len)
    except ValueError as exc:
        raise ValueError("invalid Content-Length") from exc
    if length < 0:
        raise ValueError("invalid Content-Length")
    if length > MAX_HTTP_BODY_BYTES:
        raise ValueError(f"body too large: {length} bytes")
    raw = handler.rfile.read(length)
    try:
        body = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except json.JSONDecodeError as exc:
        raise ValueError("invalid json") from exc
    if not isinstance(body, dict):
        raise ValueError("json body must be an object")
    return body, raw


def _is_dev_origin(origin: str) -> bool:
    """Loopback http(s) origins are dev servers/dashboards — echo them so the
    desktop dev shell and local PWAs reach any relay without RELAY_CORS_ORIGINS.
    Endpoints still require their secret, so nothing is exposed."""
    try:
        host = urllib.parse.urlsplit(origin).hostname or ""
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1")


def _sse_event(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


# Kwargs accepted by loop_client.run_loop_turn beyond the core turn fields.
# turn_hook callables (e.g. telegram inbound) may not accept them.
_OPTIONAL_TURN_KWARGS = ("attachments", "provider", "model", "persona", "project", "workdir", "extra_env")


_LOG_SECRET_RE = re.compile(r"([?&](?:token|secret|code|pair)=)([^&\s]+)", re.IGNORECASE)


def _redact_log_text(text: str) -> str:
    return _LOG_SECRET_RE.sub(r"\1[REDACTED]", text)


def _is_private_host(host: str) -> bool:
    """Loopback/LAN host check: direct requests to these are plain HTTP."""
    h = host.strip()
    if h.startswith("["):
        h = h[1 : h.index("]")] if "]" in h else h
    elif h.count(":") == 1:
        h = h.rsplit(":", 1)[0]
    h = h.lower()
    if h in ("localhost", "::1") or h.endswith((".local", ".lan", ".internal")):
        return True
    parts = h.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        return (
            a in (10, 127)
            or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b == 168)
            or (a == 169 and b == 254)
        )
    return False


def _trust_proxy() -> bool:
    """X-Forwarded-* headers are client-supplied — only honor them
    behind a trusted reverse proxy (RELAY_TRUST_PROXY=1). Otherwise a
    spoofed header poisons generated pair URLs and cookie security
    decisions (agents-relay#42)."""
    import os

    return os.environ.get("RELAY_TRUST_PROXY", "").strip().lower() in (
        "1", "true", "yes", "on")


_HOST_BAD_RE = re.compile(r"[\x00-\x20\"'<>\\^`{|}]+")


def _clean_host(raw: str) -> str:
    """Host/X-Forwarded-Host value or '' — whitespace, quotes and CRLF
    never reach a generated URL (agents-relay#44)."""
    h = (raw or "").split(",")[0].strip()
    return "" if not h or _HOST_BAD_RE.search(h) else h


def _request_base_url(handler, cfg) -> str:
    """Absolute base for self-links (pair URLs).

    Order: explicit ``AGENTS_RELAY_URL`` > proxy headers > request ``Host``
    > auto-bound URL. ``serve_http`` backfills ``relay_url`` with the bound
    socket address (often loopback) — that value is a fallback, not a
    user choice, so a real request Host beats it. Scheme falls back to
    http on loopback/private hosts and https elsewhere.
    """
    configured = str(getattr(cfg, "relay_url", "") or "").strip().rstrip("/")
    auto_bound = str(getattr(getattr(handler, "server", None), "relay_url", "") or "").strip().rstrip("/")
    if configured and configured != auto_bound:
        return configured
    if _trust_proxy():
        proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        host = _clean_host(
            handler.headers.get("X-Forwarded-Host")
            or handler.headers.get("Host") or "")
    else:
        proto = ""
        host = _clean_host(handler.headers.get("Host") or "")
    if host:
        if proto not in ("http", "https"):
            proto = "http" if _is_private_host(host) else "https"
        return f"{proto}://{host}"
    return configured


def _emit_relay_event(kind: str, payload: dict[str, Any] | None = None, *, workspace: str = "") -> None:
    try:
        from .events import emit_event

        emit_event(kind, payload, workspace=workspace)
    except Exception:
        pass


# ws-share hooks (klanker#145): the host app (klanker serve) wires these
# at boot. ws_invite_auth gates invite minting (owner check);
# ws_grant_hook applies the membership grant on ws-mode redeem.
ws_invite_auth = None   # fn(device_id, ws_id) -> bool
ws_grant_hook = None    # fn(device_id, ws_id, granted_by) -> None
# session:host remote exec (klanker#145 R5): wired by klanker serve —
# fn(handler, path, body) -> bool, True when the request was proxied to
# the session's bound host via the hub channel.
remote_turn_dispatch = None
# GET-shaped entry points (/v1/runs/stream): same session:host routing,
# query instead of body (klanker#148).
remote_query_dispatch = None
# Run-lock lease guards: acquire before the first local turn execution
# returns {error payload}->write 409, release runs at turn end. Klanker
# wires both for hub AND spoke executors (klanker#148, klanker#136).
turn_lease_hook = None        # fn(handler, body, ctx) -> dict|None
turn_lease_release_hook = None  # fn(body, ctx) -> None
# Live management hooks (agents-relay#43): klanker serve wires the
# device-identity layer — the relay stays free of hubs.json/vault I/O.
hub_links_fn = None            # fn() -> {"links": [...]}
hub_connect_fn = None          # fn(body) -> {"ok"|"error", ...}
hub_disconnect_fn = None       # fn(hub_id) -> {"ok"|"error", ...}
session_host_switch_fn = None  # fn(ws, sid, host, force) -> dict
# Spoke->Hub /hosts relay (agents-relay#45): klanker wires
# fn(ws_id) -> payload dict|None that fetches + caches the hub's own
# /hosts map over the live ChannelClient; None = link down.
spoke_hosts_fn = None


def _ws_hosts_payload(ws, ctx: dict, spoke_hosts_fn=None) -> dict:
    """GET /v1/workspaces/<ws>/hosts body (agents-relay#43, #45).

    Members come from ws.sync_hosts (+ execution.owner + default_host);
    ``online`` = live channel link on THIS hub process, ``presence`` the
    last inbound presence frame, ``leases`` the ws rows of hub_leases.
    The local caller ALWAYS gets a ``self`` row — admin Bearer/cookie
    callers resolve to the local host id — and a disconnected member's
    stale status is forced ``offline``. On a spoke with no inbound
    links the hub's own map is relayed via ``spoke_hosts_fn``;
    ``hub_offline`` is only true when this process genuinely cannot
    reach a hub (spoke-configured with no live link/hook).
    """
    from .channel import hub as _channel_hub
    from .devices import list_public
    from .leases import hub_leases

    ch = _channel_hub()
    self_id = str(ctx.get("device_id") or "")
    self_label = ""
    self_is_host = False
    if not self_id:
        try:
            from agents_workspace import Host

            host_obj = Host.from_env()
            self_id = str(host_obj.host_id() or "")
            lbl_fn = getattr(host_obj, "host_label", None)
            if callable(lbl_fn):
                self_label = str(lbl_fn() or "").strip()
            self_is_host = bool(self_id)
        except Exception:  # noqa: BLE001 - no host dir: no self row id
            self_id = ""
    links = ch.connected_devices()
    if not links and spoke_hosts_fn is not None:
        # Spoke path: relay the hub's own /hosts map so the picker sees
        # real presence instead of an empty member list.
        try:
            hub_map = spoke_hosts_fn(str(ws.id))
        except Exception:  # noqa: BLE001 - relay failure falls through
            hub_map = None
        if isinstance(hub_map, dict) and hub_map:
            out = dict(hub_map)
            out.update({"ok": True, "workspace": ws.id,
                        "hub_offline": False})
            hs = [h for h in (out.get("hosts") or [])
                  if isinstance(h, dict)]
            for h in hs:
                h["self"] = str(h.get("device_id") or "") == self_id
            # the local caller still gets its own self row — it is not
            # part of the hub's member map (agents-relay#45)
            if self_id and not any(h["self"] for h in hs):
                hs.append({
                    "device_id": self_id,
                    "label": self_label or self_id,
                    "role": "host" if self_is_host else "member",
                    "self": True, "online": True, "via": "local",
                    "last_seen": 0, "status": "online", "leases": []})
            out["hosts"] = hs
            return out
    members = {str(d) for d in (getattr(ws, "sync_hosts", None) or ())}
    owner = str((getattr(ws, "execution", None) or {}).get("owner") or "")
    default = str(getattr(ws, "default_host", "") or "")
    if owner:
        members.add(owner)
    if default:
        members.add(default)
    if self_id:
        members.add(self_id)
    labels = {r["id"]: r.get("label") for r in list_public()}
    leases: dict[str, list] = {}
    for s in hub_leases().snapshot():
        if s.get("ws") != ws.id:
            continue
        leases.setdefault(str(s.get("holder") or ""), []).append(
            {"session": s.get("session"), "run_id": s.get("run_id"),
             "fence": s.get("fence"),
             "expires_in": s.get("expires_in")})
    hosts = []
    for did in sorted(members):
        pres = ch.presence(did)
        connected = bool(pres.get("connected"))
        is_self = did == self_id
        hosts.append({
            "device_id": did,
            "label": (
                self_label
                if is_self and self_label and not labels.get(did)
                else str(labels.get(did) or did)
            ),
            "role": ("owner" if did == owner
                     else "default" if did == default
                     else "host" if is_self and self_is_host
                     else "member"),
            "self": is_self,
            # the caller's own row is always online — it is literally
            # serving/making this request (agents-relay#45)
            "online": connected or is_self,
            "via": "channel" if connected or did in labels else "local",
            "last_seen": pres.get("ts") or 0,
            # stale presence ("busy") must not survive a dropped link
            "status": (pres.get("status") or "online")
                      if connected or is_self else "offline",
            "leases": leases.get(did) or [],
        })
    if links:
        hub_offline = False  # this process is serving devices — it IS a hub
    else:
        try:
            from .channel_client import _load_hubs

            # spoke-configured but the relay above produced nothing:
            # the hub is unreachable from here
            hub_offline = bool(_load_hubs())
        except Exception:  # noqa: BLE001 - no hubs.json: we are the hub
            hub_offline = False
    return {
        "ok": True, "workspace": ws.id,
        "sync_policy": str(getattr(ws, "sync_policy", "") or ""),
        "owner": owner, "default_host": default,
        "hub_offline": hub_offline,
        "hosts": hosts,
    }


def _turn_lease_conflict(handler, body: dict) -> bool:
    """Acquire the (ws, session) run lease; True = conflict written."""
    if turn_lease_hook is None:
        return False
    try:
        conflict = turn_lease_hook(handler, body, handler._auth_ctx())  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - lease infra must fail loud, not run twice
        log.warning("turn lease hook failed: %s", exc)
        conflict = {"ok": False, "error": "lease_unavailable"}
    if conflict:
        handler._write_json(conflict, int(conflict.get("status") or 409))  # type: ignore[attr-defined]
        return True
    return False


class TurnHandler(BaseHTTPRequestHandler):
    relay_secret: str = ""
    config: RelayConfig | None = None
    turn_hook: _TurnHook | None = None

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), _redact_log_text(fmt % args))

    def handle_one_request(self) -> None:
        """Last error boundary: an unhandled exception must not drop the
        connection silently (client sees a bare RemoteDisconnected)."""
        try:
            super().handle_one_request()
        except Exception:
            self.close_connection = True
            log.exception("unhandled handler error on %s", getattr(self, "path", ""))
            try:
                self._write_json({"ok": False, "error": "internal handler error"}, 500)
            except Exception:  # response already half-written or peer gone
                pass

    def _read_json(self) -> dict | None:
        raw_len = (self.headers.get("Content-Length") or "0").strip()
        try:
            length = int(raw_len)
        except ValueError:
            self.send_error(400, "invalid Content-Length")
            return None
        if length < 0:
            self.send_error(400, "invalid Content-Length")
            return None
        if length > MAX_HTTP_BODY_BYTES:
            self.send_error(413, "request body too large")
            return None
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self.send_error(400, "invalid json")
            return None

    def _write_json(self, payload: dict, status: int) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _cfg(self) -> RelayConfig:
        return self.config or RelayConfig.from_env()

    def _auth_ctx(self) -> dict:
        ctx = getattr(self, "_ctx", None)
        if ctx is None:
            ctx = _auth_context(self)
            self._ctx = ctx
        return ctx

    def _gate_ws_member(self, method: str, path: str,
                        query_params: dict) -> bool:
        """True when the request was rejected (403 already sent)."""
        ctx = self._auth_ctx()
        if ctx.get("role") != "ws_member":
            return False
        reason = _ws_member_gate(ctx, method, path,
                                 path.split("/"), query_params)
        if reason:
            self.send_error(403, reason)
            return True
        return False

    #: Origins only a Tauri webview can produce — browsers never send these,
    #: so echoing them is safe for the bundled desktop shell.
    _TAURI_ORIGINS = (
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    )

    def _cors_origin(self) -> str:
        """Origin to echo on /v1/* when RELAY_CORS_ORIGINS allows it (off by default)."""
        path, _ = _parse_query(self.path or "/")
        # /health is a public liveness probe — dashboards poll it cross-origin
        # to mark instances up/down, so it needs the same echo as /v1/*.
        if not (path.startswith("/v1/") or path == "/health"):
            return ""
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return ""
        if origin in self._TAURI_ORIGINS or _is_dev_origin(origin):
            return origin
        origins = self._cfg().cors_origins
        if "*" in origins:
            return "*"
        return origin if origin in origins else ""

    def end_headers(self) -> None:
        allowed = self._cors_origin()
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        if not self._cors_origin():
            self.send_error(404)
            return
        self.send_response(204)
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, PUT, PATCH, DELETE, OPTIONS",
        )
        req_headers = (self.headers.get("Access-Control-Request-Headers") or "").strip()
        self.send_header(
            "Access-Control-Allow-Headers",
            req_headers or "Content-Type, Authorization, X-Relay-Secret",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _run_turn(self, turn_kwargs: dict) -> LoopTurnResult:
        runner = (self.turn_hook.fn if self.turn_hook else None) or run_loop_turn
        kwargs = dict(turn_kwargs)
        if runner is run_loop_turn:
            kwargs["config"] = self._cfg()
        try:
            return runner(**kwargs)
        except TypeError:
            optional = {k: kwargs.pop(k) for k in _OPTIONAL_TURN_KWARGS if k in kwargs}
            if not optional:
                raise
            return runner(**kwargs)

    def _handle_inject(self, query_params: dict[str, str] | None = None) -> None:
        body = self._read_json()
        if body is None:
            return
        # ws_member inject was the last unguarded turn surface — a guest
        # could smuggle text into any telegram chat (agents-relay#44).
        ctx = self._auth_ctx()
        if not _ws_allowed(ctx, _body_ws(body)):
            self.send_error(403, "workspace not in allowlist")
            return
        if _ws_member_body_denied(ctx, body):
            self.send_error(403, "ws_member cannot set workdir/policy fields")
            return
        qp = query_params or {}
        cfg = self._cfg()
        raw_id = str(
            body.get("chat_id")
            or body.get("user")
            or qp.get("chat_id")
            or qp.get("user")
            or ""
        ).strip()
        if not raw_id and cfg.telegram_allowed_chat_ids:
            chat_id = int(cfg.telegram_allowed_chat_ids[0])
        elif not raw_id:
            self.send_error(400, "chat_id is required")
            return
        else:
            try:
                chat_id = int(raw_id)
            except ValueError:
                self.send_error(400, "chat_id must be numeric")
                return

        text = _extract_payload_text(body, qp, include_extra=True)
        runner = self.turn_hook.fn if self.turn_hook else None
        is_async = str(qp.get("async") or body.get("async") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )

        if is_async:
            from .jobs import Job, get_registry
            from .telegram_adapter import chat_allowed, handle_inbound_text

            if not chat_allowed(chat_id, cfg):
                self.send_error(403, "chat_id not in TELEGRAM_ALLOWED_CHAT_IDS")
                return
            stripped = text.strip()
            if not stripped:
                self.send_error(400, "text is required")
                return
            reg = get_registry(cfg)

            def _run(job: Job) -> None:
                handle_inbound_text(
                    chat_id=chat_id,
                    text=stripped,
                    config=cfg,
                    on_turn=runner,
                    user=str(chat_id),
                    on_pid=job.bind_pid,
                    cancelled=job.cancel.is_set,
                )

            job = reg.submit(chat_id, stripped[:80], _run)
            if job is None:
                self.send_error(429, "job queue full")
                return
            self._write_json(
                {"ok": True, "queued": True, "job_id": job.id, "chat_id": chat_id},
                202,
            )
            return

        from .telegram_adapter import inbound_payload, inject_text

        try:
            result = inject_text(
                chat_id=chat_id,
                text=text,
                config=cfg,
                on_turn=runner,
            )
        except PermissionError as exc:
            self.send_error(403, str(exc))
            return
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        except Exception as exc:
            log.exception("inject failed")
            self.send_error(500, str(exc))
            return
        payload = inbound_payload(result)
        self._write_json(payload, 200 if result.returncode == 0 else 502)

    def do_POST(self) -> None:
        path, query_params = _parse_query(self.path)
        if self._gate_ws_member("POST", path, query_params):
            return

        if path == "/v1/auth/login":
            body = self._read_json()
            if body is None:
                return
            handle_login(self, body)
            return

        if path == "/v1/auth/logout":
            handle_logout(self)
            return

        if path == "/v1/pair/create":
            # Credential minting is master-secret only — a device token
            # must never mint further credentials (agents-relay#40).
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            from .pairing import create_pair

            mode = str(body.get("mode") or "web").strip().lower()
            if mode == "ws":
                # ws invites are minted by an owner device through
                # /v1/ws/invite, never by the admin pair endpoint.
                self.send_error(400, "mode=ws requires /v1/ws/invite")
                return
            ttl = body.get("ttl_sec", 60)
            host_id = ""
            try:
                from agents_workspace import Host

                host = Host.from_env()
                hid_fn = getattr(host, "host_id", None)
                if callable(hid_fn):
                    host_id = str(hid_fn() or "")
            except Exception:
                pass
            cfg = self._cfg()
            try:
                res = create_pair(
                    mode=mode,
                    ttl_sec=ttl,
                    host_id=host_id,
                    base_url=_request_base_url(self, cfg),
                )
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            self._write_json({"ok": True, **res}, 200)
            return

        if path == "/v1/ws/invite":
            # ws-share invite mint (klanker#145): a paired device or the
            # hub operator (master secret/cookie -> __admin__) asks for
            # a single-use code that redeems as device pair AND ws
            # member grant. ws_invite_auth decides ownership — never
            # grant without it; an unconfigured hook means invites are
            # off. relay_web cookie auth counts as operator too —
            # the UI mints invites for its own workspaces
            # (agents-relay#44).
            ctx = self._auth_ctx()
            device_id = ""
            if ctx.get("kind") == "device":
                device_id = str(ctx.get("device_id") or "")
            elif ctx.get("kind") in ("admin", "cookie"):
                device_id = "__admin__"
            if not device_id:
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            ws_id = str(body.get("ws") or "").strip()
            if not valid_id(ws_id):
                self.send_error(400, f"invalid workspace id: {ws_id!r}")
                return
            if ws_grant_hook is None or ws_invite_auth is None:
                self.send_error(503, "workspace invites not configured")
                return
            if not ws_invite_auth(device_id, ws_id):
                self.send_error(403, "not the workspace owner")
                return
            from .pairing import create_pair

            cfg = self._cfg()
            host_id = ""
            try:
                from agents_workspace import Host

                hid_fn = getattr(Host.from_env(), "host_id", None)
                if callable(hid_fn):
                    host_id = str(hid_fn() or "")
            except Exception:
                pass
            res = create_pair(
                mode="ws", ws=ws_id, granted_by=device_id,
                host_id=host_id, ttl_sec=body.get("ttl_sec", 60),
                base_url=_request_base_url(self, cfg))
            self._write_json({"ok": True, **res}, 200)
            return

        if path == "/v1/pair/redeem":
            body = self._read_json()
            if body is None:
                return
            cred = str(body.get("token") or body.get("code") or "").strip()
            from .pairing import redeem_pair
            from .web import _cookie_header, _secure_for, issue_cookie

            status_tag, entry = redeem_pair(cred, client_ip=self.address_string())
            if status_tag == "rate_limited":
                self._write_json({"ok": False, "error": "too many attempts"}, 429)
                return
            if status_tag != "ok" or entry is None:
                self._write_json({"ok": False, "error": "invalid or expired pairing code"}, 401)
                return
            cfg = self._cfg()
            if entry["mode"] == "web":
                if not web_enabled(cfg):
                    self._write_json({"ok": False, "error": "web surface disabled"}, 404)
                    return
                cookie_val = issue_cookie(cfg)
                payload = json.dumps(
                    {"ok": True, "mode": "web", "host_id": entry.get("host_id", "")}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header(
                    "Set-Cookie",
                    _cookie_header(cfg, cookie_val, secure=_secure_for(self, cfg)),
                )
                self.end_headers()
                self.wfile.write(payload)
                return
            ws_id = str(entry.get("ws") or "")
            granted_by = str(entry.get("granted_by") or "")
            label = str(body.get("label") or "").strip()
            if entry["mode"] == "ws":
                # Re-verify at redeem time (agents-relay#42): the inviter
                # may have been revoked or lost ownership since the code
                # was minted — the invite window is not a grant window.
                if ws_grant_hook is None or ws_invite_auth is None:
                    self._write_json(
                        {"ok": False,
                         "error": "workspace invites not configured"}, 503)
                    return
                from .devices import load_devices

                # __admin__ minted under the master secret — nothing to
                # re-verify at redeem; a device inviter must still exist
                # AND still pass the ownership hook.
                granter_ok = (
                    granted_by == "__admin__"
                    or (granted_by in load_devices()
                        and ws_invite_auth(granted_by, ws_id))
                )
                if not granter_ok:
                    self._write_json(
                        {"ok": False,
                         "error": "invite grant no longer valid"}, 403)
                    return
                # An already-paired device presenting its credential
                # gains the ws grant without a second device_id.
                ctx = self._auth_ctx()
                if ctx.get("kind") == "device" and ctx.get("device_id"):
                    did = str(ctx["device_id"])
                    try:
                        ws_grant_hook(did, ws_id, granted_by)
                        from .devices import add_allowlist

                        add_allowlist(did, ws_id)
                    except Exception as exc:  # noqa: BLE001
                        self._write_json(
                            {"ok": False,
                             "error": f"ws grant failed: {exc}"}, 500)
                        return
                    self._write_json(
                        {"ok": True, "mode": "ws", "ws": ws_id,
                         "host_id": entry.get("host_id", ""),
                         "granted_by": granted_by,
                         "device_id": did, "token": "",
                         "hub": {
                             "stream": "/v1/hub/stream",
                             "send": "/v1/hub/send",
                             "devices": "/v1/hub/devices",
                             "lease": "/v1/hub/lease",
                         }},
                        200,
                    )
                    return
            # mode=remote|ws mints a scoped device credential — the master
            # relay_secret is never handed out by pairing (agents-relay#38/#40).
            try:
                from .devices import mint_device

                # mode=ws guests are ws_member-scoped to the granted ws;
                # mode=remote pairs a compute node (role=host).
                dev = mint_device(
                    label=label or self.address_string(),
                    host_id=entry.get("host_id", ""),
                    role="ws_member" if entry["mode"] == "ws" else "host",
                    allowlist=[ws_id] if entry["mode"] == "ws" else None,
                )
            except Exception as exc:  # noqa: BLE001
                self._write_json(
                    {"ok": False, "error": f"device mint failed: {exc}"}, 500)
                return
            if entry["mode"] == "ws" and ws_id and ws_grant_hook is not None:
                try:
                    ws_grant_hook(
                        dev["device_id"], ws_id, granted_by)
                except Exception as exc:  # noqa: BLE001
                    # Roll back the minted device — no leaked credential
                    # for a grant that never landed (agents-relay#42).
                    from .devices import revoke_device

                    revoke_device(dev["device_id"])
                    self._write_json(
                        {"ok": False,
                         "error": f"ws grant failed: {exc}"}, 500)
                    return
            self._write_json(
                {
                    "ok": True,
                    "mode": entry["mode"],
                    "host_id": entry.get("host_id", ""),
                    "ws": ws_id,
                    "granted_by": entry.get("granted_by", ""),
                    "device_id": dev["device_id"],
                    "token": dev["device_token"],
                    "hub": {
                        "stream": "/v1/hub/stream",
                        "send": "/v1/hub/send",
                        "devices": "/v1/hub/devices",
                        "lease": "/v1/hub/lease",
                    },
                },
                200,
            )
            return

        if path == "/v1/remotes":
            # Vault writes are admin-only — device tokens may read mounts
            # but never inject or drop stored credentials.
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            from .remotes import upsert_remote

            row, err = upsert_remote(self._cfg(), body)
            if row is None:
                self.send_error(400, err)
                return
            _emit_relay_event("remote.changed", {"action": "upsert", "id": row.get("id", "")})
            self._write_json({"ok": True, "remote": row}, 200)
            return
        if path == "/v1/hub/send":
            # Upstream batch from a connected device (agents-relay#40).
            did = _check_device(self)
            if not did or did == "__admin__":
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            from .channel import handle_hub_send

            self._write_json(handle_hub_send(self, did, body), 200)
            return
        if path == "/v1/hub/lease":
            # Session run-lock lease ops (klanker#136): acquire/renew/release.
            did = _check_device(self)
            if not did or did == "__admin__":
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            from .leases import default_ttl, hub_leases

            book = hub_leases()
            action = str(body.get("action") or "")
            ws = str(body.get("ws") or "")
            session = str(body.get("session") or "")
            if not ws or not session:
                self.send_error(400, "ws and session required")
                return
            if not _ws_allowed(self._auth_ctx(), ws):
                self.send_error(403, "workspace not in allowlist")
                return
            fence = 0
            if action in ("renew", "release"):
                raw_fence = body.get("fence")
                if isinstance(raw_fence, bool) or not isinstance(raw_fence, (int, str)):
                    self.send_error(400, "fence must be an integer")
                    return
                try:
                    fence = int(raw_fence)
                except ValueError:
                    self.send_error(400, "fence must be an integer")
                    return
                if fence <= 0:
                    self.send_error(400, "fence must be positive")
                    return
            # The workspace must actually admit this device — an
            # allowlist row is a route filter, ws.allows_device is the
            # exec trust boundary (agents-relay#44).
            try:
                from .workspaces import get_workspace
            except ImportError:
                self.send_error(503, "workspace registry unavailable")
                return
            try:
                lease_ws = get_workspace(ws)
            except (KeyError, ValueError):
                self.send_error(404, "unknown workspace")
                return
            try:
                if not lease_ws.allows_device(did):
                    self.send_error(403, "device not in workspace sync_hosts")
                    return
            except Exception:  # noqa: BLE001 - policy read fails closed
                self.send_error(403, "device not in workspace sync_hosts")
                return
            if action == "acquire":
                out = book.acquire(
                    ws, session, did, str(body.get("run_id") or ""),
                    ttl=default_ttl(ws))
            elif action == "renew":
                out = book.renew(ws, session, did, fence,
                                 ttl=default_ttl(ws))
            elif action == "release":
                out = book.release(ws, session, did, fence)
            else:
                self.send_error(400, f"unknown lease action: {action!r}")
                return
            self._write_json(out, 200)
            return

        if path == "/v1/workspaces":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            if _device_admin_denied(self._auth_ctx()):
                self.send_error(403, "workspace admin is operator-only")
                return
            body = self._read_json()
            if body is None:
                return
            label = str(body.get("label") or "").strip()
            ws_id = str(body.get("id") or "").strip()
            if ws_id and not valid_id(ws_id):
                self.send_error(400, f"invalid workspace id: {ws_id!r}")
                return
            if not ws_id and not label:
                self.send_error(400, "id or label is required")
                return
            from .workspaces import create_workspace

            try:
                ws = create_workspace(label, ws_id)
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            _emit_relay_event("workspace.changed", {"action": "create", "workspace": ws.id}, workspace=ws.id)
            self._write_json({"ok": True, "workspace": ws.to_dict()}, 201)
            return

        if path == "/v1/jobs":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            if _device_admin_denied(self._auth_ctx()):
                self.send_error(403, "job control is operator-only")
                return
            body = self._read_json()
            if body is None:
                return
            ws_ref = str(body.get("workspace") or "").strip()
            persona = str(body.get("persona") or "").strip()
            goal = str(body.get("goal") or "").strip()
            if not ws_ref or not valid_id(ws_ref):
                self.send_error(400, "workspace id is required")
                return
            if not persona or not valid_id(persona):
                self.send_error(400, "persona id is required")
                return
            if not goal:
                self.send_error(400, "goal is required")
                return
            from agents_workspace import Host

            host = Host.from_env()
            if not host.workspace_exists(ws_ref):
                self.send_error(404, f"unknown workspace: {ws_ref}")
                return
            ws = host.workspace(ws_ref)
            if not (ws.personas / persona / "persona.json").is_file():
                self.send_error(404, f"unknown persona: {persona}")
                return
            from .coding_jobs import create_job
            from .loop_client import run_loop_turn

            runner = (self.turn_hook.fn if self.turn_hook else None) or run_loop_turn

            def _job_turn(**kw):
                kw.setdefault("config", self._cfg())
                return runner(**kw)

            try:
                job = create_job(
                    ws,
                    persona=persona,
                    goal=goal,
                    workdir=str(body.get("workdir") or ""),
                    provider=str(body.get("provider") or ""),
                    model=str(body.get("model") or ""),
                    user=str(body.get("user") or ""),
                    run_turn=_job_turn,
                )
            except RuntimeError as exc:
                self.send_error(501, str(exc))
                return
            self._write_json({"ok": True, "job": job}, 201)
            return

        parts = path.split("/")
        # POST /v1/workspaces/<ws>/store/append
        if (len(parts) == 6 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "store"
                and parts[5] == "append"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            ws_ref = parts[3]
            if not valid_id(ws_ref):
                self.send_error(400, f"invalid workspace id: {ws_ref!r}")
                return
            from .workspaces import get_workspace
            from .store_api import handle_store_append

            try:
                ws = get_workspace(ws_ref)
            except KeyError:
                self.send_error(404, "unknown workspace")
                return
            body = self._read_json()
            if body is None:
                return
            _actx = self._auth_ctx()
            status_code, payload = handle_store_append(
                ws, body,
                device_id=(str(_actx.get("device_id") or "")
                           if _actx.get("kind") == "device" else ""))
            self._write_json(payload, status_code)
            return

        # /v1/workspaces/<ws>/personas[/<p>[/sessions[/<s>]]]
        if (len(parts) >= 5 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "personas"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_persona_post(parts, query_params)
            return

        # POST /v1/workspaces/<ws>/sessions/<sid>/host — session host
        # switch between prompts (klanker#148 / agents-relay#43):
        # checkpoint -> rebind -> cleanup lives in the klanker hook.
        if (len(parts) == 7 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "sessions"
                and parts[6] == "host"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            ws_id, sid = parts[3], parts[5]
            if not valid_id(ws_id) or not valid_id(sid):
                self.send_error(400, "invalid workspace/session id")
                return
            body = self._read_json()
            if body is None:
                return  # _read_json already sent 400/413 (agents-relay#45)
            ctx = self._auth_ctx()
            if _ws_member_body_denied(ctx, body):
                self.send_error(403, "ws_member cannot set workdir/policy fields")
                return
            new_host = str(body.get("host") or "").strip()
            if not new_host:
                self.send_error(400, "host is required")
                return
            if not valid_id(new_host):
                self.send_error(400, f"invalid host id: {new_host!r}")
                return
            force = bool(body.get("force"))
            if force and not _force_host_switch_allowed(ctx, ws_id):
                self.send_error(403, "force host switch requires workspace owner")
                return
            fn = getattr(self.server, "session_host_switch_fn", None) \
                or session_host_switch_fn
            if fn is None:
                self._write_json(
                    {"ok": False,
                     "error": "host switch not configured"}, 501)
                return
            try:
                out = fn(ws_id, sid, new_host, force=force)
            except Exception as exc:  # noqa: BLE001
                self.send_error(500, f"host switch failed: {exc}")
                return
            out = dict(out or {})
            if out.get("error"):
                self._write_json(
                    {"ok": False, **out},
                    int(out.get("status") or 409))
                return
            try:
                from .events import emit_event

                emit_event("session.bound", {
                    "session": sid, "host": new_host}, workspace=ws_id)
            except Exception:  # noqa: BLE001
                pass
            self._write_json({"ok": True, **out}, 200)
            return

        # POST /v1/hub/connect — live hub-link management (agents-relay#43):
        # redeem invite + write hubs.json + vault + spawn ChannelClient —
        # the klanker hook owns the lifecycle, no serve restart.
        if path == "/v1/hub/connect":
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            fn = getattr(self.server, "hub_connect_fn", None) \
                or hub_connect_fn
            if fn is None:
                self._write_json(
                    {"ok": False,
                     "error": "hub connect not configured"}, 501)
                return
            try:
                out = fn(dict(body))
            except Exception as exc:  # noqa: BLE001
                self.send_error(500, f"hub connect failed: {exc}")
                return
            out = dict(out or {})
            status = int(out.pop("status", 0) or 0)
            if out.get("error") and not status:
                status = 502
            self._write_json(out, status or 200)
            return

        # POST /v1/approvals/request — turn-authenticated via Bearer AGENTS_APPROVAL_TOKEN
        if path == "/v1/approvals/request":
            self._handle_approval_request(query_params)
            return

        # POST /v1/approvals/<id> {allow: true|false}
        if len(parts) == 4 and parts[1] == "v1" and parts[2] == "approvals":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_approval_post(parts[3], query_params)
            return

        if path == "/v1/send":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            if _device_admin_denied(self._auth_ctx()):
                self.send_error(403, "send is operator-only")
                return
            body = self._read_json()
            if body is None:
                return
            cfg = self._cfg()
            try:
                chat_id = int(str(body.get("chat_id") or body.get("user") or "").strip())
            except ValueError:
                self.send_error(400, "chat_id is required and must be numeric")
                return
            text = str(body.get("text") or body.get("message") or "").strip()
            if not text:
                self.send_error(400, "text is required")
                return
            from .telegram_adapter import send_to_user

            try:
                send_to_user(
                    token=cfg.telegram_bot_token,
                    chat_id=chat_id,
                    text=text,
                    allowed=cfg.telegram_allowed_chat_ids,
                    allow_anyone=cfg.allow_anyone,
                )
            except PermissionError as exc:
                self.send_error(403, str(exc))
                return
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except Exception as exc:
                self.send_error(502, f"send failed: {exc}")
                return
            self._write_json({"ok": True, "chat_id": chat_id}, 200)
            return

        if path == "/v1/model":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            if _device_admin_denied(self._auth_ctx()):
                self.send_error(403, "model switch is operator-only")
                return
            body = self._read_json()
            if body is None:
                return
            try:
                ws_ref = str(body.get("workspace") or query_params.get("workspace") or "").strip()
                ws = resolve_workspace_id(ws_ref) if ws_ref else None
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            from runner.model import ModelError, clear, use

            session = str(body.get("session") or query_params.get("session") or "").strip()
            try:
                if body.get("clear"):
                    if session:
                        # Clear only the session override; the ws pick stays
                        # the last-used seed for new chats.
                        from .providers_api import clear_session_pick

                        out = clear_session_pick(session)
                    else:
                        out = clear(workspace=ws)
                else:
                    provider = str(body.get("provider") or "").strip()
                    if not provider:
                        self.send_error(400, "provider is required")
                        return
                    model = str(body.get("model") or "").strip()
                    out = use(provider, model, workspace=ws)
                    if session:
                        # Per-session override; the ws pick above stays the
                        # last-used seed for new chats.
                        from .providers_api import set_session_pick

                        set_session_pick(session, provider, model)
            except ModelError as exc:
                self.send_error(400, str(exc))
                return
            except Exception as exc:
                self.send_error(500, f"model switch failed: {exc}")
                return
            self._write_json({"ok": True, **out}, 200)
            return

        if path == "/v1/providers":
            # Provider manifests carry executable commands — device
            # tokens never register new ones (agents-relay#41).
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            from .providers_api import ProviderError, create_provider

            try:
                row = create_provider(body)
            except ProviderError as exc:
                self.send_error(exc.status, exc.message)
                return
            except Exception as exc:
                self.send_error(500, f"provider create failed: {exc}")
                return
            _emit_relay_event("provider.changed", {"action": "create", "provider": row["name"]})
            self._write_json({"ok": True, "provider": row}, 200)
            return

        if path == "/v1/providers/secret":
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            from .providers_api import ProviderError, set_provider_secret

            try:
                out = set_provider_secret(
                    str(body.get("key_env") or ""),
                    str(body.get("value") or ""),
                )
            except ProviderError as exc:
                self.send_error(exc.status, exc.message)
                return
            except Exception as exc:
                self.send_error(500, f"provider secret write failed: {exc}")
                return
            _emit_relay_event("provider.changed", {"action": "secret", "key_env": out["key_env"]})
            self._write_json({"ok": True, **out}, 200)
            return

        if path == "/v1/favorites":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            if _device_admin_denied(self._auth_ctx()):
                self.send_error(403, "favorites are operator-only")
                return
            body = self._read_json()
            if body is None:
                return
            from .providers_api import ProviderError, set_favorites, star_favorite

            try:
                if isinstance(body.get("favorites"), list):
                    rows = set_favorites(body["favorites"])
                else:
                    rows = star_favorite(
                        str(body.get("provider") or ""),
                        str(body.get("model") or ""),
                        bool(body.get("star")),
                    )
            except ProviderError as exc:
                self.send_error(exc.status, exc.message)
                return
            self._write_json({"ok": True, "favorites": rows}, 200)
            return

        if path == "/v1/chat/stream":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            ctx = self._auth_ctx()
            if not _ws_allowed(ctx, _body_ws(body)):
                self.send_error(403, "workspace not in allowlist")
                return
            if _ws_member_body_denied(ctx, body):
                self.send_error(403, "ws_member cannot set workdir/policy fields")
                return
            if remote_turn_dispatch is not None and remote_turn_dispatch(
                    self, path, body):
                return
            if _turn_lease_conflict(self, body):
                return
            started = False
            try:
                started = bool(handle_chat_stream(self, body))
            finally:
                # A validation 400 after acquire must not pin the
                # (ws, session) run lock forever (agents-relay#44).
                if not started and turn_lease_release_hook is not None:
                    try:
                        turn_lease_release_hook(body, self._auth_ctx())
                    except Exception:  # noqa: BLE001
                        pass
            return

        if path == "/v1/chat/stop":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            # ws gate BEFORE dispatch/local — `{}` from a ws_member must
            # never fan out a host-wide cancel (agents-relay#44).
            if not _ws_allowed(self._auth_ctx(), _body_ws(body)):
                self.send_error(403, "workspace not in allowlist")
                return
            # a bound session's stop must reach the executing host —
            # local _ACTIVE has nothing for remote runs (klanker#148)
            if remote_turn_dispatch is not None and remote_turn_dispatch(
                    self, path, body):
                return
            handle_chat_stop(self, body)
            return

        if path == "/v1/push/subscribe":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            handle_push_subscribe(self, self._cfg(), body, query_params)
            return

        if path == "/v1/push/send":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            handle_push_send(self, self._cfg(), body, query_params)
            return

        # POST /v1/remote/<id>/<upstream-path> — authenticated remote mount.
        if path.startswith("/v1/remote/"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            rparts = path.split("/", 4)
            rid = rparts[3] if len(rparts) > 3 else ""
            if not valid_id(rid):
                self.send_error(400, f"invalid remote id: {rid!r}")
                return
            rest = "/" + rparts[4] if len(rparts) > 4 else "/"
            ctx = self._auth_ctx()
            proxy_body = None
            if ctx.get("role") == "ws_member":
                # The proxy forwards raw bytes, so read once here for the
                # ws ACL and cache them for handle_proxy. Member turn
                # surfaces are JSON objects; anything else fails closed.
                from .remotes import _read_proxy_body

                try:
                    raw_body = _read_proxy_body(self)
                except ValueError as exc:
                    self._write_json(
                        {"ok": False, "error": str(exc)},
                        413 if "too large" in str(exc) else 400)
                    return
                self._proxy_body_read = True
                self._proxy_body = raw_body
                try:
                    proxy_body = (json.loads(raw_body.decode("utf-8"))
                                  if raw_body else {})
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._write_json(
                        {"ok": False, "error": "invalid json"}, 400)
                    return
                if not isinstance(proxy_body, dict):
                    self._write_json(
                        {"ok": False, "error": "json object required"}, 400)
                    return
            if not _proxy_allowed(ctx, rid, rest, self._cfg(), proxy_body):
                self.send_error(403, "remote proxy denied")
                return
            from .remotes import handle_proxy

            handle_proxy(self, rid, rest)
            return

        if path == "/v1/inject":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_inject(query_params)
            return

        if path == "/v1/a2a/mail":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            try:
                body, _raw = _read_json_body(self)
            except ValueError:
                self.send_error(400, "invalid json")
                return
            result = handle_a2a_mail(body, config=self._cfg())
            self._write_json(result, 200 if result.get("ok", True) else 502)
            return

        if path == "/v1/stream":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            try:
                body, _raw = _read_json_body(self)
            except ValueError:
                self.send_error(400, "invalid json")
                return
            ctx = self._auth_ctx()
            if not _ws_allowed(ctx, _body_ws(body)):
                self.send_error(403, "workspace not in allowlist")
                return
            if _ws_member_body_denied(ctx, body):
                self.send_error(403, "ws_member cannot set workdir/policy fields")
                return
            if remote_turn_dispatch is not None and remote_turn_dispatch(
                    self, path, body):
                return
            if _turn_lease_conflict(self, body):
                return
            try:
                self._handle_stream(body)
            finally:
                if turn_lease_release_hook is not None:
                    try:
                        turn_lease_release_hook(body, self._auth_ctx())
                    except Exception:  # noqa: BLE001
                        pass
            return

        if path.startswith("/v1/webhook/"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            try:
                body, raw = _read_json_body(self)
            except ValueError:
                self.send_error(400, "invalid json")
                return
            self._handle_webhook(path.removeprefix("/v1/webhook/"), body, raw)
            return

        if path not in ("/v1/turn", "/v1/alert", "/webhook/alert"):
            self.send_error(404)
            return
        if not _check_secret(self, self.relay_secret, query_params):
            self.send_error(401, "unauthorized")
            return
        body = self._read_json()
        if body is None:
            return
        ctx = self._auth_ctx()
        if not _ws_allowed(ctx, _body_ws(body)):
            self.send_error(403, "workspace not in allowlist")
            return
        if _ws_member_body_denied(ctx, body):
            self.send_error(403, "ws_member cannot set workdir/policy fields")
            return
        if path == "/v1/turn" and remote_turn_dispatch is not None:
            # Fail loud, not silent local fallback: a bound session that
            # errors mid-proxy must not run the turn twice
            # (agents-relay#44).
            if remote_turn_dispatch(self, path, body):
                return
        if path == "/v1/turn" and _turn_lease_conflict(self, body):
            return
        try:
            is_alert = path in ("/v1/alert", "/webhook/alert")
            channel = str(body.get("channel") or ("webhook" if is_alert else "http"))
            user = str(body.get("user") or ("alert" if is_alert else "anonymous"))
            text = _extract_payload_text(body, query_params, include_extra=is_alert)
            session = str(body.get("session") or "")
            user_id = str(body.get("user_id") or "")
            new_session = bool(body.get("new_session"))
            notify = bool(is_alert or body.get("notify") or body.get("broadcast"))
            raw_attachments = body.get("attachments") or []

            # Workspace/persona are ids only — a path or label is a client bug.
            ws = None
            ws_ref = str(body.get("workspace") or "").strip()
            if ws_ref:
                try:
                    ws = resolve_workspace_id(ws_ref)
                except ValueError as exc:
                    self.send_error(400, str(exc))
                    return
            persona_ref = str(body.get("persona") or "").strip()
            if persona_ref and not valid_id(persona_ref):
                self.send_error(400, f"invalid persona id: {persona_ref!r}")
                return

            turn_kwargs = {
                "channel": channel,
                "user": user,
                "message": text,
                "session": session,
                "user_id": user_id,
                "new_session": new_session,
            }
            for key in ("provider", "model", "persona", "project", "workspace", "workdir"):
                val = str(body.get(key) or "").strip()
                if val:
                    turn_kwargs[key] = val
            attachment_notes: list[str] = []
            if raw_attachments:
                from .attachments import AttachmentRejected, materialize_for_turn

                try:
                    turn_kwargs["attachments"] = materialize_for_turn(
                        raw_attachments,
                        config=self._cfg(),
                        workspace_id=ws.id if ws is not None else "",
                        notes=attachment_notes,
                    )
                except AttachmentRejected:
                    self.send_error(400, "attachment path outside allowed directory")
                    return
                if attachment_notes:
                    from .attachments import apply_notes

                    turn_kwargs["message"] = apply_notes(text, attachment_notes)
                if not turn_kwargs["attachments"] and not text.strip():
                    self.send_error(400, "; ".join(attachment_notes) or "attachment rejected")
                    return
            try:
                result = self._run_turn(turn_kwargs)
            except Exception as exc:
                log.exception("turn failed")
                self.send_error(500, str(exc))
                return

            notified = False
            delivered: list[int] = []
            skipped: list[str] = []
            cfg = self._cfg()
            if notify and cfg.telegram_bot_token and cfg.telegram_allowed_chat_ids:
                from .telegram_adapter import deliver_html, resolve_delivery
                from .telegram_format import extract_traces, format_telegram_html

                spec = str(body.get("deliver") or body.get("deliver_to") or "").strip()
                if spec:
                    targets, skipped = resolve_delivery(spec, cfg)
                else:
                    targets = [int(cfg.telegram_allowed_chat_ids[0])]
                if targets:
                    _, err_traces = extract_traces(result.stderr or "")
                    html = format_telegram_html(result.reply, err_traces)
                for chat in targets:
                    try:
                        deliver_html(cfg.telegram_bot_token, chat, html)
                        delivered.append(chat)
                    except Exception as exc:
                        log.warning("failed to broadcast notification to telegram %s: %s", chat, exc)
                notified = bool(delivered)

            from .attachments import apply_notes

            payload = {
                "reply": apply_notes(result.reply, attachment_notes),
                "session": result.session,
                "user_id": result.user_id,
                "alias": result.alias,
                "returncode": result.returncode,
                "notified": notified,
            }
            if delivered or skipped:
                payload["delivered"] = delivered
                payload["skipped"] = skipped
            self._write_json(payload, 200 if result.returncode == 0 else 502)
        finally:
            # Lease release covers the ENTIRE post-acquire tail — early
            # 400 validation exits included (agents-relay#44).
            if path == "/v1/turn" and turn_lease_release_hook is not None:
                try:
                    turn_lease_release_hook(body, self._auth_ctx())
                except Exception:  # noqa: BLE001
                    pass

    def _ws_and_persona(self, parts: list[str]):
        """(Workspace, persona id) from route segments; ids only (C22 rules)."""
        from .workspaces import get_workspace

        ws_id, persona = parts[3], parts[5] if len(parts) > 5 else ""
        for label, ref in (("workspace", ws_id), ("persona", persona)):
            if ref and not valid_id(ref):
                raise ValueError(f"invalid {label} id: {ref!r}")
        ws = get_workspace(ws_id)
        return ws, persona

    def _handle_persona_post(self, parts: list[str], query_params: dict) -> None:
        body = self._read_json()
        if body is None:
            return
        try:
            ws, persona = self._ws_and_persona(parts)
        except KeyError:
            self.send_error(404, "unknown workspace")
            return
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        from .workspaces import create_persona, create_session, list_sessions

        if len(parts) == 5:
            # POST /v1/workspaces/<ws>/personas {id, ...manifest fields}
            pid = str(body.get("id") or "").strip()
            if not pid or not valid_id(pid):
                self.send_error(400, "persona id is required (ids only)")
                return
            row = create_persona(ws, pid, body)
            _emit_relay_event("persona.changed", {"action": "create", "persona": pid}, workspace=ws.id)
            self._write_json({"ok": True, "persona": row}, 201)
            return
        if len(parts) == 7 and parts[6] == "sessions":
            # POST .../personas/<p>/sessions {user, channel, workdir}
            if _ws_member_body_denied(self._auth_ctx(), body):
                self.send_error(403, "ws_member cannot set workdir/policy fields")
                return
            try:
                rec = create_session(
                    ws, persona,
                    user=str(body.get("user") or body.get("user_id") or ""),
                    channel=str(body.get("channel") or ""),
                    workdir=str(body.get("workdir") or ""),
                )
            except RuntimeError as exc:
                self.send_error(501, str(exc))
                return
            _emit_relay_event(
                "session.changed",
                {"action": "create", "persona": persona, "session": str(rec.get("id") or "")},
                workspace=ws.id,
            )
            self._write_json({"ok": True, "session": rec}, 201)
            return
        if len(parts) == 7 and parts[6] == "inbox":
            # POST .../personas/<p>/inbox {title, note, user}
            title = str(body.get("title") or "").strip()
            if not title:
                self.send_error(400, "title is required")
                return
            from agents_workspace.inbox import create_task
            try:
                task = create_task(
                    ws, persona, title,
                    note=str(body.get("note") or ""),
                    user=str(body.get("user") or ""),
                    task_id=str(body.get("id") or ""),
                )
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            self._write_json({"ok": True, "task": task}, 201)
            return
        if (len(parts) == 9 and parts[6] == "inbox"
                and parts[8] == "handoff"):
            # POST .../inbox/<task>/handoff {to, note}
            to = str(body.get("to") or "").strip()
            if not to or not valid_id(to):
                self.send_error(400, "handoff 'to' persona id is required")
                return
            from agents_workspace.inbox import handoff
            try:
                task = handoff(
                    ws, parts[7], to,
                    from_persona=persona,
                    note=str(body.get("note") or ""),
                )
            except ValueError as exc:
                msg = str(exc)
                self.send_error(404 if "unknown task" in msg else 400, msg)
                return
            wake = (
                str(query_params.get("wake") or body.get("wake") or "")
                .strip().lower() in ("1", "true", "yes")
            ) or bool(task.get("wake"))
            if wake:
                from .wake import wake_task

                wake_task(ws, task, run_turn=lambda **kw: self._run_turn(kw))
                self._write_json({"ok": True, "task": task, "wake": True}, 202)
                return
            self._write_json({"ok": True, "task": task}, 200)
            return
        self.send_error(404)

    def _handle_inbox_get(self, parts: list[str]) -> None:
        try:
            ws, persona = self._ws_and_persona(parts)
        except KeyError:
            self.send_error(404, "unknown workspace")
            return
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        from agents_workspace.inbox import list_inbox

        self._write_json(
            {"ok": True, "workspace": ws.id, "persona": persona,
             "inbox": list_inbox(ws, persona)}, 200)

    def _handle_session_get(self, parts: list[str], query_params: dict) -> None:
        try:
            ws, persona = self._ws_and_persona(parts)
        except KeyError:
            self.send_error(404, "unknown workspace")
            return
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        from .workspaces import get_session, list_sessions

        if len(parts) == 7:
            user = str(query_params.get("user") or "").strip()
            include_empty = str(
                query_params.get("include_empty") or query_params.get("all") or ""
            ).strip().lower() in ("1", "true", "yes")
            try:
                rows = list_sessions(
                    ws, persona=persona, user=user, include_empty=include_empty)
            except RuntimeError as exc:
                self.send_error(501, str(exc))
                return
            self._write_json(
                {"ok": True, "workspace": ws.id, "persona": persona,
                 "sessions": rows}, 200)
            return
        sid = parts[7]
        if not valid_id(sid) and not sid.startswith(("ses_", "proc-", "telegram-", "repl-")):
            self.send_error(400, f"invalid session id: {sid!r}")
            return
        try:
            rec = get_session(ws, persona, sid)
        except RuntimeError as exc:
            self.send_error(501, str(exc))
            return
        if rec is None:
            self.send_error(404, "unknown session")
            return
        payload = {"ok": True, "session": rec}
        # history = session record + trace events + synced store msgs the
        # local trace lacks (lazy traces/store deps)
        try:
            from agents_traces.store import TraceStore
            events = [
                e.to_dict() if hasattr(e, "to_dict") else dict(e)
                for e in TraceStore(ws.traces).iter_events(session_id=sid)
            ]
        except ImportError:
            events = []
        except Exception as exc:
            log.warning("session events unavailable: %s", exc)
            events = []
        try:
            from .storeproj import union_events

            events = union_events(ws, sid, events)
        except Exception as exc:
            log.warning("synced session events unavailable: %s", exc)
        payload["events"] = events
        self._write_json(payload, 200)

    def _handle_traces(self, query_params: dict) -> None:
        """Tail the workspace's daily agents-traces JSONL files for one session.

        agents-traces appends to ``<ws>/traces/YYYY-MM-DD.jsonl`` (there is no
        per-session file), so ``session`` filters each line's ``session`` field.
        ``lines`` are the last ``limit`` matching raw JSONL lines, oldest first.
        Without ``session`` the on-disk session ids are listed instead.
        ``persona`` is a narrowing hint: events stamped with a *different*
        persona are skipped; unstamped events still match.
        """
        ws_ref = str(query_params.get("workspace") or "").strip()
        persona = str(query_params.get("persona") or "").strip()
        sid = str(query_params.get("session") or "").strip()
        for label, ref in (("workspace", ws_ref), ("persona", persona)):
            if ref and not valid_id(ref):
                self.send_error(400, f"invalid {label} id: {ref!r}")
                return
        if sid and not valid_id(sid) and not sid.startswith(
                ("ses_", "proc-", "telegram-", "repl-")):
            self.send_error(400, f"invalid session id: {sid!r}")
            return
        from .workspaces import get_workspace, select_workspace

        try:
            ws = get_workspace(ws_ref) if ws_ref else select_workspace()
        except (KeyError, ValueError):
            self.send_error(400, f"unknown workspace: {ws_ref!r}")
            return
        try:
            limit = max(1, min(int(str(query_params.get("limit") or "50")), 500))
        except ValueError:
            self.send_error(400, "limit must be numeric")
            return

        if not sid:
            root = Path(ws.personas)
            dirs = [root / persona] if persona else (
                sorted(p for p in root.iterdir() if p.is_dir())
                if root.is_dir() else []
            )
            sessions: list[str] = []
            for pdir in dirs:
                sdir = pdir / "sessions"
                if sdir.is_dir():
                    sessions.extend(f.stem for f in sdir.glob("*.json"))
            self._write_json(
                {
                    "ok": True,
                    "lines": [],
                    "sessions": sorted(sessions),
                    "note": "no session given; pass session=<id> to tail trace events",
                },
                200,
            )
            return

        tail: deque[str] = deque(maxlen=limit)
        troot = Path(ws.traces)
        if troot.is_dir():
            for path in sorted(troot.glob("*.jsonl")):
                try:
                    fh = open(path, encoding="utf-8", errors="replace")
                except OSError:
                    continue
                with fh:
                    for line in fh:
                        if sid not in line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict) or event.get("session") != sid:
                            continue
                        meta = event.get("metadata")
                        stamp = meta.get("persona") if isinstance(meta, dict) else None
                        if persona and stamp and stamp != persona:
                            continue
                        tail.append(line.rstrip("\r\n"))
        self._write_json({"ok": True, "lines": list(tail), "note": ""}, 200)

    def _handle_jobs_get(self, path: str, query_params: dict) -> None:
        """List or fetch goal-sessions (jobs) under ?workspace=<id>."""
        from agents_workspace import Host

        from .coding_jobs import get_job, list_jobs
        from .workspaces import get_workspace

        ws_ref = str(query_params.get("workspace") or "").strip()
        if not ws_ref or not valid_id(ws_ref):
            self.send_error(400, "workspace id is required")
            return
        if not Host.from_env().workspace_exists(ws_ref):
            self.send_error(404, f"unknown workspace: {ws_ref}")
            return
        ws = get_workspace(ws_ref)
        persona = str(query_params.get("persona") or "").strip()
        if persona and not valid_id(persona):
            self.send_error(400, f"invalid persona id: {persona!r}")
            return
        job_id = path.rsplit("/", 1)[-1] if "/" in path[7:] else ""
        if job_id and job_id != "jobs":
            job = get_job(ws, job_id, persona=persona)
            if job is None:
                self.send_error(404, "unknown job")
                return
            self._write_json({"ok": True, "job": job}, 200)
            return
        self._write_json(
            {"ok": True, "jobs": list_jobs(ws, persona=persona)}, 200)

    def _approvals_dir(self, query_params: dict) -> Path:
        """Host-owned ``<host>/run/approvals``. Still validates ?workspace=<id>."""
        from .state import approval_dir
        from .workspaces import get_workspace

        ws_ref = str(query_params.get("workspace") or "").strip()
        if ws_ref:
            if not valid_id(ws_ref):
                raise ValueError(f"invalid workspace id: {ws_ref!r}")
            get_workspace(ws_ref)
        return approval_dir(self._cfg())

    def _approval_bearer(self) -> str:
        auth_header = (self.headers.get("Authorization") or "").strip()
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        return ""

    def _verify_turn_approval(self, req_ws: str = "") -> tuple[str, dict[str, Any]] | None:
        from .approvals import verify_approval_token

        bearer = self._approval_bearer()
        tok_rec = verify_approval_token(bearer)
        if tok_rec is None:
            self._write_json({"ok": False, "error": "unauthorized"}, 401)
            return None
        tok_ws = str(tok_rec.get("workspace") or "").strip()
        if req_ws and req_ws != tok_ws:
            self._write_json({"ok": False, "error": "workspace mismatch"}, 403)
            return None
        return bearer, tok_rec

    def _handle_approval_request(self, query_params: dict) -> None:
        """``POST /v1/approvals/request`` — create host approval + dispatch prompt."""
        body = self._read_json()
        if body is None:
            return
        req_obj = body.get("request") if isinstance(body.get("request"), dict) else body
        req_ws = str(
            body.get("workspace")
            or req_obj.get("workspace")
            or query_params.get("workspace")
            or ""
        ).strip()
        verified = self._verify_turn_approval(req_ws=req_ws)
        if verified is None:
            return
        _bearer, tok_rec = verified

        from .approvals import (
            APPROVAL_ID_RE,
            approval_keyboard,
            approval_request_hash,
            create_pending,
            format_approval_html,
            read_approval,
            resolve_approver_chat,
            sanitize_preview,
        )

        ident = str(req_obj.get("id") or body.get("id") or "").strip()
        if not ident or not APPROVAL_ID_RE.match(ident):
            self._write_json({"ok": False, "error": "invalid approval id"}, 400)
            return

        cfg = self._cfg()
        tok_ws = str(tok_rec.get("workspace") or "").strip()
        tok_hash = str(tok_rec.get("token_hash") or "").strip()
        req_hash = approval_request_hash(req_obj)
        existing = read_approval(cfg, ident)
        if existing is not None:
            ex_tok = str(existing.get("token_hash") or "").strip()
            if not ex_tok or not hmac.compare_digest(ex_tok, tok_hash):
                self._write_json({"ok": False, "error": "approval id conflict"}, 403)
                return
            ex_hash = str(existing.get("request_hash") or "")
            if not ex_hash:
                ex_req = existing.get("request")
                if isinstance(ex_req, dict):
                    ex_hash = approval_request_hash(ex_req)
            # Fail closed: an unverifiable prior request is a conflict,
            # never a silent pass (agents-relay#44).
            if not ex_hash or not hmac.compare_digest(ex_hash, req_hash):
                self._write_json(
                    {"ok": False,
                     "error": "approval request changed"}, 409)
                return
            ex_ws = str(existing.get("workspace") or "").strip()
            if (ex_ws or tok_ws) and ex_ws != tok_ws:
                self._write_json({"ok": False, "error": "workspace mismatch"}, 403)
                return
            out: dict[str, Any] = {
                "ok": True,
                "id": ident,
                "status": str(existing.get("status") or "pending"),
                "note": str(existing.get("note") or ""),
                "workspace": ex_ws or tok_ws,
            }
            if out["status"] == "approved":
                if existing.get("remember"):
                    out["remember"] = True
                ap = str(existing.get("allow_pattern") or "").strip()
                if ap:
                    out["allow_pattern"] = ap
            self._write_json(out, 200)
            return

        # cfg.approver pins the decision target — a caller-supplied
        # "user" must never redirect the prompt (agents-relay#41).
        raw_user = "" if cfg.approver else str(
            body.get("user") or req_obj.get("user") or "")
        chat_id, _ = resolve_approver_chat(
            raw_user,
            approver=cfg.approver,
            allowed=cfg.telegram_allowed_chat_ids,
        )
        tg_chat = 0
        send_tg = False
        if cfg.telegram_bot_token and chat_id is not None:
            from .telegram_adapter import chat_allowed

            if chat_allowed(chat_id, cfg):
                tg_chat = chat_id
                send_tg = True

        req_payload = dict(req_obj)
        req_payload["id"] = ident
        if tok_ws:
            req_payload["workspace"] = tok_ws
        if "preview" in req_payload:
            clean_prev = sanitize_preview(req_payload.get("preview"))
            if clean_prev is not None:
                req_payload["preview"] = clean_prev
            else:
                req_payload.pop("preview", None)
        rec = create_pending(
            cfg,
            tg_chat,
            req_payload,
            workspace=tok_ws,
            token_hash=tok_hash,
        )
        if send_tg:
            try:
                from .telegram_adapter import send_message

                send_message(
                    cfg.telegram_bot_token,
                    tg_chat,
                    format_approval_html(req_payload),
                    parse_mode="HTML",
                    reply_markup=approval_keyboard(ident),
                )
            except Exception:
                log.warning("approval telegram prompt failed for %s", ident)

        _emit_relay_event("approval.changed", {"action": "pending", "id": ident}, workspace=tok_ws)
        self._write_json(
            {
                "ok": True,
                "id": ident,
                "status": str(rec.get("status") or "pending"),
                "workspace": tok_ws,
            },
            200,
        )

    def _handle_approval_wait(self, ident: str, query_params: dict) -> None:
        """``GET /v1/approvals/<id>/wait`` — wait on host approval decision."""
        req_ws = str(query_params.get("workspace") or "").strip()
        verified = self._verify_turn_approval(req_ws=req_ws)
        if verified is None:
            return
        bearer, tok_rec = verified

        from .approvals import (
            APPROVAL_ID_RE,
            expire_pending,
            read_approval,
            verify_approval_token,
        )

        if not APPROVAL_ID_RE.match(ident):
            self._write_json({"ok": False, "error": "invalid approval id"}, 400)
            return

        cfg = self._cfg()
        rec = read_approval(cfg, ident)
        if rec is None:
            self._write_json({"ok": False, "error": "unknown approval"}, 404)
            return

        tok_ws = str(tok_rec.get("workspace") or "").strip()
        tok_hash = str(tok_rec.get("token_hash") or "").strip()
        ap_ws = str(rec.get("workspace") or "").strip()
        ap_tok = str(rec.get("token_hash") or "").strip()
        if (ap_ws or tok_ws) and ap_ws != tok_ws:
            self._write_json({"ok": False, "error": "workspace mismatch"}, 403)
            return
        if not ap_tok or not hmac.compare_digest(ap_tok, tok_hash):
            self._write_json({"ok": False, "error": "forbidden"}, 403)
            return

        try:
            wait_sec = max(0.0, min(600.0, float(query_params.get("timeout") or "30")))
        except ValueError:
            wait_sec = 30.0
        finalize = str(query_params.get("finalize") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        deadline = time.monotonic() + wait_sec
        while True:
            cur = read_approval(cfg, ident) or {}
            status = str(cur.get("status") or "")
            if status in ("approved", "denied", "timeout"):
                out_wait: dict[str, Any] = {
                    "ok": True,
                    "id": ident,
                    "status": status,
                    "note": str(cur.get("note") or status),
                }
                if status == "approved":
                    if cur.get("remember") is True:
                        out_wait["remember"] = True
                    pat = str(cur.get("allow_pattern") or "").strip()
                    if pat:
                        out_wait["allow_pattern"] = pat
                self._write_json(out_wait, 200)
                return
            if verify_approval_token(bearer) is None:
                self._write_json(
                    {"ok": False, "error": "approval token expired or revoked"},
                    401,
                )
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(min(0.1, max(0.01, deadline - time.monotonic())))

        if finalize:
            cur = expire_pending(cfg, ident)
            status = str(cur.get("status") or "timeout")
            self._write_json(
                {
                    "ok": True,
                    "id": ident,
                    "status": status,
                    "note": str(cur.get("note") or status),
                },
                200,
            )
            return
        self._write_json({"ok": True, "id": ident, "status": "pending", "note": "pending"}, 200)

    def _handle_approvals_get(self, query_params: dict) -> None:
        from .state import read_json

        ctx = self._approval_actor_allowed()
        if not ctx:
            return
        try:
            adir = self._approvals_dir(query_params)
        except KeyError:
            self.send_error(404, "unknown workspace")
            return
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        ws_ref = str(query_params.get("workspace") or "").strip()
        allow_ws = (ctx.get("allowlist") or frozenset()
                    ) if ctx.get("kind") == "device" else frozenset()
        pending: list[tuple[float, dict]] = []
        if adir.is_dir():
            for path in adir.glob("*.json"):
                rec = read_json(path)
                if not rec or rec.get("status") != "pending":
                    continue
                req = rec.get("request")
                if not isinstance(req, dict):
                    req = {}
                rec_ws = str(rec.get("workspace") or req.get("workspace") or "").strip()
                # Fail closed: a ws-scoped filter hides unscoped records
                # too — a missing rec_ws must never widen visibility
                # (agents-relay#44).
                if ws_ref and rec_ws != ws_ref:
                    continue
                if allow_ws and rec_ws not in allow_ws:
                    continue
                try:
                    created = float(rec.get("created_at") or 0)
                except (TypeError, ValueError):
                    created = 0.0
                row = {
                    "id": str(rec.get("id") or path.stem),
                    "tool": str(req.get("tool") or ""),
                    "summary": str(req.get("summary") or ""),
                    "session": str(rec.get("session") or req.get("session") or ""),
                    "device": str(rec.get("device") or req.get("device") or ""),
                }
                if rec_ws:
                    row["workspace"] = rec_ws
                if isinstance(req.get("preview"), dict):
                    row["preview"] = req["preview"]
                pending.append((created, row))
        rows = [row for _, row in sorted(pending, key=lambda item: item[0])]
        self._write_json({"ok": True, "approvals": rows}, 200)

    def _approval_actor_allowed(self) -> dict:
        """Approval decisions belong to operators only: admin/cookie
        callers or role=client devices. host/ws_member tokens that did
        not create the record never decide it (agents-relay#41)."""
        ctx = self._auth_ctx()
        kind = ctx.get("kind")
        if kind in ("admin", "cookie"):
            return ctx
        if kind == "device" and ctx.get("role") == "client":
            return ctx
        self.send_error(403, "approvals are operator-only")
        return {}

    def _handle_approval_post(self, ident: str, query_params: dict) -> None:
        """Resolve a pending approval through the host-owned approval file."""
        ctx = self._approval_actor_allowed()
        if not ctx:
            return
        body = self._read_json()
        if body is None:
            return
        allow = body.get("allow")
        if not isinstance(allow, bool):
            self.send_error(400, "allow must be true or false")
            return
        remember = bool(body.get("remember")) if allow else False
        allow_pattern = str(body.get("allow_pattern") or "").strip()[:200] if allow else ""
        cfg = self._cfg()
        ws_ref = str(query_params.get("workspace") or "").strip()
        if ws_ref:
            try:
                self._approvals_dir(query_params)
            except KeyError:
                self.send_error(404, "unknown workspace")
                return
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
        from .approvals import decide_approval, read_approval

        rec = read_approval(cfg, ident)
        rec_ws = ""
        if rec is not None:
            rec_ws = str(rec.get("workspace") or "").strip()
        # Fail closed on BOTH sides: a rec without a workspace stamp can
        # never satisfy a ws-scoped decide (agents-relay#44).
        if ws_ref and rec is not None and rec_ws != ws_ref:
            self.send_error(403, "workspace mismatch")
            return
        if rec is not None and ctx.get("kind") == "device":
            allow_ws = ctx.get("allowlist") or frozenset()
            if allow_ws and rec_ws not in allow_ws:
                self.send_error(403, "workspace not in allowlist")
                return
        try:
            code, note = decide_approval(
                cfg,
                ident,
                allow=allow,
                remember=remember,
                allow_pattern=allow_pattern,
            )
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        except Exception as exc:
            log.exception("approval decide failed")
            self.send_error(500, str(exc))
            return
        if code != 0:
            self.send_error(400 if note == "invalid approval id" else 404, note)
            return
        _emit_relay_event(
            "approval.changed",
            {"action": "decide", "id": ident, "allow": allow, "remember": remember},
            workspace=rec_ws or ws_ref,
        )
        resp_body: dict[str, Any] = {"ok": True, "id": ident, "status": note, "remember": remember}
        if allow_pattern:
            resp_body["allow_pattern"] = allow_pattern
        self._write_json(resp_body, 200)

    def _handle_stream(self, body: dict) -> None:
        params = turn_params_from_body(body)
        if params.workspace:
            try:
                resolve_workspace_id(params.workspace)
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
        if params.persona and not valid_id(params.persona):
            self.send_error(400, f"invalid persona id: {params.persona!r}")
            return
        raw_attachments = body.get("attachments") or []
        attachment_notes: list[str] = []
        if raw_attachments:
            from .attachments import AttachmentRejected, apply_notes, materialize_for_turn

            try:
                paths = materialize_for_turn(
                    raw_attachments,
                    config=self._cfg(),
                    workspace_id=params.workspace,
                    notes=attachment_notes,
                )
            except AttachmentRejected:
                self.send_error(400, "attachment path outside allowed directory")
                return
            original = params.message
            params = replace(
                params,
                attachments=tuple(paths),
                message=apply_notes(original, attachment_notes),
            )
            if not paths and not original.strip():
                self.send_error(400, "; ".join(attachment_notes) or "attachment rejected")
                return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # keep-alive makes BaseHTTPRequestHandler wait for another request, so the body never ends.
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            if attachment_notes:
                self.wfile.write(_sse_event({
                    "type": "delta",
                    "text": "\n\n".join(attachment_notes) + "\n\n",
                }))
                self.wfile.flush()
            for event in iter_loop_stream(params, config=self._cfg()):
                payload = {"type": event.type}
                if event.type == "delta":
                    payload["text"] = event.text
                else:
                    payload.update(
                        {
                            "session": event.session,
                            "user_id": event.user_id,
                            "alias": event.alias,
                            "returncode": event.returncode,
                        }
                    )
                self.wfile.write(_sse_event(payload))
                self.wfile.flush()
        except BrokenPipeError:
            log.info("stream client disconnected")

    def _handle_webhook(self, source: str, body: dict, raw: bytes) -> None:
        secret_header = self.headers.get("X-Webhook-Secret") or self.headers.get("Webhook-Secret") or ""
        notify = str(body.get("notify_turn", "true")).lower() not in ("0", "false", "no")
        result = handle_webhook(
            source,
            body,
            raw_body=raw,
            secret_header=secret_header,
            config=self._cfg(),
            notify_turn=notify,
        )
        status = 200 if result.get("ok") else 401
        self._write_json(result, status)

    def do_GET(self) -> None:
        path, query_params = _parse_query(self.path)
        cfg = self._cfg()
        if self._gate_ws_member("GET", path, query_params):
            return
        if path == "/v1/auth/status":
            handle_status(self)
            return
        if path == "/v1/events":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            ws_filter = str(query_params.get("workspace") or "").strip()
            if ws_filter and not valid_id(ws_filter):
                self.send_error(400, f"invalid workspace id: {ws_filter!r}")
                return
            from .events import handle_events_sse

            hb = float(getattr(self.server, "events_heartbeat_sec", 25.0))
            handle_events_sse(
                self, workspace=ws_filter, heartbeat_sec=hb,
                tag=str(self._auth_ctx().get("device_id") or ""))
            return
        if path == "/v1/workspaces":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            from .workspaces import list_workspaces, workspace_row

            rows = [workspace_row(ws) for ws in list_workspaces()]
            self._write_json({"ok": True, "workspaces": rows}, 200)
            return
        if path == "/v1/personas":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            from .workspaces import get_workspace, list_personas, select_workspace

            ws_ref = str(query_params.get("workspace") or "").strip()
            try:
                ws = get_workspace(ws_ref) if ws_ref else select_workspace()
            except (KeyError, ValueError) as exc:
                self.send_error(400, f"unknown workspace: {ws_ref!r}")
                return
            self._write_json(
                {"ok": True, "workspace": ws.id,
                 "personas": list_personas(ws)}, 200)
            return
        if path == "/v1/jobs" or path.startswith("/v1/jobs/"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_jobs_get(path, query_params)
            return
        if path == "/v1/traces":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_traces(query_params)
            return
        if path == "/v1/runs/stream":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            # ws gate BEFORE dispatch — an unscoped attach must never
            # leak another workspace's run events (agents-relay#44).
            rctx = self._auth_ctx()
            if not _ws_allowed(
                    rctx, str(query_params.get("workspace") or "")):
                self.send_error(403, "workspace not in allowlist")
                return
            # re-attach follows the run's executing host, not the local
            # (empty) _ACTIVE table (klanker#148)
            if remote_query_dispatch is not None and remote_query_dispatch(
                    self, path, query_params):
                return
            handle_runs_attach(
                self, query_params,
                tag=str(rctx.get("device_id") or ""))
            return
        if path == "/v1/push/vapid-key":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            handle_vapid_key(self, self._cfg())
            return
        if path == "/v1/approvals":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_approvals_get(query_params)
            return
        if path == "/v1/providers":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            try:
                from .providers_api import provider_rows
                rows = provider_rows(self._cfg())
            except Exception as exc:
                self.send_error(500, f"providers unavailable: {exc}")
                return
            self._write_json({"ok": True, "providers": rows}, 200)
            return
        if path == "/v1/host":
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            try:
                from agents_workspace import Host

                host_obj = Host.from_env()
                hid = str(host_obj.host_id() or "")
                lbl_fn = getattr(host_obj, "host_label", None)
                hlbl = str(lbl_fn() if callable(lbl_fn) else "") or hid
            except Exception as exc:
                self.send_error(500, f"host info unavailable: {exc}")
                return
            self._write_json({"ok": True, "host_id": hid, "host_label": hlbl}, 200)
            return
        probe_parts = path.split("/")
        if (len(probe_parts) == 5 and probe_parts[1] == "v1"
                and probe_parts[2] == "providers" and probe_parts[4] == "probe"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            from .providers_api import ProviderError, probe_provider

            try:
                out = probe_provider(self._cfg(), probe_parts[3])
            except ProviderError as exc:
                self.send_error(exc.status, exc.message)
                return
            except Exception as exc:
                self.send_error(500, f"probe failed: {exc}")
                return
            self._write_json({"ok": True, "provider": probe_parts[3], **out}, 200)
            return
        if path == "/v1/favorites":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            from .providers_api import get_favorites

            self._write_json({"ok": True, "favorites": get_favorites()}, 200)
            return
        if path == "/v1/model":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            try:
                ws_ref = str(query_params.get("workspace") or "").strip()
                ws = resolve_workspace_id(ws_ref) if ws_ref else None
                from runner.model import selected
                pick = dict(selected(ws))
                scope = "workspace"
                session = str(query_params.get("session") or "").strip()
                if session:
                    # Session pick wins over the workspace pick; the workspace
                    # row stays the fallback for new chats.
                    from .providers_api import session_pick

                    sp = session_pick(session)
                    if sp["provider"]:
                        pick = sp
                        scope = "session"
                from .providers_api import provider_rows
                rows = provider_rows(
                    self._cfg(),
                    refresh=str(query_params.get("refresh") or "") == "1")
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except Exception as exc:
                self.send_error(500, f"model unavailable: {exc}")
                return
            self._write_json({"ok": True, "provider": pick.get("provider") or "",
                              "model": pick.get("model") or "", "scope": scope,
                              "providers": rows}, 200)
            return
        if path == "/v1/remotes":
            # Mount table is operator surface — device tokens never read
            # it either (agents-relay#41).
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            from .remotes import list_public

            self._write_json({"ok": True, "remotes": list_public(self._cfg())}, 200)
            return
        if path == "/v1/hub/stream":
            # Per-device SSE channel (agents-relay#40): device_token auth.
            did = _check_device(self)
            if not did or did == "__admin__":
                self.send_error(401, "unauthorized")
                return
            try:
                since = int(query_params.get("since") or 0)
            except ValueError:
                since = 0
            from .channel import handle_hub_stream

            hb = float(getattr(self.server, "events_heartbeat_sec", 25.0))
            handle_hub_stream(self, did, since=max(0, since), heartbeat_sec=hb)
            return
        if path == "/v1/hub/devices":
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            from .channel import hub
            from .devices import list_public

            connected = set(hub().connected_devices())
            rows = [
                {**row, "connected": row["id"] in connected}
                for row in list_public()
            ]
            self._write_json({"ok": True, "devices": rows}, 200)
            return
        if path == "/v1/hub/leases":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            from .leases import hub_leases

            ctx = self._auth_ctx()
            rows = hub_leases().snapshot()
            if ctx.get("kind") == "device":
                # Every device reads only workspaces that actually
                # admit it — ws.allows_device is the trust boundary,
                # the allowlist narrows ws_member reads further
                # (agents-relay#44).
                did = str(ctx.get("device_id") or "")
                allow = (set(ctx.get("allowlist") or ())
                         if ctx.get("role") == "ws_member" else None)
                try:
                    from .workspaces import get_workspace
                except ImportError:
                    get_workspace = None

                def _lease_visible(s: dict) -> bool:
                    ws_name = str(s.get("ws") or "")
                    if allow is not None and ws_name not in allow:
                        return False
                    if get_workspace is None:
                        return False
                    try:
                        return bool(get_workspace(ws_name)
                                    .allows_device(did))
                    except (KeyError, ValueError, OSError):
                        return False

                rows = [s for s in rows if _lease_visible(s)]
            self._write_json({"ok": True, "leases": rows}, 200)
            return
        if path == "/v1/discover":
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            from .discovery import discover_peers

            self._write_json({"ok": True, "peers": discover_peers()}, 200)
            return
        if path == "/v1/capabilities":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            from .store_api import relay_capabilities

            self._write_json({"ok": True, **relay_capabilities()}, 200)
            return
        # GET /v1/remote/<id>/<upstream-path> — authenticated remote mount.
        if path.startswith("/v1/remote/"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            if not _proxy_allowed(self._auth_ctx()):
                self.send_error(403, "remote proxy denied")
                return
            parts = path.split("/", 4)
            rid = parts[3] if len(parts) > 3 else ""
            if not valid_id(rid):
                self.send_error(400, f"invalid remote id: {rid!r}")
                return
            from .remotes import handle_proxy

            handle_proxy(self, rid, "/" + parts[4] if len(parts) > 4 else "/")
            return
        parts = path.split("/")
        # GET /v1/approvals/<id>/wait — turn-authenticated via Bearer AGENTS_APPROVAL_TOKEN
        if (len(parts) == 5 and parts[1] == "v1"
                and parts[2] == "approvals" and parts[4] == "wait"):
            self._handle_approval_wait(parts[3], query_params)
            return
        # GET /v1/workspaces/<ws>/hosts — member presence + active
        # leases for the host picker (agents-relay#43): on the hub this
        # aggregates sync_hosts ∩ connected devices + hub_leases rows;
        # a member device sees only its own allowlisted workspaces.
        if (len(parts) == 5 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "hosts"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            ws_ref = parts[3]
            if not valid_id(ws_ref):
                self.send_error(400, f"invalid workspace id: {ws_ref!r}")
                return
            ctx = self._auth_ctx()
            if ctx.get("role") == "ws_member" \
                    and not _ws_allowed(ctx, ws_ref):
                self.send_error(403, "workspace not in allowlist")
                return
            from .workspaces import get_workspace

            try:
                ws = get_workspace(ws_ref)
            except KeyError:
                self.send_error(404, "unknown workspace")
                return
            if ctx.get("kind") == "device":
                # EVERY device caller (host/client/ws_member) must be on
                # the ws sync allowlist — membership in another ws's
                # sync_hosts grants nothing here (agents-relay#45).
                did = str(ctx.get("device_id") or "")
                try:
                    admitted = bool(ws.allows_device(did))
                except Exception:  # noqa: BLE001 - fail closed
                    admitted = False
                if not admitted:
                    self.send_error(
                        403, "device not admitted to workspace")
                    return
            fn = getattr(self.server, "spoke_hosts_fn", None) \
                or spoke_hosts_fn
            self._write_json(
                _ws_hosts_payload(ws, ctx, spoke_hosts_fn=fn), 200)
            return
        # GET /v1/hub/links — live paired-hub state for the link
        # manager (agents-relay#43). Public metadata only, never tokens.
        if path == "/v1/hub/links":
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            fn = getattr(self.server, "hub_links_fn", None) \
                or hub_links_fn
            if fn is None:
                self._write_json({"ok": True, "links": []}, 200)
                return
            try:
                out = fn() or {}
            except Exception as exc:  # noqa: BLE001
                self.send_error(500, f"hub links failed: {exc}")
                return
            self._write_json({"ok": True, "links": out.get("links") or []},
                             200)
            return
        # GET /v1/workspaces/<ws>/inbox — team feed: counts + recent handoffs
        if (len(parts) == 5 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "inbox"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            ws_ref = parts[3]
            if not valid_id(ws_ref):
                self.send_error(400, f"invalid workspace id: {ws_ref!r}")
                return
            from .workspaces import get_workspace

            try:
                ws = get_workspace(ws_ref)
            except KeyError:
                self.send_error(404, "unknown workspace")
                return
            from .wake import team_inbox

            self._write_json(
                {"ok": True, "workspace": ws.id, **team_inbox(ws)}, 200)
            return
        # GET /v1/workspaces/<ws>/store/items
        if (len(parts) == 6 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "store"
                and parts[5] == "items"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            ws_ref = parts[3]
            if not valid_id(ws_ref):
                self.send_error(400, f"invalid workspace id: {ws_ref!r}")
                return
            from .workspaces import get_workspace
            from .store_api import handle_store_items

            try:
                ws = get_workspace(ws_ref)
            except KeyError:
                self.send_error(404, "unknown workspace")
                return
            status_code, payload = handle_store_items(ws, query_params)
            self._write_json(payload, status_code)
            return
        # GET /v1/workspaces/<ws>/store/pane/<entity_id>
        if (len(parts) == 7 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "store"
                and parts[5] == "pane"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            ws_ref = parts[3]
            if not valid_id(ws_ref):
                self.send_error(400, f"invalid workspace id: {ws_ref!r}")
                return
            from .workspaces import get_workspace
            from .store_api import handle_store_pane

            try:
                ws = get_workspace(ws_ref)
            except KeyError:
                self.send_error(404, "unknown workspace")
                return
            status_code, payload = handle_store_pane(ws, parts[6], query_params)
            self._write_json(payload, status_code)
            return
        # GET /v1/workspaces/<ws>/personas/<p>/inbox
        if (len(parts) == 7 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "personas"
                and parts[6] == "inbox"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_inbox_get(parts)
            return
        # GET /v1/workspaces/<ws>/personas/<p>/sessions[/<s>]
        if (len(parts) in (7, 8) and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "personas"
                and parts[6] == "sessions"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            self._handle_session_get(parts, query_params)
            return
        if path in ("/health", "/"):
            if path == "/" and web_enabled(cfg):
                if str(query_params.get("token") or "").strip() and handle_token_query(
                    self, str(query_params["token"])
                ):
                    return
                if serve_static(self, "index.html"):
                    return
            body: dict[str, Any] = {"ok": True}
            if path == "/health":
                body["service"] = "agents-relay"
                body["version"] = __version__
                try:
                    body["port"] = int(self.server.server_address[1])
                except (AttributeError, IndexError, TypeError, ValueError):
                    pass
                # host_id proves a live serve belongs to this <host>; never
                # expose host filesystem paths here (public probe).
                try:
                    from agents_workspace import Host

                    host_obj = Host.from_env()
                    body["host_id"] = host_obj.host_id()
                    lbl_fn = getattr(host_obj, "host_label", None)
                    if callable(lbl_fn):
                        body["host_label"] = lbl_fn()
                except Exception:
                    pass
                klanker_version = getattr(self, "klanker_version", "") or getattr(
                    self._cfg(), "klanker_version", "")
                if klanker_version:
                    body["klanker_version"] = klanker_version
            self._write_json(body, 200)
            return
        if web_enabled(cfg) and serve_static(self, path):
            return
        if path == "/v1/a2a/peers":
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            proj = query_params.get("project", "")
            result = list_peers(project=proj, config=self._cfg())
            self._write_json(result, 200 if result.get("ok", True) else 502)
            return
        self.send_error(404)

    def do_PATCH(self) -> None:
        path, query_params = _parse_query(self.path)
        if self._gate_ws_member("PATCH", path, query_params):
            return
        parts = path.split("/")
        # PATCH /v1/workspaces/<id> — per-workspace separation settings.
        if (len(parts) == 4 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[3]):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            ws_id = parts[3]
            if not valid_id(ws_id):
                self.send_error(400, f"invalid workspace id: {ws_id!r}")
                return
            ctx = self._auth_ctx()
            if _device_admin_denied(ctx):
                self.send_error(403, "workspace admin is operator-only")
                return
            body = self._read_json()
            if body is None:
                return
            if _ws_member_body_denied(ctx, body):
                self.send_error(403, "ws_member cannot set sync/policy fields")
                return
            from .workspaces import patch_workspace

            try:
                row = patch_workspace(ws_id, body)
            except KeyError:
                self.send_error(404, "unknown workspace")
                return
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            # workspace.changed is emitted once inside patch_workspace
            # (canonical "workspace" key — agents-relay#45).
            self._write_json({"ok": True, "workspace": row}, 200)
            return
        if path == "/v1/host":
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            body = self._read_json()
            if body is None:
                return
            if not isinstance(body.get("label"), str):
                self.send_error(400, "label string is required")
                return
            try:
                from agents_workspace import Host

                host_obj = Host.from_env()
                set_fn = getattr(host_obj, "set_host_label", None)
                if not callable(set_fn):
                    self.send_error(501, "host_label unsupported by agents-workspace")
                    return
                clean = set_fn(body["label"])
                hid = str(host_obj.host_id() or "")
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except Exception as exc:
                self.send_error(500, f"host label update failed: {exc}")
                return
            _emit_relay_event("host.changed", {"host_id": hid, "host_label": clean})
            self._write_json({"ok": True, "host_id": hid, "host_label": clean}, 200)
            return
        self.send_error(404)

    def do_DELETE(self) -> None:
        path, query_params = _parse_query(self.path)
        if self._gate_ws_member("DELETE", path, query_params):
            return
        parts = path.split("/")
        # DELETE /v1/providers/<name> — file-managed manifests only.
        if (len(parts) == 4 and parts[1] == "v1"
                and parts[2] == "providers" and parts[3]):
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            from .providers_api import ProviderError, delete_provider

            try:
                delete_provider(parts[3])
            except ProviderError as exc:
                self.send_error(exc.status, exc.message)
                return
            except Exception as exc:
                self.send_error(500, f"provider delete failed: {exc}")
                return
            _emit_relay_event("provider.changed", {"action": "delete", "provider": parts[3]})
            self._write_json({"ok": True, "provider": parts[3]}, 200)
            return
        # DELETE /v1/remotes/<id> — drop a remote mount (never its secret elsewhere).
        if (len(parts) == 4 and parts[1] == "v1"
                and parts[2] == "remotes" and parts[3]):
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            from .remotes import delete_remote

            if not delete_remote(self._cfg(), parts[3]):
                self.send_error(404, "unknown remote")
                return
            _emit_relay_event("remote.changed", {"action": "delete", "id": parts[3]})
            self._write_json({"ok": True, "remote": parts[3]}, 200)
            return
        # DELETE /v1/hub/devices/<id> — revoke a paired device (agents-relay#40).
        if (len(parts) == 5 and parts[1] == "v1"
                and parts[2] == "hub" and parts[3] == "devices" and parts[4]):
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            from .devices import REVOCATION_LOCK, revoke_device

            # REVOCATION_LOCK serializes (revoke + disconnect + remote
            # delete) against the channel auto-register race in
            # handle_hub_stream — a revoked device can never resurrect
            # its dev-<id> row (agents-relay#44).
            with REVOCATION_LOCK:
                if not revoke_device(parts[4]):
                    self.send_error(404, "unknown device")
                    return
                try:
                    from .channel import hub

                    hub().disconnect(parts[4])  # drop any live stream too
                    hub().drop_presence(parts[4])  # and its stale row
                    from .remotes import delete_remote

                    delete_remote(self._cfg(), f"dev-{parts[4]}")
                except Exception:
                    pass
            try:
                # Revocation is total: live leases + tagged SSE streams
                # die with the credential (agents-relay#42/#44).
                from .events import disconnect_tag
                from .leases import hub_leases
                from .web import disconnect_stream_tag

                disconnect_tag(parts[4])
                disconnect_stream_tag(parts[4])
                hub_leases().release_holder(parts[4])
            except Exception:
                pass
            _emit_relay_event("device.changed", {"action": "revoke", "id": parts[4]})
            self._write_json({"ok": True, "device": parts[4]}, 200)
            return
        # DELETE /v1/hub/links/<hub_id> — stop the live ChannelClient,
        # drop the hubs.json row + vault token (agents-relay#43).
        if (len(parts) == 5 and parts[1] == "v1"
                and parts[2] == "hub" and parts[3] == "links"
                and parts[4]):
            if not _check_secret(self, self.relay_secret, query_params,
                                 allow_device=False):
                self.send_error(401, "unauthorized")
                return
            if not valid_id(parts[4]):
                self.send_error(
                    400, f"invalid hub id: {parts[4]!r}")
                return
            fn = getattr(self.server, "hub_disconnect_fn", None) \
                or hub_disconnect_fn
            if fn is None:
                self._write_json(
                    {"ok": False,
                     "error": "hub disconnect not configured"}, 501)
                return
            try:
                out = fn(parts[4]) or {}
            except Exception as exc:  # noqa: BLE001
                self.send_error(500, f"hub disconnect failed: {exc}")
                return
            out = dict(out)
            if out.get("error"):
                self._write_json(
                    {"ok": False, **out},
                    int(out.get("status") or 502))
                return
            self._write_json({"ok": True, **out}, 200)
            return
        # DELETE /v1/remote/<id>/<upstream-path> — proxied mount.
        if path.startswith("/v1/remote/"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            rparts = path.split("/", 4)
            rid = rparts[3] if len(rparts) > 3 else ""
            if not valid_id(rid):
                self.send_error(400, f"invalid remote id: {rid!r}")
                return
            rest = "/" + rparts[4] if len(rparts) > 4 else "/"
            if not _proxy_allowed(self._auth_ctx(), rid, rest, self._cfg()):
                self.send_error(403, "remote proxy denied")
                return
            from .remotes import handle_proxy

            handle_proxy(self, rid, rest)
            return
        # DELETE /v1/workspaces/<id>[?hard=1]
        if (len(parts) == 4 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[3]):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            if _device_admin_denied(self._auth_ctx()):
                self.send_error(403, "workspace admin is operator-only")
                return
            ws_id = parts[3]
            if not valid_id(ws_id):
                self.send_error(400, f"invalid workspace id: {ws_id!r}")
                return
            from agents_workspace import Host

            try:
                out = Host.from_env().delete(
                    ws_id, hard=str(query_params.get("hard") or "") == "1")
            except ValueError as exc:
                msg = str(exc)
                code = 409 if "selected workspace" in msg else 400
                self.send_error(code, msg)
                return
            _emit_relay_event("workspace.changed", {"action": "delete", "workspace": ws_id}, workspace=ws_id)
            self._write_json({"ok": True, "workspace": ws_id,
                              "archived": str(out)}, 200)
            return
        # DELETE /v1/workspaces/<ws>/personas/<p>[?hard=1]
        if (len(parts) == 6 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "personas"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            try:
                ws, _persona = self._ws_and_persona(parts)
            except KeyError:
                self.send_error(404, "unknown workspace")
                return
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            pid = parts[5]
            if not valid_id(pid):
                self.send_error(400, f"invalid persona id: {pid!r}")
                return
            from .workspaces import delete_persona

            out = delete_persona(
                ws, pid, hard=str(query_params.get("hard") or "") == "1")
            if out is None:
                self.send_error(404, "unknown persona")
                return
            _emit_relay_event("persona.changed", {"action": "delete", "persona": pid}, workspace=ws.id)
            self._write_json({"ok": True, "persona": pid,
                              "archived": str(out)}, 200)
            return
        # DELETE /v1/workspaces/<ws>/personas/<p>/sessions/<s>
        if (len(parts) == 8 and parts[1] == "v1"
                and parts[2] == "workspaces" and parts[4] == "personas"
                and parts[6] == "sessions"):
            if not _check_secret(self, self.relay_secret, query_params):
                self.send_error(401, "unauthorized")
                return
            try:
                ws, persona = self._ws_and_persona(parts)
            except KeyError:
                self.send_error(404, "unknown workspace")
                return
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            sid = parts[7]
            if not valid_id(sid) and not sid.startswith(("ses_", "proc-", "telegram-", "repl-")):
                self.send_error(400, f"invalid session id: {sid!r}")
                return
            from .workspaces import delete_session

            try:
                removed = delete_session(ws, persona, sid)
            except RuntimeError as exc:
                self.send_error(501, str(exc))
                return
            if not removed:
                self.send_error(404, "unknown session")
                return
            _emit_relay_event(
                "session.changed",
                {"action": "delete", "persona": persona, "session": sid},
                workspace=ws.id,
            )
            self._write_json({"ok": True, "session": sid}, 200)
            return
        self.send_error(404)


def _bound_relay_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[:2]
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    if ":" in connect_host and not connect_host.startswith("["):
        return f"http://[{connect_host}]:{port}"
    return f"http://{connect_host}:{port}"


class _RelayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def shutdown(self) -> None:
        try:
            from .events import shutdown_all

            shutdown_all()
        except Exception:
            pass
        super().shutdown()

    def server_close(self) -> None:
        try:
            from .events import shutdown_all

            shutdown_all()
        except Exception:
            pass
        super().server_close()


def serve_http(config: RelayConfig, *, on_turn: Callable[..., LoopTurnResult] | None = None) -> ThreadingHTTPServer:
    try:
        from .events import reset_bus

        reset_bus()
    except Exception:
        pass
    attrs: dict = {
        "relay_secret": config.relay_secret,
        "config": config,
    }
    if on_turn is not None:
        attrs["turn_hook"] = _TurnHook(on_turn)
    try:
        from agents_workspace.runtime import sweep

        removed = sweep()
        if removed:
            log.info("swept %d stale klanker-ws containers", len(removed))
    except Exception:
        pass
    handler = type("ConfiguredRelayHandler", (TurnHandler,), attrs)
    server = _RelayHTTPServer((config.relay_host, config.relay_port), handler)
    bound_url = _bound_relay_url(server)
    effective_cfg = config if config.relay_url else replace(config, relay_url=bound_url)
    handler.config = effective_cfg
    server.relay_url = bound_url  # type: ignore[attr-defined]
    try:
        from .wake import replay_all

        if on_turn is not None:
            run_turn = attrs["turn_hook"]
        else:
            run_turn = lambda **kw: run_loop_turn(config=effective_cfg, **kw)  # noqa: E731
        replayed = replay_all(run_turn=run_turn)
        if replayed:
            log.info("replayed %d unfinished handoff wake(s)", sum(replayed.values()))
    except Exception:  # noqa: BLE001 - replay never blocks boot
        log.exception("handoff wake replay failed")
    try:
        from .acp import ensure_acp_manifest

        ensure_acp_manifest()
    except Exception:  # noqa: BLE001 - manifest drop never blocks boot
        log.exception("acp provider manifest failed")
    log.info("HTTP listening on %s:%s", config.relay_host, config.relay_port)
    return server
