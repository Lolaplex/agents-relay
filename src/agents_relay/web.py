"""Static /app serving: the herd PWA hosted same-origin by the relay.

Same-origin kills CORS for the pinned-per-relay case: a phone can pin
<relay>/app/ as an installable PWA and talk to /v1/* without any
cross-origin fetch. Multi-instance herd aggregation stays a client-side
concern (and, when needed, a CORS-allowlist change upstream — separate).
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path

_DEFAULT_WEB_DIR = Path(__file__).resolve().parent / "web"
_INDEX_NAMES = ("index.html",)


def web_root() -> Path:
    override = os.environ.get("AGENTS_RELAY_WEB_DIR", "").strip()
    return Path(override) if override else _DEFAULT_WEB_DIR


def resolve_app_path(url_path: str) -> Path | None:
    """Map /app/* to a file under the web root, or None for traversal/missing."""
    rel = url_path.removeprefix("/app").lstrip("/")
    root = web_root()
    try:
        candidate = (root / rel).resolve()
    except OSError:
        return None
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    if candidate.is_dir():
        for name in _INDEX_NAMES:
            index = candidate / name
            if index.is_file():
                return index
        return None
    return candidate if candidate.is_file() else None


def guess_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime
    if path.suffix == ".webmanifest":
        return "application/manifest+json"
    return "application/octet-stream"
