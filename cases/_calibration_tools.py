"""Create the exact private executable bundle used by direct calibrators."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Iterator


_TOOL_NAMES = ("as", "gcc", "go", "ld", "node")
_GCC_COMPONENTS = ("cc1", "collect2", "lto-wrapper")
_SANDBOX_TOOL_NAMES = ("env", "mount", "setpriv", "unshare")


def _resolve_tool(name: str) -> Path:
    raw = shutil.which(name)
    if raw is None:
        raise RuntimeError(f"required calibration tool is unavailable: {name}")
    path = Path(raw).resolve(strict=True)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise RuntimeError(f"calibration tool is not executable: {path}")
    return path


def _copy_executable(source: Path, target: Path) -> None:
    descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"calibration tool is not a regular file: {source}")
        with os.fdopen(os.dup(descriptor), "rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
    finally:
        os.close(descriptor)
    target.chmod(0o500)


def sandbox_tool_paths() -> dict[str, str]:
    """Resolve the fixed host executables used to create the nested sandbox."""

    return {name: str(_resolve_tool(name)) for name in _SANDBOX_TOOL_NAMES}


def _go_root(go: Path) -> Path:
    completed = subprocess.run(
        [str(go), "env", "GOROOT"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        shell=False,
        env={
            "GOENV": "off",
            "GOTOOLCHAIN": "local",
            "HOME": "/nonexistent",
            "PATH": "/usr/bin:/bin",
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(f"cannot resolve Go root: {completed.stderr.strip()}")
    raw = completed.stdout.strip()
    root = Path(raw)
    if not raw or not root.is_absolute() or not root.resolve().is_dir():
        raise RuntimeError(f"Go returned an invalid GOROOT: {raw!r}")
    resolved = root.resolve()
    if resolved.is_relative_to(Path.home().resolve()):
        raise RuntimeError(
            f"calibration GOROOT may not be under the user home: {resolved}"
        )
    return resolved


def _gcc_components(gcc: Path) -> dict[str, Path]:
    components: dict[str, Path] = {}
    for name in _GCC_COMPONENTS:
        completed = subprocess.run(
            [str(gcc), f"-print-prog-name={name}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            shell=False,
            env={"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"},
        )
        raw = completed.stdout.strip()
        candidate = Path(raw)
        if (
            completed.returncode != 0
            or not raw
            or not candidate.is_absolute()
            or not candidate.resolve().is_file()
            or not os.access(candidate.resolve(), os.X_OK)
        ):
            raise RuntimeError(f"cannot resolve GCC component {name}: {raw!r}")
        components[name] = candidate.resolve()
    return components


def _gcc_runtime_prefix(gcc: Path) -> Path:
    completed = subprocess.run(
        [str(gcc), "-print-libgcc-file-name"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        shell=False,
        env={"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"},
    )
    raw = completed.stdout.strip()
    library = Path(raw)
    if (
        completed.returncode != 0
        or not raw
        or not library.is_absolute()
        or not library.resolve().is_file()
    ):
        raise RuntimeError(f"cannot resolve GCC runtime prefix: {raw!r}")
    resolved_library = library.resolve()
    if len(resolved_library.parents) < 3:
        raise RuntimeError(f"GCC library path is too shallow: {resolved_library}")
    prefix = resolved_library.parents[2]
    if prefix.is_relative_to(Path.home().resolve()):
        raise RuntimeError(f"GCC runtime prefix may not be under user home: {prefix}")
    return prefix


@contextmanager
def private_tool_environment() -> Iterator[dict[str, str]]:
    """Yield a closed-PATH environment with every evaluator tool copied by FD."""

    sources = {name: _resolve_tool(name) for name in _TOOL_NAMES}
    gcc_components = _gcc_components(sources["gcc"])
    gcc_prefix = _gcc_runtime_prefix(sources["gcc"])
    python = Path(sys.executable).resolve(strict=True)
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError(f"Python interpreter is not executable: {python}")
    go_root = _go_root(sources["go"])
    with tempfile.TemporaryDirectory(prefix="skill-calibration-tools-") as raw_root:
        root = Path(raw_root)
        tool_bin = root / "bin"
        tool_bin.mkdir(mode=0o700)
        _copy_executable(python, tool_bin / "python3")
        for name, source in sources.items():
            _copy_executable(source, tool_bin / name)
        for name, source in gcc_components.items():
            _copy_executable(source, tool_bin / name)
        pycache = root / "pycache"
        temporary = root / "tmp"
        go_cache = root / "go-cache"
        module_cache = root / "go-mod-cache"
        for directory in (pycache, temporary, go_cache, module_cache):
            directory.mkdir(mode=0o700)
        yield {
            "COMPILER_PATH": str(tool_bin),
            "EVAL_GCC_EXEC_PREFIX": str(gcc_prefix) + os.sep,
            "EVAL_GO_ROOT": str(go_root),
            "EVAL_TOOL_BIN": str(tool_bin),
            "GOCACHE": str(go_cache),
            "GOENV": "off",
            "GOMODCACHE": str(module_cache),
            "GOPROXY": "off",
            "GCC_EXEC_PREFIX": str(gcc_prefix) + os.sep,
            "GOROOT": str(go_root),
            "GOTOOLCHAIN": "local",
            "GOWORK": "off",
            "HOME": "/nonexistent",
            "PATH": str(tool_bin),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(pycache),
            "TMPDIR": str(temporary),
        }
