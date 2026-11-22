"""VAPID (Web Push, RFC 8292) key generation, subscription storage, send.

P-256 key generation is done in pure stdlib Python: a scalar multiply on
secp256r1 runs once per host, so the ~60-line EC implementation beats pulling
in a crypto dependency the relay does not otherwise need. The private key is
stored hex-encoded under ``<host>/keys/vapid.json`` (mode 0600); the public
key served to browsers is the base64url uncompressed point (65 bytes).

Subscriptions (PushSubscription JSON from the web app) live in
``<host>/secrets/push.json`` (mode 0600) — a JSON array deduped by
``endpoint`` so a re-subscribe replaces, never appends. A 1-release fallback
reads legacy ``<ws>/relay/{vapid.json,push.json}`` when host files are absent.

Sending needs ``pywebpush`` (lazy import): it signs the VAPID JWT and does
the aes128gcm payload encryption. Absent → :class:`PushUnavailable`; the
key/subscription endpoints keep working regardless.
"""

from __future__ import annotations

import json
import secrets
import time
import warnings
from pathlib import Path
from typing import Any

from agents_workspace import Host

from .state import legacy_relay_dirs, read_json, read_json_any, write_json_atomic

# --- secp256r1 / P-256 (NIST) -------------------------------------------------

_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (_GX, _GY)


def _point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _P == 0:
            return None
        m = (3 * x1 * x1 + _A) * pow(2 * y1, -1, _P) % _P
    else:
        m = (y2 - y1) * pow((x2 - x1) % _P, -1, _P) % _P
    x3 = (m * m - x1 - x2) % _P
    return x3, (m * (x1 - x3) - y1) % _P


def _scalar_mul(k: int, point=_G):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _public_key_b64(priv: int) -> str:
    x, y = _scalar_mul(priv)
    return _b64url(b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big"))


# --- host key file ------------------------------------------------------------

def _vapid_path() -> Path:
    return Host.from_env().keys / "vapid.json"


def _push_path() -> Path:
    return Host.from_env().secrets_dir / "push.json"


def _valid_vapid(data: Any) -> dict[str, str] | None:
    if isinstance(data, dict) and data.get("public") and data.get("private"):
        return {"public": str(data["public"]), "private": str(data["private"])}
    return None


def vapid_keypair(config=None) -> dict[str, str]:
    """Load or mint the host VAPID keypair at ``<host>/keys/vapid.json``.

    Public key is base64url, 65 bytes. Falls back once to legacy
    ``<ws>/relay/vapid.json`` when the host file does not exist yet.
    """
    path = _vapid_path()
    pair = _valid_vapid(read_json(path))
    if pair is not None:
        return pair

    for legacy_dir in legacy_relay_dirs(config):
        legacy_path = legacy_dir / "vapid.json"
        pair = _valid_vapid(read_json(legacy_path))
        if pair is not None:
            warnings.warn(
                f"legacy VAPID keypair at {legacy_path} is deprecated; relocate to {path}",
                DeprecationWarning,
                stacklevel=2,
            )
            return pair

    priv = secrets.randbelow(_N - 1) + 1
    public = _public_key_b64(priv)
    payload = {"private": format(priv, "064x"), "public": public}
    write_json_atomic(path, payload, mode=0o600)
    return {"public": public, "private": format(priv, "064x")}


def handle_vapid_key(handler, config) -> None:
    """``GET /v1/push/vapid-key`` → the relay's VAPID public key."""
    try:
        pair = vapid_keypair(config)
    except Exception as exc:
        handler._write_json({"ok": False, "error": f"vapid unavailable: {exc}"}, 503)  # type: ignore[attr-defined]
        return
    handler._write_json({"ok": True, "key": pair["public"]}, 200)  # type: ignore[attr-defined]


# --- subscription store --------------------------------------------------------

def _store_file(root: Path) -> Path:
    return root if root.name == "push.json" else root / "push.json"


def _filter_subscriptions(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("endpoint")]


def load_subscriptions(
    root: Path | None = None,
    *,
    config=None,
    ws_ref: str = "",
) -> list[dict[str, Any]]:
    """Load push subscriptions from ``<host>/secrets/push.json`` (or explicit root).

    Falls back once to legacy ``<ws>/relay/push.json`` when the host file is absent.
    """
    if root is not None:
        return _filter_subscriptions(read_json_any(_store_file(root)))

    path = _push_path()
    if path.is_file():
        return _filter_subscriptions(read_json_any(path))

    seen: dict[str, dict[str, Any]] = {}
    legacy_sources: list[Path] = []
    for legacy_dir in legacy_relay_dirs(config, ws_ref=ws_ref):
        legacy_path = legacy_dir / "push.json"
        if not legacy_path.is_file():
            continue
        rows = _filter_subscriptions(read_json_any(legacy_path))
        if rows:
            legacy_sources.append(legacy_path)
            for row in rows:
                seen[str(row["endpoint"])] = row
    if legacy_sources:
        warnings.warn(
            f"legacy push subscriptions at {legacy_sources[0]} are deprecated; relocate to {path}",
            DeprecationWarning,
            stacklevel=2,
        )
    return list(seen.values())


def _write_subscriptions(rows: list[dict[str, Any]], root: Path | None = None) -> None:
    target = _store_file(root) if root is not None else _push_path()
    write_json_atomic(target, rows, mode=0o600)


def handle_push_subscribe(handler, config, body: dict, query: dict[str, Any]) -> None:
    """``POST /v1/push/subscribe`` — upsert a browser push subscription."""
    sub = body.get("subscription")
    if not isinstance(sub, dict):
        sub = body if body.get("endpoint") else None
    endpoint = str((sub or {}).get("endpoint") or "").strip()
    if not endpoint.startswith("https://"):
        handler._write_json({"ok": False, "error": "subscription.endpoint required"}, 400)  # type: ignore[attr-defined]
        return
    ws_ref = str(query.get("workspace") or body.get("workspace") or "").strip()
    if ws_ref:
        from .turn import resolve_workspace_id

        try:
            resolve_workspace_id(ws_ref)
        except ValueError as exc:
            handler._write_json({"ok": False, "error": str(exc)}, 400)  # type: ignore[attr-defined]
            return
    row = {
        "endpoint": endpoint,
        "keys": {k: str(v) for k, v in (sub.get("keys") or {}).items() if isinstance(v, str)},
        "workspace": ws_ref,
        "persona": str(body.get("persona") or "").strip(),
        "ua": str(handler.headers.get("User-Agent") or "")[:200],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    rows = [r for r in load_subscriptions(config=config) if r.get("endpoint") != endpoint]
    rows.append(row)
    _write_subscriptions(rows)
    handler._write_json({"ok": True, "subscriptions": len(rows)}, 200)  # type: ignore[attr-defined]


# --- send (pywebpush, lazy) ----------------------------------------------------

class PushUnavailable(RuntimeError):
    """``pywebpush`` is not installed — storage endpoints stay live anyway."""


def send_push_all(config, payload: dict[str, Any], ws_ref: str = "") -> int:
    """Push ``payload`` to every stored subscription. Returns delivered count."""
    try:
        from pywebpush import webpush  # type: ignore
    except ImportError:
        try:
            from webpush import webpush  # type: ignore
        except ImportError:
            raise PushUnavailable("pywebpush not installed") from None
    ws_ref = (ws_ref or "").strip()
    if ws_ref:
        from .turn import resolve_workspace_id

        resolve_workspace_id(ws_ref)
    pair = vapid_keypair(config)
    all_rows = load_subscriptions(config=config, ws_ref=ws_ref)
    targets = [
        sub
        for sub in all_rows
        if not ws_ref or str(sub.get("workspace") or "") in ("", ws_ref)
    ]
    sent = 0
    dead: list[str] = []
    for sub in targets:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": sub.get("keys") or {},
                },
                data=json.dumps(payload),
                vapid_private_key=pair["private"],
                vapid_claims={"sub": "mailto:relay@localhost"},
            )
            sent += 1
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            if status in (404, 410):
                dead.append(str(sub.get("endpoint")))
    if dead:
        rows = [r for r in load_subscriptions(config=config) if r.get("endpoint") not in dead]
        _write_subscriptions(rows)
    return sent


def notify_approval_pending(config, request: dict[str, Any], ws_ref: str = "") -> None:
    """Best-effort push when an approval goes pending. Never raises."""
    try:
        eff_ws = str(ws_ref or request.get("workspace") or "").strip()
        summary = str(request.get("summary") or request.get("command") or "")[:160]
        send_push_all(
            config,
            {
                "type": "approval",
                "id": str(request.get("id") or request.get("ident") or ""),
                "summary": summary,
                # sw.js drops pushes without title/body — always include.
                "title": "Klanker needs approval",
                "body": summary,
            },
            ws_ref=eff_ws,
        )
    except Exception:
        pass


def handle_push_send(handler, config, body: dict, query: dict[str, Any]) -> None:
    """``POST /v1/push/send`` — test/admin send to stored subscriptions."""
    try:
        sent = send_push_all(
            config,
            body.get("payload") if isinstance(body.get("payload"), dict) else body,
            ws_ref=str(query.get("workspace") or body.get("workspace") or "").strip(),
        )
    except PushUnavailable as exc:
        handler._write_json({"ok": False, "error": str(exc)}, 501)  # type: ignore[attr-defined]
        return
    handler._write_json({"ok": True, "sent": sent}, 200)  # type: ignore[attr-defined]
