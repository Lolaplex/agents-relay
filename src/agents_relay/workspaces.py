"""Workspace registry for the relay."""

from __future__ import annotations

from agents_workspace import Workspace


def list_workspaces() -> list[Workspace]:
    return Workspace.list_all()


def create_workspace(label: str) -> Workspace:
    return Workspace.create(label)
