"""Turn attachments: local paths and http(s) downloads into the relay inbox.

Accepted files are copied into the session workdir so jailed file tools can
read them. Image parts are the harness's job; this module only decides
whether the configured provider claims vision, and says so when it does not.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import RelayConfig
from .state import resolve_state_dir

log = logging.getLogger("agents_relay.attachments")

_MAX_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENT_BYTES = _MAX_BYTES
# Copies live here, under the session workdir, never next to project files.
MIRROR_DIRNAME = ".relay-inbox"
IMAGE_UNAVAILABLE_PREFIX = "Image not shown to the model:"
_MIRROR_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".pdf", ".txt", ".md", ".json", ".csv",
    ".html", ".xml", ".yaml", ".yml", ".log",
})
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_ON = frozenset({"1", "true", "yes", "on"})
_OFF = frozenset({"0", "false", "no", "off"})
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
    notes: list[str] | None = None,
) -> list[str]:
    """Resolve `{path|url|data, mime}` objects to local filesystem paths.

    Local paths must resolve inside `allowed_root`. Missing `allowed_root`
    rejects every local path. `mime` sets the saved file extension when it
    disagrees with the source name. Failures are appended to `notes` when
    that list is passed; the caller shows them instead of dropping the file
    silently.
    """
    paths: list[str] = []
    if not isinstance(items, list):
        _note(notes, "Attachment dropped: attachments must be a list.")
        return paths
    for item in items:
        if not isinstance(item, dict):
            _note(notes, "Attachment dropped: invalid item.")
            continue
        raw_path = str(item.get("path") or "").strip()
        raw_url = str(item.get("url") or "").strip()
        raw_data = str(item.get("data") or "").strip()
        mime = str(item.get("mime") or "").split(";", 1)[0].strip().lower()
        if raw_path:
            try:
                saved = _local_attachment(raw_path, dest_dir, mime, allowed_root)
            except _SkipAttachment as exc:
                _note(notes, f"Attachment dropped: {exc.reason}.")
                continue
            paths.append(str(saved))
            continue
        if raw_data:
            saved = _save_b64(
                raw_data, dest_dir, mime=mime,
                name=str(item.get("name") or ""))
            if saved is None:
                _note(notes, "Attachment dropped: inline data was empty, not base64, or larger than 20 MB.")
            else:
                paths.append(str(saved))
            continue
        if raw_url:
            saved = download_http(raw_url, dest_dir, mime=mime)
            if saved is None:
                _note(notes, "Attachment dropped: URL was rejected, too large, or the download failed.")
            else:
                paths.append(str(saved))
            continue
        _note(notes, "Attachment dropped: missing path, url, or data.")
    return paths


def _note(notes: list[str] | None, text: str) -> None:
    if notes is not None and text:
        notes.append(text)


def _save_b64(data: str, dest_dir: Path, *, mime: str = "", name: str = "") -> Path | None:
    """Decode a base64 upload from HTTP clients into the turn inbox."""
    import base64

    if len(data) > _MAX_BYTES * 2:
        log.warning("attachment data too large")
        return None
    try:
        blob = base64.b64decode(data, validate=True)
    except Exception:
        log.warning("attachment data is not base64")
        return None
    if not blob or len(blob) > _MAX_BYTES:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = extension_for_mime(mime) or _kept_suffix(name)
    stem = safe_filename(Path(name).stem) if name else "upload"
    target = dest_dir / f"{stem}_{uuid.uuid4().hex[:6]}{ext}"
    target.write_bytes(blob)
    return target


class _SkipAttachment(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _local_attachment(raw_path: str, dest_dir: Path, mime: str, allowed_root: Path | None) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_file():
        log.warning("attachment path missing: %s", raw_path)
        raise _SkipAttachment("file not found")
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


def read_capped(stream, limit: int = _MAX_BYTES) -> bytes | None:
    """Read a binary stream up to `limit` bytes. Over the cap returns None."""
    chunks: list[bytes] = []
    total = 0
    while True:
        block = stream.read(64 * 1024)
        if not block:
            break
        total += len(block)
        if total > limit:
            log.warning("attachment exceeds %s bytes", limit)
            return None
        chunks.append(block)
    return b"".join(chunks)


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
            blob = read_capped(resp, _MAX_BYTES)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("attachment download failed: %s", exc.__class__.__name__)
        return None
    if blob is None:
        return None
    target.write_bytes(blob)
    return target


def _ext_from_url(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0]
    return _kept_suffix(path)


def _kept_suffix(name: str) -> str:
    suffix = Path(name or "").suffix.lower()
    if suffix in _MIRROR_SUFFIXES:
        return suffix
    return ".bin"


def is_image_path(path: str) -> bool:
    return Path(path or "").suffix.lower() in _IMAGE_SUFFIXES


def apply_notes(reply: str, notes: list[str] | tuple[str, ...]) -> str:
    """Append note lines the reply does not already contain."""
    body = reply or ""
    missing = [note for note in notes if note and note not in body]
    if not missing:
        return body
    extra = "\n\n".join(missing)
    if not body.strip():
        return extra
    return body.rstrip() + "\n\n" + extra


def materialize_for_turn(
    items: list,
    *,
    config: RelayConfig | None,
    workspace_id: str = "",
    notes: list[str] | None = None,
) -> list[str]:
    """Save request attachments into the turn workspace's relay inbox."""
    ws = None
    if (workspace_id or "").strip():
        from .turn import resolve_workspace_id

        ws = resolve_workspace_id(workspace_id)
    if ws is not None:
        dest = ws.relay_state / "inbox"
        allowed = ws.relay_state
    else:
        from .state import inbox_dir

        dest = inbox_dir(config)
        allowed = allowed_attach_root(config)
    return materialize_attachments(items, dest, allowed_root=allowed, notes=notes)


@dataclass(frozen=True)
class PreparedAttachments:
    paths: tuple[str, ...]
    notes: tuple[str, ...]
    allow_root: str = ""


def prepare_turn_files(
    paths: list[str] | tuple[str, ...],
    *,
    config: RelayConfig | None,
    workspace: str = "",
    workdir: str = "",
) -> PreparedAttachments:
    """Copy trusted attachments into `<workdir>/.relay-inbox`.

    File tools (`read` and the rest) only open the session workdir. The relay
    inbox is outside that root, so a path there is invisible to them. The copy
    uses a generated name, refuses symlinks, and stays under `.relay-inbox`.
    Paths the relay did not accept are left unchanged. With no workdir, an
    inbox directory is returned as `allow_root` for `AGENTS_WORKDIR_ALLOW`.
    """
    roots = _trust_roots(config, workspace)
    session = _session_workdir(workspace, workdir)
    out: list[str] = []
    notes: list[str] = []
    allow: list[str] = []
    for raw in paths:
        text = str(raw or "").strip()
        if not text:
            continue
        src = Path(text).expanduser()
        if not _trusted(src, roots):
            out.append(text)
            continue
        reason = _unsafe_reason(src)
        if reason:
            notes.append(f"Attachment dropped: {reason}.")
            continue
        if session is not None and _is_inside(src, session):
            out.append(str(src))
            continue
        copied = None if session is None else mirror_regular_file(src, session)
        if copied is None:
            out.append(str(src))
            inbox = _inbox_for(src, roots)
            if inbox is not None:
                allow.append(str(inbox))
                notes.append(
                    "Attachment kept in the relay inbox; file tools may not read it: "
                    + src.name
                    + "."
                )
            else:
                notes.append(
                    "Attachment is outside the workdir, so file tools may not read it: "
                    + src.name
                    + "."
                )
            continue
        out.append(str(copied))
    unique_allow = list(dict.fromkeys(allow))
    return PreparedAttachments(tuple(out), tuple(notes), os.pathsep.join(unique_allow))


def vision_mode(config: RelayConfig | None) -> str:
    """`on`, `off`, or `auto`. Explicit relay config wins over `AGENTS_VISION`."""
    raw = ""
    if config is not None:
        raw = (config.vision or "").strip().lower()
    if not raw:
        raw = os.environ.get("AGENTS_RELAY_VISION", "").strip().lower()
    if raw in _ON:
        return "on"
    if raw in _OFF:
        return "off"
    return "auto"


def vision_active(config: RelayConfig | None, provider: str) -> bool:
    """Whether this turn should send image parts.

    `AGENTS_RELAY_VISION=on|off` forces it. Otherwise `AGENTS_VISION` is the
    harness override. `auto` reads the provider manifest: `vision: true` or a
    `capabilities` list containing `vision`. No such claim means unsupported.
    """
    mode = vision_mode(config)
    if mode == "on":
        return True
    if mode == "off":
        return False
    flag = os.environ.get("AGENTS_VISION", "").strip().lower()
    if flag in _ON:
        return True
    if flag in _OFF:
        return False
    manifest = find_provider_manifest(provider)
    if manifest is None:
        return False
    return _manifest_has_vision(manifest)


def vision_notice(paths: list[str] | tuple[str, ...], *, active: bool) -> str:
    """User-visible line when an image will not be sent to the model."""
    if active:
        return ""
    images = [path for path in paths if is_image_path(path)]
    if not images:
        return ""
    lines = "\n".join(f"- {path}" for path in images)
    mirrored = [path for path in images if _in_mirror(path)]
    text = (
        f"{IMAGE_UNAVAILABLE_PREFIX} vision is off or this provider does not "
        "advertise image support.\n"
        + lines
    )
    if mirrored:
        text += "\nFile tools can read the copy under the workdir `.relay-inbox` directory."
    return text


def vision_env(
    config: RelayConfig | None,
    *,
    active: bool | None = None,
    provider: str = "",
) -> dict[str, str]:
    """Child ``AGENTS_VISION`` for this turn.

    ``on`` and ``off`` force the flag. ``auto`` still passes the resolved
    decision (``1`` or ``0``) so the harness cannot send an image the relay
    has already told the user it withheld.
    """
    mode = vision_mode(config)
    if mode == "on":
        decided = True
    elif mode == "off":
        decided = False
    elif active is not None:
        decided = active
    else:
        decided = vision_active(config, provider)
    return {"AGENTS_VISION": "1" if decided else "0"}


def find_provider_manifest(name: str) -> dict | None:
    """Provider JSON by name. Overlay (`AGENTS_PROVIDERS_DIR`) wins over the installed package.

    Reads data files only. Does not import the harness.
    """
    filename = _provider_filename(name)
    if not filename:
        return None
    found: dict | None = None
    bundled = _bundled_providers_dir()
    if bundled is not None:
        found = _read_manifest(_match_file(bundled, filename))
    overlay = os.environ.get("AGENTS_PROVIDERS_DIR", "").strip()
    if overlay:
        data = _read_manifest(_match_file(Path(overlay).expanduser(), filename))
        if data is not None:
            found = data
    return found


def mirror_regular_file(src: Path, workdir: Path) -> Path | None:
    """Copy one regular file into `workdir/.relay-inbox`. None if that is unsafe."""
    mirror_root = _mirror_root(workdir)
    if mirror_root is None:
        return None
    nofollow = getattr(os, "O_NOFOLLOW", 0) or 0
    try:
        src_fd = os.open(src, os.O_RDONLY | nofollow)
    except OSError:
        return None
    dst_fd = -1
    dest: Path | None = None
    try:
        info = os.fstat(src_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES or info.st_size < 0:
            return None
        dest = mirror_root / f"file_{uuid.uuid4().hex[:12]}{_kept_suffix(src.name)}"
        try:
            dest.relative_to(mirror_root)
        except ValueError:
            return None
        dst_fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o644)
        copied = 0
        while True:
            block = os.read(src_fd, 64 * 1024)
            if not block:
                break
            copied += len(block)
            if copied > _MAX_BYTES:
                raise OSError("attachment exceeds cap during copy")
            view = block
            while view:
                wrote = os.write(dst_fd, view)
                if wrote <= 0:
                    raise OSError("short write")
                view = view[wrote:]
        os.fsync(dst_fd)
    except OSError:
        if dst_fd >= 0:
            os.close(dst_fd)
            dst_fd = -1
        if dest is not None:
            dest.unlink(missing_ok=True)
        return None
    finally:
        os.close(src_fd)
        if dst_fd >= 0:
            os.close(dst_fd)
    try:
        os.chmod(dest, 0o444)
    except OSError:
        pass
    try:
        if not dest.resolve(strict=True).is_relative_to(mirror_root):
            dest.unlink(missing_ok=True)
            return None
    except OSError:
        return None
    return dest


def _in_mirror(path: str) -> bool:
    parts = Path(path).parts
    return MIRROR_DIRNAME in parts


def _provider_filename(name: str) -> str:
    needle = (name or "").strip()
    if not needle or needle in {".", ".."}:
        return ""
    if any(sep in needle for sep in ("/", "\\", "\x00")):
        return ""
    if ".." in needle:
        return ""
    return f"{needle}.json"


def _manifest_has_vision(data: dict) -> bool:
    if data.get("vision") is True:
        return True
    caps = data.get("capabilities")
    if isinstance(caps, list) and "vision" in {str(item) for item in caps}:
        return True
    return False


def _bundled_providers_dir() -> Path | None:
    try:
        from importlib.metadata import distribution

        dist = distribution("agents-harness")
        root = Path(dist.locate_file("runner/providers"))
    except Exception:
        return None
    try:
        if root.is_dir() and not root.is_symlink():
            return root.resolve()
    except OSError:
        return None
    return None


def _match_file(directory: Path, filename: str) -> Path | None:
    try:
        if not directory.is_dir():
            return None
        direct = directory / filename
        if direct.is_file() and not direct.is_symlink():
            resolved = direct.resolve()
            if resolved.is_relative_to(directory.resolve()):
                return direct
        wanted = filename.lower()
        for entry in directory.iterdir():
            if entry.name.lower() != wanted or not entry.is_file() or entry.is_symlink():
                continue
            resolved = entry.resolve()
            if resolved.is_relative_to(directory.resolve()):
                return entry
    except (OSError, ValueError):
        return None
    return None


def _read_manifest(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _trust_roots(config: RelayConfig | None, workspace: str) -> list[Path]:
    roots: list[Path] = []
    if (workspace or "").strip():
        try:
            from .turn import resolve_workspace_id

            roots.append(resolve_workspace_id(workspace).relay_state)
        except Exception:
            pass
    if config is not None and config.attach_dir:
        roots.append(Path(config.attach_dir).expanduser())
    if config is not None and config.state_dir:
        roots.append(Path(config.state_dir).expanduser())
    elif not roots:
        try:
            from .state import inbox_dir

            inbox = inbox_dir(config)
            roots.append(inbox)
            roots.append(inbox.parent)
        except Exception:
            pass
    return _dedupe_paths(roots)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.expanduser().resolve(strict=False))
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _session_workdir(workspace: str, workdir: str) -> Path | None:
    """Workdir for this turn. Does not create a workspace."""
    explicit = (workdir or "").strip()
    if explicit:
        return _resolve_dir(Path(explicit))
    if (workspace or "").strip():
        try:
            from .turn import resolve_workspace_id

            return _resolve_dir(resolve_workspace_id(workspace).workdir)
        except Exception:
            return None
    raw = os.environ.get("AGENTS_WORKDIR", "").strip()
    if not raw:
        raw = os.environ.get("AGENTS_WORKSPACE_DIR", "").strip()
    if raw:
        return _resolve_dir(Path(raw))
    return None


def _resolve_dir(path: Path) -> Path | None:
    try:
        return path.expanduser().resolve(strict=False)
    except OSError:
        return None


def _trusted(src: Path, roots: list[Path]) -> bool:
    """True when `src` or its parent sits in a relay accept root.

    A symlink whose target leaves the root still counts: the caller must
    refuse the link instead of handing that path to the loop.
    """
    if not roots:
        return False
    for root in roots:
        if _is_inside(src, root):
            return True
        try:
            parent = src.expanduser().parent
        except (OSError, RuntimeError):
            continue
        if parent.exists() and _is_inside(parent, root):
            return True
    return False


def _unsafe_reason(src: Path) -> str:
    try:
        info = src.lstat()
    except OSError:
        return "file not found"
    if stat.S_ISLNK(info.st_mode):
        return "symlink refused"
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if info.st_size > _MAX_BYTES:
        return "file exceeds 20 MB"
    try:
        real = src.resolve(strict=True)
    except OSError:
        return "file not found"
    try:
        real_info = real.lstat()
    except OSError:
        return "file not found"
    if stat.S_ISLNK(real_info.st_mode) or not stat.S_ISREG(real_info.st_mode):
        return "not a regular file"
    if real_info.st_size > _MAX_BYTES:
        return "file exceeds 20 MB"
    return ""


def _inbox_for(src: Path, roots: list[Path]) -> Path | None:
    """Inbox directory containing `src`, if the file is already inside one."""
    try:
        real = src.resolve(strict=True)
    except OSError:
        return None
    for root in roots:
        try:
            base = root.expanduser().resolve(strict=False)
        except OSError:
            continue
        inbox = base if base.name == "inbox" else base / "inbox"
        try:
            inbox = inbox.resolve(strict=False)
            if real.is_relative_to(inbox):
                return inbox
        except (OSError, ValueError):
            continue
    return None


def _mirror_root(workdir: Path) -> Path | None:
    """Real `<workdir>/.relay-inbox` directory, or None if that path can escape."""
    try:
        root = workdir.expanduser().resolve(strict=False)
    except OSError:
        return None
    try:
        root.mkdir(parents=True, exist_ok=True)
        root_info = root.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return None
    mirror = root / MIRROR_DIRNAME
    try:
        mirror.mkdir(exist_ok=True)
        info = mirror.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    try:
        real = mirror.resolve(strict=True)
        if not real.is_relative_to(root.resolve(strict=True)):
            return None
    except OSError:
        return None
    if not _ensure_gitignore(real):
        return None
    return real


def _ensure_gitignore(mirror: Path) -> bool:
    """Keep ``git add`` in the workdir from committing attachment copies.

    The file contains ``*``. A symlink is not followed.
    """
    target = mirror / ".gitignore"
    try:
        info = target.lstat()
    except FileNotFoundError:
        return _write_gitignore(target)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return False
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return False
    if "*" in {line.strip() for line in text.splitlines()}:
        return True
    try:
        with target.open("a", encoding="utf-8") as handle:
            if text and not text.endswith("\n"):
                handle.write("\n")
            handle.write("*\n")
    except OSError:
        return False
    return True


def _write_gitignore(target: Path) -> bool:
    nofollow = getattr(os, "O_NOFOLLOW", 0) or 0
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o644)
    except OSError:
        return False
    try:
        os.write(fd, b"*\n")
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        target.unlink(missing_ok=True)
        return False
    try:
        os.close(fd)
    except OSError:
        return False
    return True
