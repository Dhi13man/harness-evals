"""Launch candidate-controlled code without exposing parent oracle files."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import resource
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Sequence


MAX_CAPTURE_BYTES = 1024 * 1024
MAX_STDIN_BYTES = 1024 * 1024
MAX_SOURCE_FILE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_SOURCE_ENTRIES = 16_384
MAX_SOURCE_DEPTH = 64
CANDIDATE_TMPFS_BYTES = 512 * 1024 * 1024
CANDIDATE_TMPFS_INODES = 32_768
MAX_BUNDLED_TOOLS = 64
SANDBOX_FAILURE = 125


@dataclasses.dataclass(frozen=True)
class UntrustedResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_limited: bool = False
    sandbox_error: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.returncode == 0
            and not self.timed_out
            and not self.output_limited
            and self.sandbox_error is None
        )


def run_untrusted(
    command: Sequence[str],
    cwd: Path,
    timeout_seconds: int = 30,
    *,
    input_text: str | None = None,
) -> UntrustedResult:
    """Run a private tmpfs copy of ``cwd`` without exposing the host clone."""

    argv = _validate_command(command)
    work = _validate_cwd(cwd)
    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 300:
        raise ValueError("candidate timeout must be an integer between 1 and 300")
    input_bytes = None if input_text is None else input_text.encode("utf-8")
    if input_bytes is not None and len(input_bytes) > MAX_STDIN_BYTES:
        raise ValueError("candidate stdin exceeds the one MiB limit")

    required = {
        "unshare": _required_tool("EVAL_UNSHARE", "unshare"),
        "mount": _required_tool("EVAL_MOUNT", "mount"),
        "setpriv": _required_tool("EVAL_SETPRIV", "setpriv"),
        "env": _required_tool("EVAL_ENV", "env"),
    }
    tool_bin = _required_directory("EVAL_TOOL_BIN")
    host_uid = os.environ.get("EVAL_HOST_UID")
    if host_uid is None or not host_uid.isascii() or not host_uid.isdigit():
        raise RuntimeError("EVAL_HOST_UID must contain the host numeric uid")

    _work, work_fd = _open_validated_cwd(work)
    try:
        go_root = _optional_directory_descriptor("EVAL_GO_ROOT")
        gcc_exec_prefix = _optional_directory("EVAL_GCC_EXEC_PREFIX")
    except Exception:
        os.close(work_fd)
        raise
    status_read, status_write = os.pipe2(os.O_CLOEXEC)
    descriptors_handed_off = False
    try:
        with tempfile.TemporaryDirectory(prefix="candidate-masks-") as raw_masks:
            masks = Path(raw_masks)
            empty_home = masks / "home"
            empty_runtime = masks / "runtime"
            empty_var_tmp = masks / "var-tmp"
            empty_shared_memory = masks / "shared-memory"
            for directory in (
                empty_home,
                empty_runtime,
                empty_var_tmp,
                empty_shared_memory,
            ):
                directory.mkdir()
            trampoline = [
                required["unshare"],
                "--user",
                "--map-root-user",
                "--mount",
                "--pid",
                "--fork",
                "--mount-proc",
                "--kill-child",
                "--net",
                "--ipc",
                "--uts",
                sys.executable,
                str(Path(__file__).resolve()),
                "--trampoline",
                str(work_fd),
                str(tool_bin),
                str(status_write),
                host_uid,
                str(empty_home),
                str(empty_runtime),
                str(empty_var_tmp),
                str(empty_shared_memory),
                required["mount"],
                required["setpriv"],
                required["env"],
                str(go_root[1] if go_root is not None else -1),
                str(go_root[0]) if go_root is not None else "",
                str(gcc_exec_prefix) if gcc_exec_prefix is not None else "",
                "--",
                *argv,
            ]
            inherited = (status_write, work_fd)
            if go_root is not None:
                inherited += (go_root[1],)
            descriptors_handed_off = True
            return _execute(
                trampoline,
                cwd=Path("/"),
                timeout_seconds=timeout_seconds,
                input_bytes=input_bytes,
                pass_fds=inherited,
                status_read=status_read,
                status_write=status_write,
            )
    finally:
        if not descriptors_handed_off:
            for descriptor in (status_read, status_write):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        os.close(work_fd)
        if go_root is not None:
            os.close(go_root[1])


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)) or not command:
        raise ValueError("candidate command must be a non-empty string sequence")
    argv = tuple(command)
    if any(not isinstance(item, str) or not item or "\0" in item for item in argv):
        raise ValueError("candidate command contains an invalid argument")
    return argv


def _validate_cwd(cwd: Path) -> Path:
    raw = Path(cwd)
    resolved = raw.resolve(strict=True)
    if raw.is_symlink() or not resolved.is_dir():
        raise ValueError(f"candidate cwd must be a regular directory: {raw}")
    workspace = _required_directory("EVAL_WORKSPACE").resolve()
    private_tmp = Path(tempfile.gettempdir()).resolve()
    if resolved.is_relative_to(workspace):
        raise ValueError("candidate cwd must be a disposable clone of EVAL_WORKSPACE")
    if not resolved.is_relative_to(private_tmp):
        raise ValueError(
            f"candidate cwd is outside private temporary storage: {resolved}"
        )
    return resolved


def _open_validated_cwd(cwd: Path) -> tuple[Path, int]:
    resolved = _validate_cwd(cwd)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(resolved, flags)
    descriptor_stat = os.fstat(descriptor)
    path_stat = resolved.stat(follow_symlinks=False)
    if not stat.S_ISDIR(descriptor_stat.st_mode) or (
        descriptor_stat.st_dev,
        descriptor_stat.st_ino,
    ) != (path_stat.st_dev, path_stat.st_ino):
        os.close(descriptor)
        raise ValueError(f"candidate cwd changed during validation: {resolved}")
    return resolved, descriptor


def _required_directory(variable: str) -> Path:
    raw = os.environ.get(variable)
    if not raw:
        raise RuntimeError(f"{variable} is not set")
    path = Path(raw).resolve(strict=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"{variable} is not a regular directory: {path}")
    return path


def _required_tool(variable: str, fallback: str) -> str:
    raw = os.environ.get(variable)
    if not raw:
        raise RuntimeError(
            f"{variable} must pin the required candidate sandbox tool: {fallback}"
        )
    path = Path(raw).resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"candidate sandbox tool is not executable: {path}")
    return str(path)


def _optional_directory_descriptor(variable: str) -> tuple[Path, int] | None:
    raw = os.environ.get(variable)
    if raw is None:
        return None
    if not raw or "\0" in raw:
        raise RuntimeError(f"{variable} is invalid")
    path = Path(raw).resolve(strict=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    metadata = os.fstat(descriptor)
    observed = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or (
        metadata.st_dev,
        metadata.st_ino,
    ) != (observed.st_dev, observed.st_ino):
        os.close(descriptor)
        raise RuntimeError(f"{variable} changed during validation: {path}")
    return path, descriptor


def _optional_directory(variable: str) -> Path | None:
    raw = os.environ.get(variable)
    if raw is None:
        return None
    if not raw or "\0" in raw:
        raise RuntimeError(f"{variable} is invalid")
    path = Path(raw).resolve(strict=True)
    if not path.is_dir():
        raise RuntimeError(f"{variable} is not a directory: {path}")
    return path


def _execute(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    input_bytes: bytes | None,
    pass_fds: tuple[int, ...],
    status_read: int,
    status_write: int,
) -> UntrustedResult:
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=True,
            preexec_fn=_disable_child_core_dumps,
        )
    except OSError as error:
        os.close(status_read)
        os.close(status_write)
        return UntrustedResult(
            SANDBOX_FAILURE,
            "",
            "",
            sandbox_error=f"candidate sandbox failed to start: {error}",
        )
    os.close(status_write)
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    output_limited = threading.Event()
    kill_lock = threading.Lock()

    def kill_process_group() -> None:
        with kill_lock:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def read_bounded(stream: object, destination: bytearray) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            remaining = MAX_CAPTURE_BYTES - len(destination)
            if remaining > 0:
                destination.extend(chunk[:remaining])
            if len(chunk) > remaining:
                output_limited.set()
                kill_process_group()
                return

    def write_input() -> None:
        if input_bytes is None or process.stdin is None:
            return
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    assert process.stdout is not None
    assert process.stderr is not None
    readers = [
        threading.Thread(target=read_bounded, args=(process.stdout, stdout_buffer)),
        threading.Thread(target=read_bounded, args=(process.stderr, stderr_buffer)),
    ]
    writer = threading.Thread(target=write_input)
    for thread in (*readers, writer):
        thread.start()
    timed_out = False
    termination_error: str | None = None
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_group()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_process_group()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                termination_error = "candidate process group survived repeated SIGKILL"
    finally:
        for thread in (*readers, writer):
            thread.join(timeout=5)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
    status = _read_status(status_read)
    sandbox_error = termination_error
    if status != "READY":
        setup_error = status.removeprefix("ERROR:") or "no setup status returned"
        sandbox_error = (
            f"{sandbox_error}; {setup_error}" if sandbox_error else setup_error
        )
    return UntrustedResult(
        process.returncode if process.returncode is not None else SANDBOX_FAILURE,
        stdout_buffer.decode("utf-8", errors="replace"),
        stderr_buffer.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        output_limited=output_limited.is_set(),
        sandbox_error=sandbox_error,
    )


def _disable_child_core_dumps() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _read_status(descriptor: int) -> str:
    try:
        payload = os.read(descriptor, 4096)
    finally:
        os.close(descriptor)
    return payload.decode("utf-8", errors="replace").strip()


def _mount(mount_tool: str, *arguments: str, pass_fds: tuple[int, ...] = ()) -> None:
    completed = subprocess.run(
        [mount_tool, *arguments],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        close_fds=True,
        pass_fds=pass_fds,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"mount {' '.join(arguments)} failed: {detail}")


def _trampoline(arguments: list[str]) -> int:
    if len(arguments) < 16 or arguments[14] != "--":
        raise ValueError("invalid candidate trampoline arguments")
    raw_cwd_fd, raw_tool_bin, raw_status_fd = arguments[:3]
    status_fd = int(raw_status_fd)
    (
        host_uid,
        empty_home,
        empty_runtime,
        empty_var_tmp,
        empty_shared_memory,
    ) = arguments[3:8]
    mount_tool, setpriv_tool, env_tool = arguments[8:11]
    go_root_fd = int(arguments[11])
    raw_go_root = arguments[12]
    gcc_exec_prefix = arguments[13]
    candidate = arguments[15:]
    if not candidate:
        raise ValueError("candidate trampoline received no command")

    cwd_fd = int(raw_cwd_fd)
    if not stat.S_ISDIR(os.fstat(cwd_fd).st_mode):
        raise RuntimeError("candidate source descriptor is not a directory")
    tool_fds = _open_bundled_tools(raw_tool_bin, candidate[0])

    runtime_path = Path("/run/user") / host_uid
    if not runtime_path.is_dir():
        raise RuntimeError(f"host runtime directory is missing: {runtime_path}")
    read_only_masks = (
        (empty_home, "/home"),
        (empty_runtime, "/run"),
        (empty_var_tmp, "/var/tmp"),
        (empty_shared_memory, "/dev/shm"),
    )
    for source, target in read_only_masks:
        _mount(mount_tool, "--bind", source, target)
        _mount(
            mount_tool,
            "-o",
            "remount,bind,ro,nosuid,nodev,noexec",
            target,
        )
    _mount(
        mount_tool,
        "-t",
        "tmpfs",
        "-o",
        (
            "mode=1777,nosuid,nodev,"
            f"size={CANDIDATE_TMPFS_BYTES},nr_inodes={CANDIDATE_TMPFS_INODES}"
        ),
        "tmpfs",
        "/tmp",
    )
    workspace = Path("/tmp/workspace")
    tool_bin = Path("/tmp/tool-bin")
    home = Path("/tmp/home")
    for directory in (workspace, tool_bin, home):
        directory.mkdir(mode=0o700)
    _copy_source_tree(cwd_fd, workspace)
    mounted_go_root: Path | None = None
    if go_root_fd >= 0:
        if not stat.S_ISDIR(os.fstat(go_root_fd).st_mode):
            raise RuntimeError("Go runtime root descriptor is not a directory")
        mounted_go_root = Path(raw_go_root)
        if not mounted_go_root.is_absolute() or not mounted_go_root.is_dir():
            raise RuntimeError("Go runtime root mountpoint is invalid")
        _mount(
            mount_tool,
            "--bind",
            f"/proc/self/fd/{go_root_fd}",
            str(mounted_go_root),
            pass_fds=(go_root_fd,),
        )
        _mount(
            mount_tool,
            "-o",
            "remount,bind,ro,nosuid,nodev",
            str(mounted_go_root),
        )
    for name, descriptor in tool_fds.items():
        bundled_tool = tool_bin / name
        bundled_tool.touch(mode=0o500)
        _mount(
            mount_tool,
            "--bind",
            f"/proc/self/fd/{descriptor}",
            str(bundled_tool),
            pass_fds=(descriptor,),
        )
        _mount(mount_tool, "-o", "remount,bind,ro", str(bundled_tool))
    top_level = Path(candidate[0]).name
    if top_level in tool_fds:
        candidate[0] = str(tool_bin / top_level)
    os.chdir(workspace)
    os.close(cwd_fd)
    for descriptor in tool_fds.values():
        os.close(descriptor)
    if go_root_fd >= 0:
        os.close(go_root_fd)

    candidate_environment = [
        "PATH=/tmp/tool-bin",
        "HOME=/tmp/home",
        "TMPDIR=/tmp",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
        "TZ=UTC",
        "GOCACHE=/tmp/go-cache",
        "GOMODCACHE=/tmp/go-mod-cache",
        "GOTOOLCHAIN=local",
        "NPM_CONFIG_CACHE=/tmp/npm-cache",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONPYCACHEPREFIX=/tmp/python-pycache",
        "PYTHONHASHSEED=0",
    ]
    if mounted_go_root is not None:
        candidate_environment.append(f"GOROOT={mounted_go_root}")
    candidate_environment.append(f"CGO_ENABLED={1 if 'gcc' in tool_fds else 0}")
    if gcc_exec_prefix:
        candidate_environment.extend(
            (
                f"GCC_EXEC_PREFIX={gcc_exec_prefix.rstrip('/')}/",
                "COMPILER_PATH=/tmp/tool-bin",
            )
        )
    os.umask(0o077)
    os.write(status_fd, b"READY\n")
    os.close(status_fd)
    exec_arguments = [
        setpriv_tool,
        "--bounding-set=-all",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--no-new-privs",
        env_tool,
        "-i",
        *candidate_environment,
        *candidate,
    ]
    os.execve(setpriv_tool, exec_arguments, {})
    raise AssertionError("execve returned")


def _open_bundled_tools(raw_tool_bin: str, command: str) -> dict[str, int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory = os.open(raw_tool_bin, flags)
    descriptors: dict[str, int] = {}
    try:
        names = sorted(os.listdir(directory))
        if len(names) > MAX_BUNDLED_TOOLS:
            raise RuntimeError("private tool bin exceeds the executable count limit")
        for name in names:
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & 0o111:
                raise RuntimeError(
                    f"private tool bin contains a non-executable entry: {name}"
                )
            tool_flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                tool_flags |= os.O_NOFOLLOW
            descriptor = os.open(name, tool_flags, dir_fd=directory)
            if _source_identity(os.fstat(descriptor)) != _source_identity(metadata):
                os.close(descriptor)
                raise RuntimeError(
                    f"private bundled tool changed while opening: {name}"
                )
            descriptors[name] = descriptor
    except Exception:
        for descriptor in descriptors.values():
            os.close(descriptor)
        raise
    finally:
        os.close(directory)
    name = Path(command).name
    if name not in descriptors and (Path(command).is_absolute() or "/" not in command):
        for descriptor in descriptors.values():
            os.close(descriptor)
        raise RuntimeError(
            f"candidate top-level command is absent from the private tool bin: {command}"
        )
    return descriptors


def _copy_source_tree(source_fd: int, destination: Path) -> None:
    counters = {"entries": 0, "bytes": 0}

    def copy_directory(directory_fd: int, target: Path, depth: int) -> None:
        if depth > MAX_SOURCE_DEPTH:
            raise RuntimeError(
                f"candidate source exceeds maximum depth {MAX_SOURCE_DEPTH}"
            )
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as error:
            raise RuntimeError(f"cannot enumerate candidate source: {error}") from error
        for name in names:
            if not name or name in {".", ".."} or "/" in name or "\0" in name:
                raise RuntimeError("candidate source contains an invalid entry name")
            counters["entries"] += 1
            if counters["entries"] > MAX_SOURCE_ENTRIES:
                raise RuntimeError(
                    f"candidate source exceeds maximum entries {MAX_SOURCE_ENTRIES}"
                )
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            child_target = target / name
            if stat.S_ISDIR(metadata.st_mode):
                child_target.mkdir(mode=0o700)
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if _source_identity(opened) != _source_identity(metadata):
                        raise RuntimeError(
                            f"candidate source directory changed during copy: {name}"
                        )
                    copy_directory(child_fd, child_target, depth + 1)
                    if _source_identity(os.fstat(child_fd)) != _source_identity(opened):
                        raise RuntimeError(
                            f"candidate source directory drifted during copy: {name}"
                        )
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                _copy_source_file(directory_fd, name, metadata, child_target, counters)
            else:
                raise RuntimeError(
                    f"candidate source contains a symlink or special entry: {name}"
                )

    copy_directory(source_fd, destination, 0)


def _copy_source_file(
    directory_fd: int,
    name: str,
    metadata: os.stat_result,
    destination: Path,
    counters: dict[str, int],
) -> None:
    if metadata.st_size > MAX_SOURCE_FILE_BYTES:
        raise RuntimeError(
            f"candidate source file exceeds {MAX_SOURCE_FILE_BYTES} bytes: {name}"
        )
    counters["bytes"] += metadata.st_size
    if counters["bytes"] > MAX_SOURCE_BYTES:
        raise RuntimeError(
            f"candidate source exceeds aggregate limit {MAX_SOURCE_BYTES} bytes"
        )
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    source = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(source)
        if _source_identity(opened) != _source_identity(metadata):
            raise RuntimeError(f"candidate source file changed during copy: {name}")
        target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        target = os.open(
            destination,
            target_flags,
            0o700 if metadata.st_mode & 0o111 else 0o600,
        )
        try:
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(source, min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError(
                        f"candidate source file was truncated during copy: {name}"
                    )
                view = memoryview(chunk)
                while view:
                    written = os.write(target, view)
                    view = view[written:]
                remaining -= len(chunk)
            if os.read(source, 1):
                raise RuntimeError(f"candidate source file grew during copy: {name}")
            os.fchmod(target, 0o700 if metadata.st_mode & 0o111 else 0o600)
        finally:
            os.close(target)
        if _source_identity(os.fstat(source)) != _source_identity(opened):
            raise RuntimeError(f"candidate source file drifted during copy: {name}")
    finally:
        os.close(source)


def _source_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _trampoline_main(arguments: list[str]) -> int:
    status_fd = -1
    try:
        if len(arguments) >= 3:
            status_fd = int(arguments[2])
        return _trampoline(arguments)
    except Exception as error:
        if status_fd >= 0:
            try:
                os.write(
                    status_fd,
                    f"ERROR:{type(error).__name__}: {error}\n".encode(
                        "utf-8", errors="replace"
                    ),
                )
            except OSError:
                pass
        return SANDBOX_FAILURE


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--trampoline":
        raise SystemExit(_trampoline_main(sys.argv[2:]))
    raise SystemExit("untrusted_child.py is an internal trampoline")
