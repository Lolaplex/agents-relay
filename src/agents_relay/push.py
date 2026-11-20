"""VAPID (Web Push, RFC 8292) key generation, subscription storage, send.

P-256 key generation is done in pure stdlib Python: a scalar multiply on
secp256r1 runs once per host, so the ~60-line EC implementation beats pulling
in a crypto dependency the relay does not otherwise need. The private key is
stored hex-encoded under ``<ws>/relay/vapid.json`` (mode 0600) — i.e. the
configured relay state dir; the public key served to browsers is the
base64url uncompressed point (65 bytes).

Subscriptions (PushSubscription JSON from the web app) live in
``<ws>/relay/push.json`` — a JSON array deduped by ``endpoint`` so a
re-subscribe replaces, never appends. Without a ``?workspace=`` the serve
state dir is used instead.

Sending needs ``pywebpush`` (lazy import): it signs the VAPID JWT and does
the aes128gcm payload encryption. Absent → :class:`PushUnavailable`; the
key/subscription endpoints keep working regardless.
"""

from __future__ import annotations

import json
import secrets
import stat
import time
from pathlib import Path
from typing import Any

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

def _state_dir(config) -> Path:
    return Path(config.state_dir)


def vapid_keypair(config) -> dict[str, str]:
    """Load or mint the relay VAPID keypair. Public key is base64url, 65 bytes."""
    path = _state_dir(config) / "vapid.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("public") and data.get("private"):
            return {"public": data["public"], "private": data["private"]}
    except (OSError, json.JSONDecodeError):
        pass
    priv = secrets.randbelow(_N - 1) + 1
    public = _public_key_b64(priv)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"private": format(priv, "064x"), "public": public}, indent=1),
        encoding="utf-8",
    )
    try:
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    tmp.replace(path)
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

def _push_dir(config, ws_ref: str) -> Path:
    if ws_ref:
        from .turn import resolve_workspace_id

        return resolve_workspace_id(ws_ref).relay_state
    return _state_dir(config)


def _store_file(root: Path) -> Path:
    return root / "push.json"


def load_subscriptions(root: Path) -> list[dict[str, Any]]:
    path = _store_file(root)
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("endpoint")]


def _write_subscriptions(root: Path, rows: list[dict[str, Any]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = _store_file(root).with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    try:
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    tmp.replace(_store_file(root))


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
    try:
        root = _push_dir(config, ws_ref)
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
    rows = [r for r in load_subscriptions(root) if r.get("endpoint") != endpoint]
    rows.append(row)
    _write_subscriptions(root, rows)
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
    pair = vapid_keypair(config)
    root = _push_dir(config, ws_ref)
    sent = 0
    dead: list[str] = []
    for sub in load_subscriptions(root):
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
        rows = [r for r in load_subscriptions(root) if r.get("endpoint") not in dead]
        _write_subscriptions(root, rows)
    return sent


def notify_approval_pending(config, request: dict[str, Any], ws_ref: str = "") -> None:
    """Best-effort push when an approval goes pending. Never raises."""
    try:
        send_push_all(
            config,
            {
                "type": "approval",
                "id": str(request.get("id") or request.get("ident") or ""),
                "summary": str(request.get("summary") or request.get("command") or "")[:160],
            },
            ws_ref=ws_ref,
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
