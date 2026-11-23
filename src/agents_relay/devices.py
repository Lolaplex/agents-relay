"""Per-device scoped credentials for the hub channel (agents-relay#40).

Device meta lives in ``<host>/clients/devices.json`` as
``{id: {label, host_id, created_at, last_seen}}``. Bearer tokens live
exclusively in the Host Secret Vault (``DEVICE_<ID>_TOKEN``, scope="host")
and are never written into the meta file or emitted over the wire after
the one-time redeem response.
"""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path

from agents_workspace import Host, delete_secret, resolve_secret, write_secret

from .state import read_json, write_json_atomic
from .turn import valid_id


def _path() -> Path:
    return Host.from_env().clients / "devices.json"


def _secret_key(device_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", device_id.strip()).upper()
    return f"DEVICE_{clean}_TOKEN"


def _read_secret(device_id: str) -> str:
    try:
        return str(resolve_secret(_secret_key(device_id), scope="host") or "")
    except Exception:
        return ""


def _write_secret(device_id: str, value: str) -> None:
    write_secret(_secret_key(device_id), value, scope="host")


def load_devices() -> dict:
    data = read_json(_path()) or {}
    rows = data.get("devices")
    if not isinstance(rows, dict):
        return {}
    out: dict = {}
    for did, meta in rows.items():
        if not isinstance(meta, dict):
            continue
        out[did] = {
            "label": str(meta.get("label") or ""),
            "host_id": str(meta.get("host_id") or ""),
            "role": str(meta.get("role") or "host"),
            "created_at": meta.get("created_at") or 0,
            "last_seen": meta.get("last_seen") or 0,
            "allowlist": list(meta.get("allowlist") or []),
        }
    return out


def _store(rows: dict) -> None:
    write_json_atomic(_path(), {"devices": rows})


def mint_device(device_id: str = "", *, label: str = "", host_id: str = "",
                role: str = "host",
                allowlist: list[str] | None = None) -> dict:
    """Mint a device row + vault token. Re-minting the same id rotates the
    token (re-pair flow) and keeps label/role/allowlist unless overridden.

    role: 'host' = compute node (channel + ws ops); 'client' = operator UI
    (chat + own approvals); 'ws_member' = workspace guest minted by a
    mode=ws invite (klanker#145) — restricted to the workspaces in its
    allowlist and denied everywhere else (admin surfaces stay
    device-denied regardless — see _check_secret(allow_device=False)
    routes and the channel http-RPC denylist)."""
    did = (device_id or "").strip() or secrets.token_hex(6)
    if not valid_id(did):
        raise ValueError(f"invalid device id: {did!r}")
    if role not in ("host", "client", "ws_member"):
        raise ValueError(f"invalid device role: {role!r}")
    if role == "ws_member" and not (allowlist or []):
        raise ValueError("ws_member devices need a non-empty allowlist")
    rows = load_devices()
    prev = rows.get(did) or {}
    token = secrets.token_urlsafe(24)
    _write_secret(did, token)
    rows[did] = {
        "label": label or prev.get("label") or did,
        "host_id": host_id or prev.get("host_id") or "",
        "role": role or prev.get("role") or "host",
        "created_at": prev.get("created_at") or int(time.time()),
        "last_seen": 0,
        "allowlist": allowlist if allowlist is not None else prev.get("allowlist", []),
    }
    _store(rows)
    return {"device_id": did, "device_token": token,
            "label": rows[did]["label"], "role": rows[did]["role"],
            "created_at": rows[did]["created_at"]}


def verify_device(device_id: str, token: str) -> bool:
    """Constant-time check of a presented device token."""
    import hmac

    did = (device_id or "").strip()
    tok = (token or "").strip()
    if not did or not tok or did not in load_devices():
        return False
    expected = _read_secret(did)
    if not expected:
        return False
    return hmac.compare_digest(tok.encode("utf-8"), expected.encode("utf-8"))


def device_id_for_token(token: str) -> str:
    """Reverse lookup for Bearer-only auth: which device does this token open."""
    tok = (token or "").strip()
    if not tok:
        return ""
    for did in load_devices():
        if verify_device(did, tok):
            return did
    return ""


def touch_device(device_id: str) -> None:
    rows = load_devices()
    if device_id in rows:
        rows[device_id]["last_seen"] = int(time.time())
        _store(rows)


def revoke_device(device_id: str) -> bool:
    rows = load_devices()
    if device_id not in rows:
        return False
    del rows[device_id]
    _store(rows)
    try:
        delete_secret(_secret_key(device_id), scope="host")
    except Exception:
        pass
    return True


def list_public() -> list[dict]:
    return [
        {"id": did, "label": m["label"], "host_id": m["host_id"],
         "role": m["role"],
         "created_at": m["created_at"], "last_seen": m["last_seen"],
         "has_token": bool(_read_secret(did))}
        for did, m in sorted(load_devices().items())
    ]
