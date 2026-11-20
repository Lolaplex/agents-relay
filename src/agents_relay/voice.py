"""Voice in/out via the Groq API (stdlib urllib only).

Opt-in: ``GROQ_API_KEY`` enables speech-to-text for incoming Telegram voice
and audio messages.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger("agents_relay.voice")

GROQ_BASE = "https://api.groq.com/openai/v1"
STT_MODEL = "whisper-large-v3-turbo"


def _multipart(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = f"----relay{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
        )
    mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{file_path.name}\"\r\nContent-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), boundary


def transcribe_audio(path: Path, api_key: str, *, model: str = STT_MODEL) -> str:
    """Groq Whisper transcription. Empty string on failure."""
    if not api_key or not path.is_file():
        return ""
    body, boundary = _multipart({"model": model, "response_format": "json"}, "file", path)
    req = urllib.request.Request(
        f"{GROQ_BASE}/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        log.warning("groq transcription failed for %s", path.name)
        return ""
    return str(data.get("text") or "").strip()
