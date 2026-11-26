"""Host-owned approval files and ephemeral turn approval tokens.

Pending and resolved approval records live under ``<host>/run/approvals/<id>.json``
(mode 0600 on POSIX); active per-turn approval tokens live under
``<host>/run/approvals/tokens/<token_hash>.json`` (mode 0600 on POSIX).

Inside relay-hosted turns, ``agents-relay approve`` uses ``AGENTS_RELAY_URL`` +
``AGENTS_APPROVAL_TOKEN`` to drive ``POST /v1/approvals/request`` and
``GET /v1/approvals/<id>/wait`` over HTTP without holding Telegram or relay
secrets. When relay HTTP env is unset (e.g. direct ``klanker chat`` or
``--local``), it falls back cleanly to host-side ``<host>/run/approvals/``.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import RelayConfig
from .state import approval_dir, exclusive_lock, read_json, write_json_atomic

log = logging.getLogger("agents_relay.approvals")

# UUID is 36 chars. Callback data is `ap:<id>:1` and must stay <= 64 bytes.
APPROVAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_TOKEN_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PENDING = "pending"
_APPROVED = "approved"
_DENIED = "denied"
_TIMEOUT = "timeout"

# Approval mirror hooks (klanker#148): a shared-workspace spoke mirrors
# pending asks onto the hub over the channel (approval-req) so the
# operator sees them on whichever host's UI is open; decisions ride
# back as approval-res frames. klanker serve wires both.
on_pending_fn = None   # fn(record) — freshly-pending local approval
on_decided_fn = None   # fn(record) — terminal local decision to relay

MAX_PREVIEW_BYTES = 65_536


def _clean_allow_pattern(raw: Any) -> str:
    text = str(raw or "").replace("\r", " ").replace("\n", " ").replace("\x00", "")
    return " ".join(text.split()).strip()[:200]


def sanitize_preview(raw: Any) -> dict[str, Any] | None:
    """Validate and cap rich approval preview metadata (U02, never raises)."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip()
    if kind == "command":
        raw_argv = raw.get("argv")
        argv = [str(x)[:300] for x in raw_argv[:40]] if isinstance(raw_argv, (list, tuple)) else []
        risk = str(raw.get("risk") or "medium").strip().lower()
        if risk not in ("low", "medium", "high"):
            risk = "medium"
        out: dict[str, Any] = {
            "kind": "command",
            "argv": argv,
            "cwd": str(raw.get("cwd") or "")[:500],
            "risk": risk,
        }
    elif kind == "diff":
        out = {
            "kind": "diff",
            "path": str(raw.get("path") or "")[:300],
        }
        if isinstance(raw.get("error"), str) and raw["error"].strip():
            out["error"] = raw["error"].strip()[:200]
        if raw.get("binary") is True:
            out["binary"] = True
        for key in ("old_text", "new_text", "unified"):
            val = raw.get(key)
            if isinstance(val, str):
                out[key] = val[:16_000]
        if "truncated" in raw:
            out["truncated"] = bool(raw.get("truncated"))
    else:
        return None
    while len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > MAX_PREVIEW_BYTES:
        if kind == "diff":
            longest = max(("old_text", "new_text", "unified"), key=lambda k: len(str(out.get(k) or "")))
            cur = str(out.get(longest) or "")
            if len(cur) <= 256:
                return None
            out[longest] = cur[: len(cur) // 2]
            out["truncated"] = True
        elif kind == "command" and isinstance(out.get("argv"), list) and len(out["argv"]) > 1:
            out["argv"] = out["argv"][:-1]
        else:
            return None
    return out


def _extract_opts(data: dict[str, Any], out_opts: dict[str, Any] | None) -> None:
    if out_opts is None:
        return
    if data.get("remember") is True:
        out_opts["remember"] = True
    pat = _clean_allow_pattern(data.get("allow_pattern"))
    if pat:
        out_opts["allow_pattern"] = pat


def approval_tokens_dir(config: RelayConfig | None = None) -> Path:
    return approval_dir(config) / "tokens"


def hash_approval_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_approval_token(
    workspace: str = "",
    *,
    ttl_sec: float = 3600.0,
) -> tuple[str, str]:
    """Mint an ephemeral per-turn approval token and register its hash host-side."""
    token = secrets.token_urlsafe(32)
    token_hash = hash_approval_token(token)
    now = time.time()
    ttl = max(1.0, float(ttl_sec or 3600.0))
    payload = {
        "token_hash": token_hash,
        "workspace": (workspace or "").strip(),
        "created_at": now,
        "expires_at": now + ttl,
    }
    write_json_atomic(approval_tokens_dir() / f"{token_hash}.json", payload, mode=0o600)
    return token, token_hash


def revoke_approval_token(token_or_hash: str, *, is_hash: bool = False) -> None:
    """Invalidate an active turn approval token."""
    raw = (token_or_hash or "").strip()
    if not raw:
        return
    token_hash = raw if is_hash else hash_approval_token(raw)
    if not _TOKEN_HASH_RE.fullmatch(token_hash):
        return
    try:
        (approval_tokens_dir() / f"{token_hash}.json").unlink(missing_ok=True)
    except OSError:
        pass


def verify_approval_token(token: str, *, workspace: str = "") -> dict[str, Any] | None:
    """Constant-time hash check against registered active turn tokens."""
    raw = (token or "").strip()
    if not raw:
        return None
    token_hash = hash_approval_token(raw)
    path = approval_tokens_dir() / f"{token_hash}.json"
    if not path.is_file():
        return None
    rec = read_json(path)
    if not rec:
        return None
    stored_hash = str(rec.get("token_hash") or "").strip()
    if not stored_hash or not hmac.compare_digest(
        stored_hash.encode("utf-8"), token_hash.encode("utf-8")
    ):
        return None
    try:
        expires_at = float(rec.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if time.time() > expires_at:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    req_ws = (workspace or "").strip()
    tok_ws = str(rec.get("workspace") or "").strip()
    if req_ws and req_ws != tok_ws:
        return None
    return rec


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
        write_json_atomic(path, updated, mode=0o600)
        return updated


def canonical_json(obj: Any) -> str:
    """Stable serialization for approval request hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def approval_request_hash(request: dict) -> str:
    """sha256 over the exact tool+args an approval covers — replaying a
    granted id with different arguments must fail (agents-relay#41)."""
    return hashlib.sha256(
        canonical_json(
            {"tool": request.get("tool"),
             "args": request.get("args")}).encode("utf-8")).hexdigest()


def create_pending(
    config: RelayConfig,
    chat_id: int,
    request: dict,
    *,
    workspace: str = "",
    token_hash: str = "",
    from_mirror: bool = False,
) -> dict:
    ident = str(request.get("id") or "")
    eff_ws = (workspace or str(request.get("workspace") or "")).strip()
    stored_req = dict(request)
    if "preview" in stored_req:
        clean_prev = sanitize_preview(stored_req.get("preview"))
        if clean_prev is not None:
            stored_req["preview"] = clean_prev
        else:
            stored_req.pop("preview", None)

    def _apply(current: dict) -> dict | None:
        status = str(current.get("status") or "")
        if status in (_APPROVED, _DENIED) and current.get("id") == ident:
            return None
        record: dict[str, Any] = {
            "id": ident,
            "status": _PENDING,
            "chat_id": int(chat_id),
            "workspace": eff_ws,
            "request": stored_req,
            "request_hash": approval_request_hash(stored_req),
            "note": "",
            "decided_by": None,
            "created_at": time.time(),
            "decided_at": None,
        }
        if token_hash:
            record["token_hash"] = token_hash
        return record

    updated = _mutate(config, ident, _apply)
    if updated and updated.get("status") == _PENDING:
        from .push import notify_approval_pending

        notify_approval_pending(config, {**stored_req, "id": ident}, ws_ref=eff_ws)
        if not from_mirror and on_pending_fn is not None:
            try:
                on_pending_fn(updated)
            except Exception:  # noqa: BLE001 - mirror never blocks the ask
                pass
    return updated or {}


def record_decision(
    config: RelayConfig,
    ident: str,
    *,
    status: str,
    note: str,
    decided_by: int | None,
    expected_chat_id: int | None = None,
    from_mirror: bool = False,
    remember: bool = False,
    allow_pattern: str = "",
) -> dict | None:
    """Write a terminal status if the record is still pending. None if rejected."""
    changed = False
    clean_pat = _clean_allow_pattern(allow_pattern) if (status == _APPROVED and not from_mirror) else ""
    allow_remember = bool(remember and status == _APPROVED and not from_mirror)

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
        if allow_remember:
            current["remember"] = True
        if clean_pat:
            current["allow_pattern"] = clean_pat
        changed = True
        return current

    path = approval_path(config, ident)
    if not path.is_file():
        return None
    updated = _mutate(config, ident, _apply)
    if not changed:
        return None
    if not from_mirror and on_decided_fn is not None:
        try:
            on_decided_fn(updated)
        except Exception:  # noqa: BLE001 - mirror never blocks the decision
            pass
    return updated


def read_approval(config: RelayConfig, ident: str) -> dict | None:
    if not APPROVAL_ID_RE.match(ident):
        return None
    return read_json(approval_path(config, ident))


def expire_pending(config: RelayConfig, ident: str, *, note: str = "timeout") -> dict:
    """Transition a pending approval record to timeout atomically."""
    path = approval_path(config, ident)

    def _apply(current: dict) -> dict | None:
        status = str(current.get("status") or "")
        if status in (_APPROVED, _DENIED):
            return None
        if not current:
            current = {"id": ident, "chat_id": None, "request": {}}
        current["status"] = _TIMEOUT
        current["note"] = note
        current["decided_at"] = time.time()
        return current

    try:
        return _mutate(config, ident, _apply) or {}
    except TimeoutError:
        return read_json(path) or {}


def wait_for_decision(
    config: RelayConfig,
    ident: str,
    timeout: float,
    *,
    poll_interval: float = 0.2,
    out_opts: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Block until approved/denied, or mark timeout. Returns `(status, note)`."""
    deadline = time.monotonic() + max(0.0, timeout)
    interval = poll_interval if poll_interval and poll_interval > 0 else 0.05
    path = approval_path(config, ident)
    while True:
        data = read_json(path) or {}
        status = str(data.get("status") or "")
        if status in (_APPROVED, _DENIED):
            if status == _APPROVED:
                _extract_opts(data, out_opts)
            return status, str(data.get("note") or status)
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)

    updated = expire_pending(config, ident)
    status = str(updated.get("status") or _TIMEOUT)
    if status in (_APPROVED, _DENIED):
        if status == _APPROVED:
            _extract_opts(updated, out_opts)
        return status, str(updated.get("note") or status)
    return _TIMEOUT, "timeout"


def resolve_approver_chat(
    raw_user: str,
    *,
    approver: str = "",
    allowed: tuple[int, ...] = (),
) -> tuple[int | None, str]:
    """Pick the Telegram chat that should receive an approval prompt.

    A numeric `--user` wins. Otherwise use `AGENTS_RELAY_APPROVER`, then the
    sole allowlisted chat. Anything else is unavailable (caller exits 2).
    """
    text = (raw_user or "").strip()
    if text:
        try:
            return int(text), ""
        except ValueError:
            pass
    configured = (approver or "").strip()
    if configured:
        try:
            return int(configured), ""
        except ValueError:
            return None, "AGENTS_RELAY_APPROVER is not a numeric chat id"
    if len(allowed) == 1:
        return int(allowed[0]), ""
    if text:
        return None, (
            f"user {text!r} is not a numeric chat id; "
            "set AGENTS_RELAY_APPROVER or allow exactly one chat in TELEGRAM_ALLOWED_CHAT_IDS"
        )
    return None, (
        "no numeric approver; set AGENTS_RELAY_APPROVER "
        "or allow exactly one chat in TELEGRAM_ALLOWED_CHAT_IDS"
    )


def status_exit_code(status: str) -> int:
    if status == _APPROVED:
        return 0
    if status == _DENIED:
        return 1
    return 2


def run_local_approve(
    config: RelayConfig,
    request: dict,
    timeout: float,
    *,
    poll_interval: float = 0.2,
    out_opts: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """Write the pending file and block. No Telegram. Exit 0, 1, or 2."""
    ident = str(request.get("id") or "").strip()
    if not APPROVAL_ID_RE.match(ident):
        return 2, "invalid approval id"
    existing = read_approval(config, ident)
    if existing and existing.get("status") in (_APPROVED, _DENIED):
        # Replay guard: a decided id only resolves a request that
        # matches what was decided — a mismatched/empty stored hash
        # fails closed, never grants (agents-relay#44).
        ex_hash = str(existing.get("request_hash") or "")
        if not ex_hash:
            ex_req = existing.get("request")
            if isinstance(ex_req, dict):
                ex_hash = approval_request_hash(ex_req)
        if not ex_hash or not hmac.compare_digest(
                ex_hash, approval_request_hash(request)):
            return 2, "approval request changed"
        if existing.get("status") == _APPROVED:
            _extract_opts(existing, out_opts)
            return 0, str(existing.get("note") or "approved")
        return 1, str(existing.get("note") or "denied")
    create_pending(config, 0, request)
    status, note = wait_for_decision(
        config,
        ident,
        float(timeout),
        poll_interval=poll_interval,
        out_opts=out_opts,
    )
    return status_exit_code(status), note or status


def decide_approval(
    config: RelayConfig,
    ident: str,
    *,
    allow: bool,
    remember: bool = False,
    allow_pattern: str = "",
) -> tuple[int, str]:
    """Write one decision. 0 when the file was pending, 2 when it was not."""
    ident = (ident or "").strip()
    if not APPROVAL_ID_RE.match(ident):
        return 2, "invalid approval id"
    status = _APPROVED if allow else _DENIED
    updated = record_decision(
        config,
        ident,
        status=status,
        note=status,
        decided_by=None,
        expected_chat_id=None,
        remember=bool(allow and remember),
        allow_pattern=allow_pattern if allow else "",
    )
    if updated is None:
        return 2, "not pending"
    return 0, status


def _http_json(
    url: str,
    *,
    token: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=max(1.0, float(timeout))) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw) if raw.strip() else {}
    if not isinstance(parsed, dict):
        raise ValueError("invalid relay approval response")
    return parsed


def run_http_approve(
    relay_url: str,
    approval_token: str,
    request: dict,
    timeout: float,
    *,
    user: str = "",
    workspace: str = "",
    poll_slice: float = 25.0,
    out_opts: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """Submit an approval via relay HTTP using Bearer AGENTS_APPROVAL_TOKEN and wait."""
    ident = str(request.get("id") or "").strip()
    if not APPROVAL_ID_RE.match(ident):
        return 2, "invalid approval id"
    base = (relay_url or "").strip().rstrip("/")
    token = (approval_token or "").strip()
    if not base or not token:
        return 2, "relay approval HTTP env not configured"
    payload = dict(request)
    if user and not payload.get("user"):
        payload["user"] = user
    if workspace and not payload.get("workspace"):
        payload["workspace"] = workspace
    try:
        created = _http_json(
            f"{base}/v1/approvals/request",
            token=token,
            method="POST",
            body=payload,
            timeout=15.0,
        )
    except urllib.error.HTTPError as exc:
        return 2, f"approval request rejected ({exc.code})"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return 2, f"approval relay unreachable: {exc}"

    status = str(created.get("status") or "")
    if status in (_APPROVED, _DENIED):
        if status == _APPROVED:
            _extract_opts(created, out_opts)
        return status_exit_code(status), str(created.get("note") or status)

    slice_sec = max(0.05, float(poll_slice or 25.0))
    deadline = time.monotonic() + max(0.0, float(timeout))
    wait_url_base = f"{base}/v1/approvals/{urllib.parse.quote(ident, safe='')}/wait"
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        chunk = min(slice_sec, remaining)
        finalize = "1" if remaining <= slice_sec else "0"
        qs = urllib.parse.urlencode({"timeout": f"{chunk:.3f}", "finalize": finalize})
        try:
            res = _http_json(
                f"{wait_url_base}?{qs}",
                token=token,
                method="GET",
                timeout=max(5.0, chunk + 10.0),
            )
        except urllib.error.HTTPError as exc:
            return 2, f"approval wait rejected ({exc.code})"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return 2, f"approval relay unreachable: {exc}"
        status = str(res.get("status") or "")
        note = str(res.get("note") or status)
        if status in (_APPROVED, _DENIED, _TIMEOUT):
            if status == _APPROVED:
                _extract_opts(res, out_opts)
            return status_exit_code(status), note or status
        if time.monotonic() >= deadline:
            return 2, "timeout"
