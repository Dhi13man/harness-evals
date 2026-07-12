#!/usr/bin/env python3
from pathlib import Path
import sys


Path(sys.argv[1], "restore.py").write_text(
    '''"""Project backup restoration."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile


MAX_ENTRIES = 128
MAX_FILE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 4_194_304


def _publish_create(staging: Path, destination: Path) -> None:
    """Atomically publish without replacing a competing destination on Linux."""
    import ctypes
    import errno
    import os

    at_fdcwd = -100
    rename_noreplace = 1
    library = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = library.renameat2
    except AttributeError as error:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renameat2(
        at_fdcwd,
        os.fsencode(staging),
        at_fdcwd,
        os.fsencode(destination),
        rename_noreplace,
    )
    if result != 0:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, os.strerror(code), str(destination))


class UnsafeBackup(Exception):
    """The backup cannot be restored safely."""


def _validate(archive_path: str | Path) -> None:
    seen: set[str] = set()
    total_size = 0
    with tarfile.open(archive_path, "r") as archive:
        for count, member in enumerate(archive, start=1):
            if count > MAX_ENTRIES:
                raise UnsafeBackup("archive has too many entries")
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise UnsafeBackup(f"unsafe archive path: {member.name}")
            normalized = relative.as_posix()
            if normalized in seen:
                raise UnsafeBackup(f"duplicate archive path: {member.name}")
            seen.add(normalized)
            if not (member.isfile() or member.isdir()):
                raise UnsafeBackup(f"unsupported archive entry: {member.name}")
            if member.isfile():
                if member.size > MAX_FILE_BYTES:
                    raise UnsafeBackup(f"archive entry too large: {member.name}")
                total_size += member.size
                if total_size > MAX_TOTAL_BYTES:
                    raise UnsafeBackup("archive content exceeds total size limit")


def restore_project(archive_path: str | Path, destination: str | Path) -> list[str]:
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise UnsafeBackup("destination already exists")
    _validate(archive_path)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging = Path(temporary) / "payload"
        staging.mkdir()
        restored: list[str] = []
        with tarfile.open(archive_path, "r") as archive:
            for member in archive:
                relative = PurePosixPath(member.name)
                target = staging.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise UnsafeBackup(f"unreadable archive entry: {member.name}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                restored.append(relative.as_posix())
        _publish_create(staging, destination)
    return sorted(restored)
''',
    encoding="utf-8",
)
