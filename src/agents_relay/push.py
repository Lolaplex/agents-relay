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

Sending prefers ``pywebpush`` when installed (lazy import). Otherwise a
native sender uses lazy-imported ``cryptography`` for the RFC 8291 aes128gcm
payload encryption and the RFC 8292 VAPID ES256 JWT over stdlib urllib. When
neither is importable, sending raises :class:`PushUnavailable`; the
key/subscription endpoints keep working regardless.
"""

from __future__ import annotations

import base64
import json
import secrets
import struct
import time
import urllib.parse
import urllib.request
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
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


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
    """``GET /v1/push/vapid-key``/``/v1/push/key`` → the VAPID public key."""
    try:
        pair = vapid_keypair(config)
    except Exception as exc:
        handler._write_json({"ok": False, "error": f"vapid unavailable: {exc}"}, 503)  # type: ignore[attr-defined]
        return
    backend = push_send_backend()
    handler._write_json(  # type: ignore[attr-defined]
        {
            "ok": True,
            "key": pair["public"],
            "public_key": pair["public"],
            "publicKey": pair["public"],
            "push_capable": bool(backend),
            "backend": backend,
        },
        200,
    )


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


# --- send (pywebpush first, native cryptography fallback) ---------------------

class PushUnavailable(RuntimeError):
    """Neither ``pywebpush`` nor ``cryptography`` can send web pushes."""


def _import_pywebpush():
    try:
        from pywebpush import webpush  # type: ignore

        return webpush
    except ImportError:
        try:
            from webpush import webpush  # type: ignore

            return webpush
        except ImportError:
            return None


def _crypto_primitives():
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:
        raise PushUnavailable("cryptography not installed") from exc
    return hashes, serialization, ec, decode_dss_signature, AESGCM, HKDF


def _cryptography_available() -> bool:
    try:
        _crypto_primitives()
    except PushUnavailable:
        return False
    return True


def push_send_backend() -> str | None:
    """Return the available push delivery backend, in preference order."""
    if _import_pywebpush() is not None:
        return "pywebpush"
    if _cryptography_available():
        return "cryptography"
    return None


def _encrypt_aes128gcm(
    plaintext: bytes,
    ua_public_b64: str,
    auth_b64: str,
    *,
    server_private_key: Any = None,
    salt: bytes | None = None,
    record_size: int = 4096,
) -> bytes:
    """Encrypt one single-record RFC 8291 ``aes128gcm`` push payload."""
    hashes, serialization, ec, _decode_dss_signature, AESGCM, HKDF = _crypto_primitives()
    ua_public_raw = _b64url_decode(ua_public_b64)
    auth_secret = _b64url_decode(auth_b64)
    if len(ua_public_raw) != 65 or ua_public_raw[0] != 4:
        raise ValueError("subscription keys.p256dh must be an uncompressed P-256 point")
    if len(auth_secret) != 16:
        raise ValueError("subscription keys.auth must decode to 16 bytes")
    if record_size < 18:
        raise ValueError("aes128gcm record size must be at least 18 bytes")
    max_plaintext = min(record_size - 17, 3993)
    if len(plaintext) > max_plaintext:
        raise ValueError("push payload exceeds the 3993-byte aes128gcm limit")

    ua_public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public_raw)
    if server_private_key is None:
        server_private_key = ec.generate_private_key(ec.SECP256R1())
    server_public_raw = server_private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    salt = secrets.token_bytes(16) if salt is None else salt
    if len(salt) != 16:
        raise ValueError("aes128gcm salt must be 16 bytes")

    # RFC 8291 §3.3/§3.4: combine ECDH and auth secret, then RFC 8188 CEK/nonce.
    ecdh_secret = server_private_key.exchange(ec.ECDH(), ua_public_key)
    key_info = b"WebPush: info\x00" + ua_public_raw + server_public_raw
    ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=auth_secret,
        info=key_info,
    ).derive(ecdh_secret)
    cek = HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=salt,
        info=b"Content-Encoding: aes128gcm\x00",
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(),
        length=12,
        salt=salt,
        info=b"Content-Encoding: nonce\x00",
    ).derive(ikm)

    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", b"")
    return (
        salt
        + struct.pack("!I", record_size)
        + bytes([len(server_public_raw)])
        + server_public_raw
        + ciphertext
    )


def _endpoint_origin(endpoint: str) -> str:
    parts = urllib.parse.urlsplit(endpoint)
    if not parts.scheme or not parts.hostname:
        raise ValueError("push endpoint must be an absolute URL")
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = {"http": 80, "https": 443}.get(scheme)
    port = parts.port
    if port is not None and port != default_port:
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def _vapid_authorization(endpoint: str, pair: dict[str, str], now: int | None = None) -> str:
    """Build the RFC 8292 ``Authorization: vapid t=<JWT>, k=<pub>`` value."""
    hashes, _serialization, ec, decode_dss_signature, _AESGCM, _HKDF = _crypto_primitives()
    now = int(time.time()) if now is None else int(now)
    header = {"typ": "JWT", "alg": "ES256"}
    claims = {
        "aud": _endpoint_origin(endpoint),
        "exp": now + 12 * 60 * 60,
        "sub": "mailto:relay@localhost",
    }
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    claims_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{claims_b64}"
    private_key = ec.derive_private_key(int(pair["private"], 16), ec.SECP256R1())
    der_sig = private_key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    token = f"{signing_input}.{_b64url(signature)}"
    return f"vapid t={token}, k={pair['public']}"


def _send_native(subscription: dict[str, Any], data: str, pair: dict[str, str]) -> int:
    keys = subscription.get("keys") or {}
    body = _encrypt_aes128gcm(
        data.encode("utf-8"),
        str(keys.get("p256dh") or ""),
        str(keys.get("auth") or ""),
    )
    endpoint = str(subscription["endpoint"])
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": _vapid_authorization(endpoint, pair),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": "0",
        },
        method="POST",
    )
    response = None
    try:
        response = urllib.request.urlopen(request, timeout=15)
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        return int(status or 0)
    finally:
        close = getattr(response, "close", None)
        if close is not None:
            close()


def _pywebpush_private_key(private: str) -> str:
    """pywebpush wants the raw scalar base64url-encoded; host storage is hex."""
    try:
        raw = bytes.fromhex(private)
    except ValueError:
        return private
    return _b64url(raw) if len(raw) == 32 else private


def _exception_status(exc: Exception) -> int:
    for attr in ("code", "status", "status_code"):
        try:
            status = int(getattr(exc, attr, 0) or 0)
        except Exception:
            status = 0
        if status:
            return status
    try:
        response = getattr(exc, "response", None)
    except Exception:
        response = None
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except Exception:
        return 0


def send_push_all(config, payload: dict[str, Any], ws_ref: str = "") -> int:
    """Push ``payload`` to every stored subscription. Returns delivered count."""
    webpush = _import_pywebpush()
    backend = "pywebpush" if webpush is not None else (
        "cryptography" if _cryptography_available() else ""
    )
    if not backend:
        raise PushUnavailable("pywebpush or cryptography not installed")
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
    data = json.dumps(payload)
    sent = 0
    dead: list[str] = []
    for sub in targets:
        try:
            if backend == "pywebpush":
                webpush(
                    subscription_info={
                        "endpoint": sub["endpoint"],
                        "keys": sub.get("keys") or {},
                    },
                    data=data,
                    vapid_private_key=_pywebpush_private_key(pair["private"]),
                    vapid_claims={"sub": "mailto:relay@localhost"},
                )
                sent += 1
                continue
            status = _send_native(sub, data, pair)
            if status in (404, 410):
                dead.append(str(sub.get("endpoint")))
            elif 200 <= status < 300:
                sent += 1
        except Exception as exc:
            if _exception_status(exc) in (404, 410):
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
