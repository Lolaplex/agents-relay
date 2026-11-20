"""VAPID (Web Push, RFC 8292) key generation and subscription storage.

P-256 key generation is done in pure stdlib Python: a scalar multiply on
secp256r1 runs once per host, so the ~60-line EC implementation beats pulling
in a crypto dependency the relay does not otherwise need. The private key is
stored hex-encoded under ``<host>/keys/vapid.json`` (mode 0600); the public
key served to browsers is the base64url uncompressed point (65 bytes).

Subscriptions (PushSubscription JSON from the web app) live in
``<ws>/relay/push/subscriptions.jsonl`` — one JSON object per line, deduped
by ``endpoint`` so a re-subscribe replaces, never appends. Without a
``?workspace=`` the serve state dir is used instead.
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

def _host_keys_dir() -> Path:
    from agents_workspace import Host

    return Host.from_env().keys


def vapid_keypair() -> dict[str, str]:
    """Load or mint the host VAPID keypair. Public key is base64url, 65 bytes."""
    path = _host_keys_dir() / "vapid.json"
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
        pair = vapid_keypair()
    except Exception as exc:
        handler._write_json({"ok": False, "error": f"vapid unavailable: {exc}"}, 503)  # type: ignore[attr-defined]
        return
    handler._write_json({"ok": True, "key": pair["public"]}, 200)  # type: ignore[attr-defined]


# --- subscription store --------------------------------------------------------

def _push_dir(config, ws_ref: str) -> Path:
    if ws_ref:
        from .turn import resolve_workspace_id

        return resolve_workspace_id(ws_ref).relay_state / "push"
    return Path(config.state_dir) / "push"


def _store_file(root: Path) -> Path:
    return root / "subscriptions.jsonl"


def load_subscriptions(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    path = _store_file(root)
    if not path.is_file():
        return rows
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("endpoint"):
                rows.append(row)
    except OSError:
        pass
    return rows


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
    root.mkdir(parents=True, exist_ok=True)
    tmp = _store_file(root).with_suffix(".tmp")
    tmp.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
    )
    try:
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    tmp.replace(_store_file(root))
    handler._write_json({"ok": True, "subscriptions": len(rows)}, 200)  # type: ignore[attr-defined]
