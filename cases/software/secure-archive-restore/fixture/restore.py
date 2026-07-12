"""Project backup restoration."""

from __future__ import annotations

from pathlib import Path


def restore_project(archive_path: str | Path, destination: str | Path) -> list[str]:
    """Restore a project backup and return its regular-file paths."""
    raise NotImplementedError
