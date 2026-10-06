"""Turn attachments: local paths and http(s) downloads into the relay inbox."""

from __future__ import annotations

import logging
import re
import shutil
import uuid
import urllib.error
import urllib.request
from pathlib import Path

from .config import RelayConfig
from .state import resolve_state_dir

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


class AttachmentRejected(ValueError):
    """Local attachment path is outside the allowed directory."""


def safe_filename(name: str) -> str:
    base = Path(name or "").name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")[:80]
    return base or "file"


def extension_for_mime(mime: str) -> str:
    base = (mime or "").split(";", 1)[0].strip().lower()
    return _MIME_EXT.get(base, "")


def allowed_attach_root(config: RelayConfig | None) -> Path:
    """Local `/v1/turn` paths must live here. Override with `AGENTS_RELAY_ATTACH_DIR`."""
    if config is not None and config.attach_dir:
        return Path(config.attach_dir).expanduser()
    return resolve_state_dir(config)


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def materialize_attachments(
    items: list,
    dest_dir: Path,
    *,
    allowed_root: Path | None = None,
) -> list[str]:
    """Resolve `{path|url, mime}` objects to local filesystem paths.

    Local paths must resolve inside `allowed_root`. Missing `allowed_root`
    rejects every local path. `mime` sets the saved file extension when it
    disagrees with the source name.
    """
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
            try:
                saved = _local_attachment(raw_path, dest_dir, mime, allowed_root)
            except _SkipAttachment:
                continue
            paths.append(str(saved))
            continue
        if raw_url:
            saved = download_http(raw_url, dest_dir, mime=mime)
            if saved is not None:
                paths.append(str(saved))
    return paths


class _SkipAttachment(Exception):
    pass


def _local_attachment(raw_path: str, dest_dir: Path, mime: str, allowed_root: Path | None) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_file():
        log.warning("attachment path missing: %s", raw_path)
        raise _SkipAttachment()
    if allowed_root is None or not _is_inside(path, allowed_root):
        log.warning("rejected attachment outside allowed dir")
        raise AttachmentRejected("attachment path outside allowed directory")
    return _with_mime_extension(path, dest_dir, mime)


def _with_mime_extension(path: Path, dest_dir: Path, mime: str) -> Path:
    ext = extension_for_mime(mime)
    if not ext or path.suffix.lower() == ext:
        return path
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{safe_filename(path.stem)}_{uuid.uuid4().hex[:6]}{ext}"
    shutil.copyfile(path, target)
    return target


def download_http(url: str, dest_dir: Path, *, mime: str = "") -> Path | None:
    if not (url.startswith("https://") or url.startswith("http://")):
        log.warning("attachment url rejected (http/https only)")
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = extension_for_mime(mime) or _ext_from_url(url)
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
