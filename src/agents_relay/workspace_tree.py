"""Directory names a workspace file listing should not descend into."""

from __future__ import annotations

from .attachments import MIRROR_DIRNAME

DEFAULT_SKIP_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    MIRROR_DIRNAME,
})
