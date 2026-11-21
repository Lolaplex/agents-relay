"""Presence offers — RFC-0003 road declarations in a shared registry dir.

Each klanker publishes a signed ``road-declaration/1`` document announcing
where it is reachable. Signing and verification shell out to
``python -m agents_keys`` — key custody stays with agents-keys, this relay
never touches secret material. Consumers probe ``/health`` for liveness
separately: an offer is a claim, never truth. Forged or expired offers are
reported with their failure reason, never silently dropped.

Registry dir: ``AGENTS_PRESENCE_DIR`` or ``~/.agents/presence`` — a shared
(or synced) directory every host in the fleet can drop ``*.road.json`` in.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

DOC_TYPE = "road-declaration/1"
DID_PREFIX = "did:key:z"
# Offer lifetime. A publisher that wants persistence republishes (RFC-0003 §4).
OFFER_TTL_SECS = 30 * 24 * 60 * 60
_LABEL_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")


def registry_dir() -> Path:
    env = os.environ.get("AGENTS_PRESENCE_DIR", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".agents" / "presence"


def _canonical(doc: dict) -> bytes:
    """obol canonical: compact JSON, keys sorted, ``sig`` stripped."""
    stripped = {k: v for k, v in doc.items() if k != "sig"}
    return json.dumps(stripped, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _endpoint_of(kind: str, addr: str) -> str:
    if "://" in addr:
        return addr
    return f"http://{addr}" if kind == "http" else f"https://{addr}"


def _verify_doc(doc: dict) -> tuple[str, list, int, int]:
    """Returns (did, roads, iat, exp) or raises ValueError with the reason."""
    if doc.get("type") != DOC_TYPE:
        raise ValueError(f"type is not {DOC_TYPE}")
    did = str(doc.get("id") or "")
    if not did:
        raise ValueError("missing id")
    exp = int(doc.get("exp") or 0)
    if not exp:
        raise ValueError("missing exp")
    if exp <= int(time.time()):
        raise ValueError(f"expired (exp {exp})")
    sig = str(doc.get("sig") or "")
    if not sig:
        raise ValueError("missing sig")
    _agents_keys(["verify", did, _canonical(doc).decode("utf-8"), sig])

    roads = []
    for entry in doc.get("roads") or []:
        kind = str(entry.get("kind") or "")
        addr = str(entry.get("addr") or "")
        if not addr:
            continue
        priority = int(entry.get("priority") or 10)
        roads.append({"kind": kind, "addr": addr, "priority": priority,
                      "endpoint": _endpoint_of(kind, addr)})
    roads.sort(key=lambda r: r["priority"])
    return did, roads, int(doc.get("iat") or 0), exp


def _agents_keys(args: list[str]) -> str:
    """Shell out to `python -m agents_keys` — the suite's key custody CLI."""
    import sys
    try:
        out = subprocess.run(
            [sys.executable, "-m", "agents_keys", *args],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        raise ValueError("python -m agents_keys unavailable")
    except subprocess.TimeoutExpired:
        raise ValueError("agents_keys timed out")
    if out.returncode != 0:
        raise ValueError((out.stderr or out.stdout or "agents_keys failed").strip().splitlines()[0])
    return out.stdout.strip()


def _scan_file(path: Path) -> dict:
    file = path.name
    label = file[:-len(".road.json")] if file.endswith(".road.json") else path.stem
    row = {"file": file, "label": label, "did": "", "roads": [],
           "iat": 0, "exp": 0, "valid": False, "reason": ""}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        row["reason"] = f"unreadable: {e}"
        return row
    except json.JSONDecodeError as e:
        row["reason"] = f"not json: {e}"
        return row
    try:
        did, roads, iat, exp = _verify_doc(doc)
        row.update(did=did, roads=roads, iat=iat, exp=exp, valid=True)
    except (ValueError, TypeError) as e:
        row["reason"] = str(e)
        row["did"] = str(doc.get("id") or "")
        row["iat"] = int(doc.get("iat") or 0)
        row["exp"] = int(doc.get("exp") or 0)
    return row


def list_offers() -> list[dict]:
    """Every ``*.json`` in the registry becomes exactly one offer row —
    verification failures surface as invalid rows, never dropped entries.
    A missing directory is an empty registry, not an error."""
    d = registry_dir()
    if not d.is_dir():
        return []
    return [_scan_file(p) for p in sorted(d.glob("*.json"))]


def maybe_self_publish() -> None:
    """Announce this relay in the shared registry at boot. Opt-in via
    ``AGENTS_PRESENCE_LABEL`` + ``AGENTS_PRESENCE_KEY`` + ``AGENTS_PRESENCE_ADDR``
    (addr as ``host[:port]`` or a full ``scheme://…`` URL). One-sided: the
    host declares its roads; consumers verify and probe liveness."""
    label = os.environ.get("AGENTS_PRESENCE_LABEL", "").strip()
    key = os.environ.get("AGENTS_PRESENCE_KEY", "").strip()
    addr = os.environ.get("AGENTS_PRESENCE_ADDR", "").strip()
    if not (label and key and addr):
        return
    kind = addr.split("://")[0] if "://" in addr else "https"
    offer, err = publish_offer(key, label, [{"kind": kind, "addr": addr}])
    if err:
        log.warning("presence self-publish failed: %s", err)
    else:
        log.info("presence offer published: %s -> %s", label, addr)


def publish_offer(key_slug: str, label: str, roads: list[dict]) -> tuple[dict | None, str]:
    """Mint + sign + write a declaration. The finished document is
    self-verified before it touches the registry: an unverifiable offer
    must never be published."""
    if not _LABEL_OK.match(label or ""):
        return None, f"invalid label: {label!r}"
    clean_roads = []
    for r in roads or []:
        addr = str(r.get("addr") or "").strip()
        kind = str(r.get("kind") or "").strip()
        if not addr or not kind:
            return None, "every road needs kind + addr"
        clean_roads.append({"kind": kind, "addr": addr,
                            "priority": int(r.get("priority") or 10)})
    if not clean_roads:
        return None, "no roads given"
    try:
        did = _agents_keys(["did", key_slug])
    except ValueError as e:
        return None, str(e)
    now = int(time.time())
    doc = {"type": DOC_TYPE, "id": did, "roads": clean_roads,
           "iat": now, "exp": now + OFFER_TTL_SECS}
    try:
        sig_hex = _agents_keys(["sign", key_slug, _canonical(doc).decode("utf-8")])
        doc["sig"] = base64.urlsafe_b64encode(bytes.fromhex(sig_hex)).rstrip(b"=").decode()
        _verify_doc(doc)
    except (ValueError, TypeError) as e:
        return None, f"self-check failed: {e}"
    d = registry_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{label}.road.json"
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        return None, f"failed to write {d}: {e}"
    return _scan_file(path), ""
