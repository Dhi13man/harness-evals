"""Build and execute parent-owned Go oracles outside candidate modules.

The host kernel, Go compiler/linker, standard library, race runtime, and the final
binary remain trusted. Candidate package initialization shares the final process,
so every successful mode requires a complete parent-token response and exact
parent-side observation checks; this is isolation, not a claim that public
workloads cannot be recognized or gamed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
from typing import Any, Iterator

from verifier_lib import run


PROTOCOL = "external-go-oracle-v1"
PROBE_BINARY = "oracle-probe"
SOURCE_GUARD_TIMEOUT_SECONDS = 90


class GoOracleError(RuntimeError):
    """Raised when the external Go oracle cannot produce trustworthy evidence."""


@dataclasses.dataclass(frozen=True)
class GoModulePolicy:
    module_path: str
    package_name: str
    required_source: str
    go_version: str = "1.22"
    api_contract: str = ""


@dataclasses.dataclass(frozen=True)
class BuiltGoOracle:
    executable_workspace: Path
    go_version: str
    candidate_source_sha256: str
    excluded_test_files: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class GoModeResult:
    observations: dict[str, Any]
    stdout_sha256: str


@contextlib.contextmanager
def build_external_go_oracle(
    workspace: Path,
    policy: GoModulePolicy,
    harness_source: str,
    *,
    race: bool,
    timeout_seconds: int,
) -> Iterator[BuiltGoOracle]:
    """Compile an external binary, remove every source tree, then yield it."""

    candidate_root = Path(workspace).resolve(strict=True)
    if not candidate_root.is_dir() or candidate_root.is_symlink():
        raise GoOracleError(f"candidate module is not a regular directory: {workspace}")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        raise ValueError("Go build timeout must be an integer")
    if not 1 <= timeout_seconds <= 120:
        raise ValueError("Go build timeout must be between 1 and 120 seconds")

    go = _required_go()
    with tempfile.TemporaryDirectory(prefix="external-go-oracle-") as raw_root:
        root = Path(raw_root)
        environment = _go_environment(root, go, race=race)
        _validate_module(go, candidate_root, policy, environment, timeout_seconds)
        guarded = _inspect_candidate(
            go, candidate_root, policy, environment, timeout_seconds
        )

        candidate_copy = root / "candidate"
        harness = root / "harness"
        executable_workspace = root / "executable"
        candidate_copy.mkdir(mode=0o700)
        harness.mkdir(mode=0o700)
        executable_workspace.mkdir(mode=0o700)

        canonical_module = f"module {policy.module_path}\n\ngo {policy.go_version}\n"
        candidate_copy.joinpath("go.mod").write_text(canonical_module, encoding="utf-8")
        source_digest = hashlib.sha256()
        for relative in guarded["production_files"]:
            source = candidate_root / relative
            destination = candidate_copy / relative
            content = source.read_bytes()
            destination.write_bytes(content)
            destination.chmod(0o600)
            source_digest.update(relative.encode("utf-8"))
            source_digest.update(b"\0")
            source_digest.update(content)
            source_digest.update(b"\0")

        harness_module = (
            "module eval.local/externaloracle\n\n"
            f"go {policy.go_version}\n\n"
            f"require {policy.module_path} v0.0.0\n\n"
            f"replace {policy.module_path} => ../candidate\n"
        )
        harness.joinpath("go.mod").write_text(harness_module, encoding="utf-8")
        harness.joinpath("main.go").write_text(harness_source, encoding="utf-8")
        binary = executable_workspace / PROBE_BINARY
        command = [
            go,
            "build",
            "-mod=mod",
            "-trimpath",
            "-buildvcs=false",
        ]
        if race:
            command.append("-race")
        command.extend(["-o", str(binary), "."])
        completed = _run_parent(
            command,
            cwd=harness,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )
        if completed.returncode != 0 or not binary.is_file():
            detail = (completed.stdout + completed.stderr)[-2400:]
            raise GoOracleError(f"external Go harness failed to compile: {detail}")
        binary.chmod(0o500)

        go_version = _run_parent(
            [go, "version"],
            cwd=root,
            environment=environment,
            timeout_seconds=5,
        )
        if go_version.returncode != 0 or not go_version.stdout.strip():
            raise GoOracleError("could not capture the selected Go toolchain version")

        shutil.rmtree(harness)
        shutil.rmtree(candidate_copy)
        if harness.exists() or candidate_copy.exists():
            raise GoOracleError("Go source trees remained after external harness build")
        if set(path.name for path in executable_workspace.iterdir()) != {PROBE_BINARY}:
            raise GoOracleError("binary workspace contains unexpected build artifacts")

        yield BuiltGoOracle(
            executable_workspace=executable_workspace,
            go_version=go_version.stdout.strip(),
            candidate_source_sha256=source_digest.hexdigest(),
            excluded_test_files=tuple(guarded["test_files"]),
        )


def run_go_mode(built: BuiltGoOracle, mode: str, timeout_seconds: int) -> GoModeResult:
    """Run one mode in a fresh clone and require its exact completion envelope."""

    if not mode or "\0" in mode:
        raise ValueError("Go oracle mode must be a non-empty string")
    token = secrets.token_hex(32)
    completed = run(
        [f"./{PROBE_BINARY}", "--mode", mode, "--token", token],
        built.executable_workspace,
        timeout_seconds,
    )
    if not completed.passed:
        raise GoOracleError(f"Go oracle mode {mode!r} failed: {completed.summary()}")
    if completed.stderr:
        raise GoOracleError(f"Go oracle mode {mode!r} emitted unexpected stderr")
    payload = _strict_json(completed.stdout, f"Go oracle mode {mode!r}")
    if not isinstance(payload, dict) or set(payload) != {
        "complete",
        "mode",
        "observations",
        "protocol",
        "token",
    }:
        raise GoOracleError(f"Go oracle mode {mode!r} returned an invalid envelope")
    if payload["protocol"] != PROTOCOL or payload["mode"] != mode:
        raise GoOracleError(f"Go oracle mode {mode!r} returned mismatched metadata")
    returned_token = payload["token"]
    if not isinstance(returned_token, str) or not hmac.compare_digest(
        returned_token, token
    ):
        raise GoOracleError(f"Go oracle mode {mode!r} omitted its completion token")
    if payload["complete"] is not True or not isinstance(payload["observations"], dict):
        raise GoOracleError(f"Go oracle mode {mode!r} did not complete")
    return GoModeResult(
        observations=payload["observations"],
        stdout_sha256=hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
    )


def _required_go() -> str:
    raw = shutil.which("go")
    if raw is None:
        raise GoOracleError("the declared Go toolchain is unavailable")
    resolved = Path(raw).resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise GoOracleError(f"the selected Go tool is not executable: {resolved}")
    return str(resolved)


def _go_environment(root: Path, go: str, *, race: bool) -> dict[str, str]:
    home = root / "home"
    temporary = root / "tmp"
    cache = root / "go-cache"
    module_cache = root / "go-mod-cache"
    for directory in (home, temporary, cache, module_cache):
        directory.mkdir(mode=0o700)
    declared_tool_bin = os.environ.get("EVAL_TOOL_BIN")
    if declared_tool_bin:
        tool_bin = Path(declared_tool_bin).resolve(strict=True)
        if not tool_bin.is_dir() or Path(go).parent != tool_bin:
            raise GoOracleError("declared Go executable is outside EVAL_TOOL_BIN")
        path = str(tool_bin)
    else:
        path = ":".join(
            dict.fromkeys(
                (
                    str(Path(go).parent),
                    "/usr/local/sbin",
                    "/usr/local/bin",
                    "/usr/sbin",
                    "/usr/bin",
                    "/sbin",
                    "/bin",
                )
            )
        )
    raw_go_root = os.environ.get("EVAL_GO_ROOT")
    if raw_go_root is None:
        resolved_root = subprocess.run(
            [go, "env", "GOROOT"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            shell=False,
            env={
                "GOENV": "off",
                "GOTOOLCHAIN": "local",
                "HOME": str(home),
                "PATH": path,
            },
        )
        if resolved_root.returncode != 0:
            raise GoOracleError(
                "cannot resolve GOROOT: "
                + (resolved_root.stderr or resolved_root.stdout)[-600:]
            )
        raw_go_root = resolved_root.stdout.strip()
    go_root = Path(raw_go_root)
    if not raw_go_root or not go_root.is_absolute() or not go_root.resolve().is_dir():
        raise GoOracleError(f"invalid GOROOT: {raw_go_root!r}")
    go_root = go_root.resolve()
    if go_root.is_relative_to(Path.home().resolve()):
        raise GoOracleError("GOROOT may not be under the user home")
    environment = {
        "PATH": path,
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "GOCACHE": str(cache),
        "GOMODCACHE": str(module_cache),
        "GOROOT": str(go_root),
        "GOENV": "off",
        "GOWORK": "off",
        "GOTOOLCHAIN": "local",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "CGO_ENABLED": "1" if race else "0",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
    gcc_prefix = os.environ.get("EVAL_GCC_EXEC_PREFIX")
    if race and declared_tool_bin:
        if not gcc_prefix:
            raise GoOracleError("race build requires EVAL_GCC_EXEC_PREFIX")
        prefix = Path(gcc_prefix)
        if not prefix.is_absolute() or not prefix.resolve().is_dir():
            raise GoOracleError(f"invalid GCC runtime prefix: {gcc_prefix!r}")
        prefix = prefix.resolve()
        if prefix.is_relative_to(Path.home().resolve()):
            raise GoOracleError("GCC runtime prefix may not be under the user home")
        environment["COMPILER_PATH"] = str(tool_bin)
        environment["GCC_EXEC_PREFIX"] = str(prefix) + os.sep
    return environment


def _validate_module(
    go: str,
    workspace: Path,
    policy: GoModulePolicy,
    environment: dict[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    go_mod = workspace / "go.mod"
    if not go_mod.is_file() or go_mod.is_symlink():
        raise GoOracleError("candidate module requires a regular go.mod")
    completed = _run_parent(
        [go, "mod", "edit", "-json"],
        cwd=workspace,
        environment=environment,
        timeout_seconds=min(timeout_seconds, 10),
    )
    if completed.returncode != 0:
        raise GoOracleError(
            "candidate go.mod is invalid: "
            + (completed.stderr or completed.stdout)[-1200:]
        )
    module = _strict_json(completed.stdout, "go mod edit output")
    if not isinstance(module, dict):
        raise GoOracleError("go mod edit output must be an object")
    module_record = module.get("Module")
    if (
        not isinstance(module_record, dict)
        or module_record.get("Path") != policy.module_path
    ):
        raise GoOracleError(f"candidate module path must remain {policy.module_path!r}")
    if module.get("Go") != policy.go_version:
        raise GoOracleError(f"candidate go directive must remain {policy.go_version!r}")
    for field in ("Require", "Exclude", "Replace", "Retract"):
        if module.get(field) not in (None, []):
            raise GoOracleError(f"candidate go.mod may not declare {field.lower()}")
    if module.get("Toolchain") not in (None, {}):
        raise GoOracleError("candidate go.mod may not declare a toolchain")
    return module


def _inspect_candidate(
    go: str,
    workspace: Path,
    policy: GoModulePolicy,
    environment: dict[str, str],
    timeout_seconds: int,
) -> dict[str, list[str]]:
    guard = Path(__file__).with_name("go_oracle_guard.go")
    request = json.dumps(
        {
            "root": str(workspace),
            "package": policy.package_name,
            "required_source": policy.required_source,
            "api_contract": policy.api_contract,
        },
        sort_keys=True,
    )
    try:
        completed = subprocess.run(
            [go, "run", str(guard)],
            cwd=guard.parent,
            env=environment,
            input=request,
            capture_output=True,
            text=True,
            timeout=SOURCE_GUARD_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GoOracleError(f"candidate source guard failed: {error}") from error
    if completed.returncode != 0:
        raise GoOracleError(
            "candidate source guard failed: "
            + (completed.stderr or completed.stdout)[-1600:]
        )
    payload = _strict_json(completed.stdout, "candidate source guard output")
    if not isinstance(payload, dict) or set(payload) != {
        "production_files",
        "test_files",
        "violations",
    }:
        raise GoOracleError("candidate source guard returned an invalid response")
    if any(not isinstance(payload[field], list) for field in payload):
        raise GoOracleError("candidate source guard returned invalid field types")
    if payload["violations"]:
        detail = "; ".join(str(item) for item in payload["violations"])
        raise GoOracleError(
            f"candidate module violates the Go oracle boundary: {detail}"
        )
    if any(
        not isinstance(item, str)
        for field in ("production_files", "test_files")
        for item in payload[field]
    ):
        raise GoOracleError("candidate source guard returned invalid file paths")
    return payload


def _run_parent(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GoOracleError(f"Go toolchain command failed: {error}") from error


def _strict_json(value: str, location: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise GoOracleError(f"{location} contains duplicate key {key!r}")
            result[key] = item
        return result

    try:
        return json.loads(value, object_pairs_hook=unique_object)
    except json.JSONDecodeError as error:
        raise GoOracleError(f"{location} is invalid JSON: {error}") from error
