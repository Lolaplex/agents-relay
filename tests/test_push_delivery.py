"""Native RFC 8291/8292 Web Push delivery tests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import sys
import threading
import urllib.error
from http.client import HTTPConnection

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agents_relay import push as push_mod
from agents_relay.config import RelayConfig
from agents_relay.http_adapter import serve_http
from agents_relay.workspaces import create_workspace


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _public_bytes(private_key) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def _subscription(endpoint: str, workspace: str = "") -> dict:
    private_key = ec.generate_private_key(ec.SECP256R1())
    return {
        "endpoint": endpoint,
        "keys": {
            "p256dh": _b64url(_public_bytes(private_key)),
            "auth": _b64url(b"0123456789abcdef"),
        },
        "workspace": workspace,
    }


def _content_keys(ua_private, ua_public: bytes, as_public: bytes, auth: bytes, salt: bytes) -> tuple[bytes, bytes]:
    as_public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    ecdh_secret = ua_private.exchange(ec.ECDH(), as_public_key)
    prk_key = hmac.new(auth, ecdh_secret, hashlib.sha256).digest()
    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = hmac.new(prk_key, key_info + b"\x01", hashlib.sha256).digest()
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    cek = hmac.new(prk, b"Content-Encoding: aes128gcm\x00\x01", hashlib.sha256).digest()[:16]
    nonce = hmac.new(prk, b"Content-Encoding: nonce\x00\x01", hashlib.sha256).digest()[:12]
    return cek, nonce


def _decrypt_push_body(body: bytes, ua_private, auth: bytes) -> bytes:
    salt = body[:16]
    record_size = struct.unpack("!I", body[16:20])[0]
    idlen = body[20]
    as_public = body[21:21 + idlen]
    ciphertext = body[21 + idlen:]
    assert record_size == 4096
    assert len(salt) == 16
    assert len(as_public) == 65
    ua_public = _public_bytes(ua_private)
    cek, nonce = _content_keys(ua_private, ua_public, as_public, auth, salt)
    padded = AESGCM(cek).decrypt(nonce, ciphertext, b"")
    stripped = padded.rstrip(b"\x00")
    assert stripped[-1] == 2
    return stripped[:-1]


def test_aes128gcm_matches_rfc8291_appendix_a_vector():
    as_private_scalar = int.from_bytes(
        _b64url_decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"),
        "big",
    )
    as_private = ec.derive_private_key(as_private_scalar, ec.SECP256R1())
    body = push_mod._encrypt_aes128gcm(
        _b64url_decode("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        server_private_key=as_private,
        salt=_b64url_decode("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    expected = (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
        "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
        "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )
    assert body == _b64url_decode(expected)


def test_aes128gcm_roundtrip_decrypts_original_json_bytes():
    ua_private = ec.generate_private_key(ec.SECP256R1())
    auth = secrets.token_bytes(16)
    payload = {"type": "approval", "title": "Klanker needs approval", "body": "run pytest"}
    plaintext = json.dumps(payload).encode("utf-8")
    body = push_mod._encrypt_aes128gcm(
        plaintext,
        _b64url(_public_bytes(ua_private)),
        _b64url(auth),
    )
    assert _decrypt_push_body(body, ua_private, auth) == plaintext


def test_vapid_authorization_claims_and_es256_signature():
    pair = push_mod.vapid_keypair(None)
    now = 1_700_000_000
    value = push_mod._vapid_authorization("https://PUSH.example.net:443/p/res?x=1", pair, now=now)
    scheme, parameters = value.split(" ", 1)
    assert scheme == "vapid"
    parts = dict(item.split("=", 1) for item in parameters.split(", "))
    assert parts["k"] == pair["public"]
    header_b64, claims_b64, signature_b64 = parts["t"].split(".")
    header = json.loads(_b64url_decode(header_b64))
    claims = json.loads(_b64url_decode(claims_b64))
    assert header == {"typ": "JWT", "alg": "ES256"}
    assert claims["aud"] == "https://push.example.net"
    assert 0 < claims["exp"] - now <= 12 * 60 * 60
    assert claims["sub"] == "mailto:relay@localhost"

    signature = _b64url_decode(signature_b64)
    assert len(signature) == 64
    der_signature = encode_dss_signature(
        int.from_bytes(signature[:32], "big"),
        int.from_bytes(signature[32:], "big"),
    )
    public_key = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(),
        _b64url_decode(pair["public"]),
    )
    public_key.verify(
        der_signature,
        f"{header_b64}.{claims_b64}".encode("ascii"),
        ec.ECDSA(hashes.SHA256()),
    )


def test_send_push_all_native_delivered_prunes_dead_and_scopes_workspace(monkeypatch):
    create_workspace("demo")
    monkeypatch.setattr(push_mod, "_import_pywebpush", lambda: None)
    rows = [
        _subscription("https://push.example/live", "demo"),
        _subscription("https://push.example/unscoped", ""),
        _subscription("https://push.example/gone", "demo"),
        _subscription("https://push.example/missing", "demo"),
        _subscription("https://push.example/other", "other"),
    ]
    push_mod._write_subscriptions(rows)
    statuses = {
        "https://push.example/live": 201,
        "https://push.example/unscoped": 201,
        "https://push.example/gone": 410,
        "https://push.example/missing": 404,
        "https://push.example/other": 201,
    }
    calls = []

    class Response:
        def __init__(self, status: int) -> None:
            self.status = status

        def close(self) -> None:
            pass

    def fake_urlopen(request, timeout=0):
        del timeout
        calls.append(request)
        status = statuses[request.full_url]
        if status == 410:
            raise urllib.error.HTTPError(request.full_url, status, "gone", None, None)
        return Response(status)

    monkeypatch.setattr(push_mod.urllib.request, "urlopen", fake_urlopen)
    sent = push_mod.send_push_all(None, {"type": "test"}, ws_ref="demo")

    assert sent == 2
    assert {request.full_url for request in calls} == {
        "https://push.example/live",
        "https://push.example/unscoped",
        "https://push.example/gone",
        "https://push.example/missing",
    }
    assert all((request.get_header("Authorization") or "").startswith("vapid t=") for request in calls)
    assert all(request.get_header("Content-encoding") == "aes128gcm" for request in calls)
    remaining = {row["endpoint"] for row in push_mod.load_subscriptions()}
    assert remaining == {
        "https://push.example/live",
        "https://push.example/unscoped",
        "https://push.example/other",
    }


def test_send_push_all_pywebpush_stays_preferred_and_prunes(monkeypatch):
    rows = [
        _subscription("https://push.example/py-live"),
        _subscription("https://push.example/py-gone"),
    ]
    push_mod._write_subscriptions(rows)
    calls = []

    class PushError(Exception):
        status_code = 410

    def fake_webpush(**kwargs):
        calls.append(kwargs)
        if kwargs["subscription_info"]["endpoint"].endswith("py-gone"):
            raise PushError("gone")

    monkeypatch.setattr(push_mod, "_import_pywebpush", lambda: fake_webpush)
    monkeypatch.setattr(push_mod, "_cryptography_available", lambda: True)
    sent = push_mod.send_push_all(None, {"type": "test"})

    assert sent == 1
    assert len(calls) == 2
    assert len(_b64url_decode(calls[0]["vapid_private_key"])) == 32
    assert [row["endpoint"] for row in push_mod.load_subscriptions()] == [
        "https://push.example/py-live"
    ]


def test_push_backend_preference_and_unavailable(monkeypatch):
    monkeypatch.setattr(push_mod, "_import_pywebpush", lambda: object())
    assert push_mod.push_send_backend() == "pywebpush"
    monkeypatch.setattr(push_mod, "_import_pywebpush", lambda: None)
    monkeypatch.setattr(push_mod, "_cryptography_available", lambda: True)
    assert push_mod.push_send_backend() == "cryptography"
    monkeypatch.setattr(push_mod, "_cryptography_available", lambda: False)
    assert push_mod.push_send_backend() is None
    with pytest.raises(push_mod.PushUnavailable):
        push_mod.send_push_all(None, {"type": "test"})


def _relay_config() -> RelayConfig:
    return RelayConfig(
        loop_cmd=(sys.executable, "-c", "print('skip')"),
        loop_provider="echo",
        relay_secret="expected",
        telegram_bot_token="",
        telegram_allowed_chat_ids=(),
        relay_host="127.0.0.1",
        relay_port=0,
        telegram_poll_timeout=1,
        web_token="tok",
        web_user="web",
    )


def _request(server, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    host, port = server.server_address  # type: ignore[misc]
    conn = HTTPConnection(host, port, timeout=10)
    payload = json.dumps(body) if body is not None else None
    headers = {"X-Relay-Secret": "expected"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    data = json.loads(response.read().decode("utf-8"))
    conn.close()
    return response.status, data


def test_push_key_capability_and_send_route_with_cryptography(monkeypatch):
    monkeypatch.setattr(push_mod, "_import_pywebpush", lambda: None)
    server = serve_http(_relay_config())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Response:
        status = 201

        def close(self) -> None:
            pass

    monkeypatch.setattr(push_mod.urllib.request, "urlopen", lambda request, timeout=0: Response())
    try:
        status, data = _request(server, "GET", "/v1/push/key")
        assert status == 200
        assert data["push_capable"] is True
        assert data["backend"] == "cryptography"
        assert data["key"] == data["public_key"] == data["publicKey"]

        push_mod._write_subscriptions([_subscription("https://push.example/route")])
        status, data = _request(server, "POST", "/v1/push/send", {"payload": {"type": "test"}})
        assert status == 200
        assert data == {"ok": True, "sent": 1}
    finally:
        server.shutdown()
        thread.join(timeout=2)
