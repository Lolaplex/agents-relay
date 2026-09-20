"""A2A routes: proxy lolaplex-board mailbox and peers."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import GatewayConfig


def _board_request(
    cfg: GatewayConfig,
    *,
    verb: str,
    project: str,
    payload: dict[str, Any],
    method: str = "POST",
) -> dict[str, Any]:
    if not cfg.board_url:
        return {"ok": False, "error": "BOARD_URL not configured"}
    project_slug = project or cfg.board_project
    if not project_slug:
        return {"ok": False, "error": "project required"}
    body = dict(payload)
    body["verb"] = verb
    body["project"] = project_slug
    data = json.dumps(body).encode("utf-8")
    url = f"{cfg.board_url.rstrip('/')}/connect"
    headers = {"Content-Type": "application/json"}
    if cfg.board_key_slug:
        try:
            nonce = subprocess.check_output(
                ["python", "-m", "agents_keys", "sign", cfg.board_key_slug, "board"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            headers["X-Agent-Proof"] = nonce
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")[:400]
        return {"ok": False, "error": f"board HTTP {exc.code}: {err}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc.reason)}


def mail_put(
    *,
    to: str,
    body: dict[str, Any],
    project: str = "",
    config: GatewayConfig | None = None,
) -> dict[str, Any]:
    cfg = config or GatewayConfig.from_env()
    return _board_request(
        cfg,
        verb="mail.put",
        project=project,
        payload={"to": to, "body": body},
    )


def mail_get(
    *,
    project: str = "",
    config: GatewayConfig | None = None,
) -> dict[str, Any]:
    cfg = config or GatewayConfig.from_env()
    return _board_request(cfg, verb="mail.get", project=project, payload={})


def list_peers(
    *,
    project: str = "",
    config: GatewayConfig | None = None,
) -> dict[str, Any]:
    cfg = config or GatewayConfig.from_env()
    return _board_request(cfg, verb="peers", project=project, payload={})


def handle_a2a_mail(body: dict[str, Any], config: GatewayConfig | None = None) -> dict[str, Any]:
    verb = str(body.get("verb") or "mail.put")
    project = str(body.get("project") or "")
    if verb == "mail.get":
        return mail_get(project=project, config=config)
    if verb == "peers":
        return list_peers(project=project, config=config)
    to = str(body.get("to") or "")
    payload = body.get("body") if isinstance(body.get("body"), dict) else body
    if not to and verb == "mail.put":
        return {"ok": False, "error": "to required for mail.put"}
    return mail_put(to=to, body=payload if isinstance(payload, dict) else {"text": str(payload)}, project=project, config=config)
