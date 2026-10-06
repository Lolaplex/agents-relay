"""Turn attachments: local paths and http(s) downloads into the relay inbox."""

from __future__ import annotations

import logging
import re
import uuid
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("agents_relay.attachments")

_MAX_BYTES = 20 * 1024 * 1024
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/json": ".json",
}


def safe_filename(name: str) -> str:
    base = Path(name or "").name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")[:80]
    return base or "file"


def materialize_attachments(items: list, dest_dir: Path) -> list[str]:
    """Resolve `{path|url, mime}` objects to local filesystem paths."""
    paths: list[str] = []
    if not isinstance(items, list):
        return paths
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_path = str(item.get("path") or "").strip()
        raw_url = str(item.get("url") or "").strip()
        mime = str(item.get("mime") or "").split(";", 1)[0].strip().lower()
        if raw_path:
            path = Path(raw_path).expanduser()
            if path.is_file():
                paths.append(str(path))
            else:
                log.warning("attachment path missing: %s", raw_path)
            continue
        if raw_url:
            saved = download_http(raw_url, dest_dir, mime=mime)
            if saved is not None:
                paths.append(str(saved))
    return paths


def download_http(url: str, dest_dir: Path, *, mime: str = "") -> Path | None:
    if not (url.startswith("https://") or url.startswith("http://")):
        log.warning("attachment url rejected (http/https only)")
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = _MIME_EXT.get(mime) or _ext_from_url(url)
    target = dest_dir / f"url_{uuid.uuid4().hex[:8]}{ext}"
    req = urllib.request.Request(url, headers={"User-Agent": "agents-relay"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            chunks: list[bytes] = []
            total = 0
            while True:
                block = resp.read(64 * 1024)
                if not block:
                    break
                total += len(block)
                if total > _MAX_BYTES:
                    log.warning("attachment url exceeds %s bytes", _MAX_BYTES)
                    return None
                chunks.append(block)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("attachment download failed: %s", exc.__class__.__name__)
        return None
    target.write_bytes(b"".join(chunks))
    return target


def _ext_from_url(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0]
    suffix = Path(path).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return suffix
    return ".bin"
