"""Filesystem layout for human client-* apps under the host dir."""

from __future__ import annotations

import shutil
from pathlib import Path

from agents_workspace import Host

# Human UI config lives here — not brain/memory/traces/identity.
SURFACES = ("host", "overlay")


def host_dir() -> Path:
    return Host.from_env().dir


def clients_dir() -> Path:
    """``<host>/clients`` — UI prefs for client apps."""
    return Host.from_env().clients


def client_surface_dir(surface: str) -> Path:
    if surface not in SURFACES:
        raise ValueError(f"unknown client surface: {surface}")
    return clients_dir() / surface


def host_profiles_path() -> Path:
    return client_surface_dir("host") / "profiles.json"


def overlay_config_path() -> Path:
    return client_surface_dir("overlay") / "config.json"


_legacy_client_warned = False


def migrate_legacy_dot_client() -> None:
    """One-time lift from deprecated ~/.client/ if the new tree is empty."""
    global _legacy_client_warned
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
        if not _legacy_client_warned:
            _legacy_client_warned = True
            import warnings

            warnings.warn(
                f"legacy client dir {legacy} lifted into {dst.parent}; run "
                "`agents-workspace migrate` to retire it",
                DeprecationWarning,
                stacklevel=2,
            )
