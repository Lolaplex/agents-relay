"""Filesystem layout for human client-* apps under the fleet home."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Human UI config lives here — not brain/memory/traces/identity (those stay under ~/.agents).
SURFACES = ("host", "overlay")


def agents_home() -> Path:
    raw = os.environ.get("AGENTS_HOME", "").strip()
    return Path(raw) if raw else Path.home() / ".agents"


def clients_dir() -> Path:
    """~/.agents/clients (or $AGENTS_HOME/clients)."""
    return agents_home() / "clients"


def client_surface_dir(surface: str) -> Path:
    if surface not in SURFACES:
        raise ValueError(f"unknown client surface: {surface}")
    return clients_dir() / surface


def host_profiles_path() -> Path:
    return client_surface_dir("host") / "profiles.json"


def overlay_config_path() -> Path:
    return client_surface_dir("overlay") / "config.json"


def migrate_legacy_dot_client() -> None:
    """One-time lift from deprecated ~/.client/ if the new tree is empty."""
    legacy = Path.home() / ".client"
    if not legacy.is_dir():
        return
    mapping = {
        legacy / "profiles.json": host_profiles_path(),
        legacy / "overlay.json": overlay_config_path(),
    }
    for src, dst in mapping.items():
        if not src.is_file() or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
