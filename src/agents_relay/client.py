"""HTTP client for /v1/turn and /v1/stream."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass
class GatewayClient:
    base_url: str = "http://127.0.0.1:8787"
    secret: str = ""
    session: str = ""
    user_id: str = ""
    user: str = "local"
    channel: str = "http"
    provider: str = ""
    persona: str = ""
    project: str = ""
    prefs_path: Path | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["X-Gateway-Secret"] = self.secret
        return headers

    def _apply_meta(self, data: dict[str, Any]) -> None:
        """Update in-memory session/user from a turn response. Not persisted."""
        if data.get("session"):
            self.session = str(data["session"])
        if data.get("user_id"):
            self.user_id = str(data["user_id"])

    def _save_prefs(self) -> None:
        if self.prefs_path is None:
            return
        self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
        self.prefs_path.write_text(
            json.dumps(
                {
                    "base_url": self.base_url,
                    "secret": self.secret,
                    "channel": self.channel,
                    "user": self.user,
                    "provider": self.provider,
                    "persona": self.persona,
                    "project": self.project,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def load_prefs(self) -> None:
        if self.prefs_path is None or not self.prefs_path.exists():
            return
        data = json.loads(self.prefs_path.read_text(encoding="utf-8"))
        self.base_url = str(data.get("base_url") or self.base_url)
        self.secret = str(data.get("secret") or self.secret)
        self.channel = str(data.get("channel") or self.channel)
        self.user = str(data.get("user") or self.user)
        self.provider = str(data.get("provider") or self.provider)
        self.persona = str(data.get("persona") or self.persona)
        self.project = str(data.get("project") or self.project)

    def _turn_body(self, text: str, *, new_session: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "channel": self.channel,
            "user": self.user,
            "text": text,
        }
        if self.session and not new_session:
            body["session"] = self.session
        if self.user_id:
            body["user_id"] = self.user_id
        if new_session:
            body["new_session"] = True
        if self.provider:
            body["provider"] = self.provider
        if self.persona:
            body["persona"] = self.persona
        if self.project:
            body["project"] = self.project
        return body

    def turn(self, text: str, *, new_session: bool = False) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}/v1/turn"
        payload = json.dumps(self._turn_body(text, new_session=new_session)).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"turn HTTP {exc.code}: {err}") from exc
        self._apply_meta(data)
        self._save_prefs()
        return data

    def stream(self, text: str, *, new_session: bool = False) -> Iterator[dict[str, Any]]:
        url = f"{self.base_url.rstrip('/')}/v1/stream"
        payload = json.dumps(self._turn_body(text, new_session=new_session)).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=self._headers(), method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as exc:
            err = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"stream HTTP {exc.code}: {err}") from exc
        with resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:].strip())
                if data.get("type") == "trailer":
                    self._apply_meta(data)
                    self._save_prefs()
                yield data

    def health(self) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
