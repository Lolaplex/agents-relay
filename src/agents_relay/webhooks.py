"""Inbound alert webhooks (CI, Coolify, schedule failures).

Verification fails closed: an unset, empty, or invalid ``WEBHOOK_SECRET``
always rejects the request as unauthorized.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from .config import RelayConfig
from .turn import TurnParams, run_loop_turn


def _verify_secret(raw_body: bytes, header: str, expected: str) -> bool:
    """Verify webhook secret header or HMAC-SHA256 digest; fail closed when unset."""
    expected_clean = (expected or "").strip()
    if not expected_clean:
        return False
    got = (header or "").strip()
    if not got:
        return False
    if hmac.compare_digest(got.encode("utf-8"), expected_clean.encode("utf-8")):
        return True
    digest = hmac.new(expected_clean.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return (
        hmac.compare_digest(got.encode("utf-8"), digest.encode("utf-8"))
        or hmac.compare_digest(got.encode("utf-8"), f"sha256={digest}".encode("utf-8"))
    )


def _format_alert(source: str, body: dict[str, Any]) -> str:
    if source == "ci":
        repo = body.get("repo") or body.get("repository") or "repo"
        status = body.get("status") or body.get("conclusion") or "unknown"
        url = body.get("url") or body.get("html_url") or ""
        return f"CI alert: {repo} {status}" + (f" ({url})" if url else "")
    if source == "coolify":
        app = body.get("application") or body.get("name") or "app"
        status = body.get("status") or body.get("health") or "unhealthy"
        return f"Coolify alert: {app} {status}"
    if source == "schedule":
        name = body.get("name") or body.get("schedule") or "schedule"
        err = body.get("error") or body.get("message") or "failed"
        return f"Schedule alert: {name} — {err}"
    summary = body.get("message") or body.get("text") or json.dumps(body, ensure_ascii=False)[:500]
    return f"Webhook {source}: {summary}"


def handle_webhook(
    source: str,
    body: dict[str, Any],
    *,
    raw_body: bytes,
    secret_header: str,
    config: RelayConfig | None = None,
    notify_turn: bool = True,
) -> dict[str, Any]:
    cfg = config or RelayConfig.from_env()
    if not _verify_secret(raw_body, secret_header, cfg.webhook_secret):
        return {"ok": False, "error": "unauthorized"}
    text = _format_alert(source, body)
    out: dict[str, Any] = {"ok": True, "source": source, "alert": text}
    if notify_turn:
        result = run_loop_turn(
            channel=f"webhook:{source}",
            user="alerts",
            message=text,
            config=cfg,
            new_session=True,
            workspace=str(body.get("workspace") or ""),
        )
        out["turn"] = {
            "reply": result.reply,
            "session": result.session,
            "returncode": result.returncode,
        }
    return out
