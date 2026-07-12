#!/usr/bin/env python3
from pathlib import Path
import sys


Path(sys.argv[1], "restore.py").write_text(
    '''"""Project backup restoration."""

from __future__ import annotations

from pathlib import Path
import tarfile


def restore_project(archive_path: str | Path, destination: str | Path) -> list[str]:
    """Restore a project backup and return its regular-file paths."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r") as archive:
        files = sorted(member.name for member in archive.getmembers() if member.isfile())
        archive.extractall(destination)
    return files
''',
    encoding="utf-8",
)
