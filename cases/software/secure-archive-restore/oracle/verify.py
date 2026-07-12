#!/usr/bin/env python3
"""Hidden archive-safety, resource-bound, and atomicity oracle."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

sys.path.insert(0, os.environ["EVAL_SHARED_ROOT"])
from untrusted_child import run_untrusted  # noqa: E402


MAX_ENTRIES = 128
MAX_FILE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 4_194_304
ENTRY_PROBE_MEMBERS = 8_192
MAX_ENTRY_PROBE_READ_CHARS = 131_072
MAX_ENTRY_PROBE_READ_DELTA = 16_384
MAX_OVERSIZE_PROBE_READ_CHARS = 65_536
MAX_TOTAL_PROBE_READ_CHARS = 4_259_840
CHILD_TIMEOUT_SECONDS = 15
VERIFIER_TIMEOUT_OVERHEAD_SECONDS = 20

PATH_ATTACKS: list[tuple[str, list[tuple[str, bytes, str]]]] = [
    ("parent", [("../escaped.txt", b"escape", "file")]),
    ("absolute", [("/tmp/absolute.txt", b"escape", "file")]),
]
UNSUPPORTED_ATTACKS: list[tuple[str, list[tuple[str, bytes, str]]]] = [
    ("symlink", [("project-link", b"../outside", "symlink")]),
    (
        "hardlink",
        [("base.txt", b"base", "file"), ("hard.txt", b"base.txt", "hardlink")],
    ),
    ("fifo", [("named-pipe", b"", "fifo")]),
    ("device", [("null-device", b"", "character-device")]),
]
DUPLICATE_ATTACKS: list[tuple[str, list[tuple[str, bytes, str]]]] = [
    ("duplicate", [("same.txt", b"first", "file"), ("same.txt", b"second", "file")]),
    (
        "canonical-alias",
        [("alias/path.txt", b"first", "file"), ("alias//path.txt", b"second", "file")],
    ),
    (
        "file-directory-conflict",
        [("node", b"file", "file"), ("node/child.txt", b"child", "file")],
    ),
    (
        "directory-file-conflict",
        [("node/child.txt", b"child", "file"), ("node", b"file", "file")],
    ),
]
RESOURCE_ATTACKS: list[tuple[str, list[tuple[str, bytes, str]]]] = [
    ("single-file-size", [("large.bin", b"x" * (MAX_FILE_BYTES + 1), "file")]),
    (
        "total-size-boundary",
        [
            *(
                (f"boundary-{index}.bin", b"x" * MAX_FILE_BYTES, "file")
                for index in range(4)
            ),
            ("one-byte-over.bin", b"x", "file"),
        ],
    ),
    (
        "total-size-read-timing",
        [
            *(
                (f"chunk-{index}.bin", b"x" * MAX_FILE_BYTES, "file")
                for index in range(4)
            ),
            ("fifth-full-chunk.bin", b"x" * MAX_FILE_BYTES, "file"),
        ],
    ),
]


def outcome(identifier: str, passed: bool, evidence: str) -> dict[str, object]:
    return {"id": identifier, "passed": passed, "evidence": evidence}


def create_tar(path: Path, entries: list[tuple[str, bytes, str]]) -> None:
    with tarfile.open(path, "w") as archive:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = content.decode("utf-8")
                archive.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = content.decode("utf-8")
                archive.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                archive.addfile(info)
            elif kind == "character-device":
                info.type = tarfile.CHRTYPE
                info.devmajor = 1
                info.devminor = 3
                archive.addfile(info)
            else:
                raise ValueError(f"unsupported oracle entry kind: {kind}")


def stdlib_only(workspace: Path) -> tuple[bool, str]:
    production_files = sorted(
        path
        for path in workspace.rglob("*.py")
        if "tests" not in path.parts and "__pycache__" not in path.parts
    )
    local_modules = {path.stem for path in workspace.glob("*.py")}
    local_modules.update(
        path.name
        for path in workspace.iterdir()
        if path.is_dir() and path.joinpath("__init__.py").is_file()
    )
    external: set[str] = set()
    for source in production_files:
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as error:
            external.add(f"unreadable:{source.name}:{type(error).__name__}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.partition(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = {node.module.partition(".")[0]}
            else:
                continue
            external.update(names - sys.stdlib_module_names - local_modules)

    dependency_names = {
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "uv.lock",
    }
    manifests = sorted(
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file()
        and (path.name in dependency_names or path.name.startswith("requirements"))
    )
    passed = not external and not manifests
    return (
        passed,
        f"external imports={sorted(external)}, dependency manifests={manifests}",
    )


workspace = Path(os.environ["EVAL_WORKSPACE"]).resolve()
assertions: list[dict[str, object]] = []
worker_source = r"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import resource
import signal
import stat
import sys

def path_present(path):
    return path.exists() or path.is_symlink()

def tree_fingerprint(root):
    fingerprint = [[".", "symlink" if root.is_symlink() else "directory" if root.is_dir() else "file", stat.S_IMODE(root.lstat().st_mode), os.readlink(root) if root.is_symlink() else hashlib.sha256(root.read_bytes()).hexdigest() if root.is_file() else ""]]
    if not root.is_dir() or root.is_symlink():
        return fingerprint
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            fingerprint.append([relative, "symlink", mode, os.readlink(path)])
        elif path.is_dir():
            fingerprint.append([relative, "directory", mode, ""])
        elif path.is_file():
            fingerprint.append([relative, "file", mode, hashlib.sha256(path.read_bytes()).hexdigest()])
        else:
            fingerprint.append([relative, "special", mode, ""])
    return fingerprint

def outside_fingerprint(destination):
    excluded = Path(os.path.abspath(destination))
    fingerprint = []
    for root in map(Path, ("/tmp", "/home", "/run", "/var/tmp", "/dev/shm")):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            absolute = Path(os.path.abspath(path))
            if absolute == excluded or excluded in absolute.parents:
                continue
            relative = f"{root}:{path.relative_to(root).as_posix()}"
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                fingerprint.append([relative, "symlink", mode, os.readlink(path)])
            elif path.is_dir():
                fingerprint.append([relative, "directory", mode, ""])
            elif path.is_file():
                fingerprint.append([relative, "file", mode, hashlib.sha256(path.read_bytes()).hexdigest()])
            else:
                fingerprint.append([relative, "special", mode, ""])
    return fingerprint

def read_chars():
    for line in Path("/proc/self/io").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if key == "rchar" and separator:
            return int(value.strip())
    raise RuntimeError("/proc/self/io did not report rchar")

captured = io.StringIO()
request = json.load(sys.stdin)
destination = Path(request["destination"])
existing_destination = request.get("existing_destination")
if existing_destination == "directory":
    destination.joinpath("nested", "empty").mkdir(parents=True)
    destination.joinpath("keep.txt").write_bytes(b"original\x00bytes")
    destination.joinpath("nested", "state.json").write_text(
        '{"version": 7}\n', encoding="utf-8"
    )
    destination.joinpath("keep.txt").chmod(0o640)
elif existing_destination == "file":
    destination.write_bytes(b"existing destination file\x00")
    destination.chmod(0o640)
elif existing_destination == "dangling-symlink":
    destination.symlink_to("missing-destination-target")
elif existing_destination is not None:
    raise ValueError(f"unsupported existing destination fixture: {existing_destination}")
outside_before = outside_fingerprint(destination)
before = tree_fingerprint(destination) if path_present(destination) else []
with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
    spec = importlib.util.spec_from_file_location("candidate_restore", Path.cwd() / "restore.py")
    if spec is None or spec.loader is None:
        raise ImportError("could not load restore.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
race_state = {"created": False, "before": None}
if request.get("race_publication") is True:
    workspace_root = Path.cwd().absolute()
    sandbox_tmp_root = workspace_root.parent
    excluded_write_targets = {
        workspace_root / "input.tar",
        workspace_root / "restore.py",
    }
    excluded_write_roots = {
        sandbox_tmp_root / "go-cache",
        sandbox_tmp_root / "go-mod-cache",
        sandbox_tmp_root / "npm-cache",
        sandbox_tmp_root / "python-pycache",
        sandbox_tmp_root / "tool-bin",
    }
    def create_competing_destination(event, arguments):
        if event != "open" or len(arguments) < 3 or race_state["created"]:
            return
        raw_target, raw_mode, raw_flags = arguments[:3]
        if not isinstance(raw_target, (str, bytes, os.PathLike)):
            return
        mode = raw_mode if isinstance(raw_mode, str) else ""
        flags = raw_flags if isinstance(raw_flags, int) else 0
        writable = any(character in mode for character in "wax+") or bool(
            flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        )
        if not writable:
            return
        target = Path(os.path.abspath(os.fsdecode(os.fspath(raw_target))))
        eligible = (
            target not in excluded_write_targets
            and not any(
                target == excluded or excluded in target.parents
                for excluded in excluded_write_roots
            )
            and (target == sandbox_tmp_root or sandbox_tmp_root in target.parents)
        )
        if eligible and not path_present(destination):
            destination.mkdir()
            race_state["created"] = True
            race_state["before"] = tree_fingerprint(destination)
    sys.addaudithook(create_competing_destination)
warmup_response = None
warmup_read_chars = None
warmup_destination_exists = None
if request.get("warmup_archive") is not None:
    warmup_destination = Path(request["warmup_destination"])
    warmup_read_before = read_chars()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            warmup_value = module.restore_project(
                archive_path=Path(request["warmup_archive"]),
                destination=warmup_destination,
            )
        warmup_response = {"ok": True, "value": warmup_value}
    except Exception as error:
        warmup_response = {"ok": False, "error_type": type(error).__name__}
    warmup_read_chars = max(0, read_chars() - warmup_read_before)
    warmup_destination_exists = path_present(warmup_destination)
write_limit_state = {"applied": False, "hit": False}
original_file_limit = None
original_xfsz_handler = None
if request.get("fail_write") is True:
    original_file_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
    hard_limit = original_file_limit[1]
    write_limit = 65_536
    if hard_limit != resource.RLIM_INFINITY and hard_limit < write_limit:
        raise RuntimeError("candidate file-size hard limit is below the write-failure probe")
    original_xfsz_handler = signal.getsignal(signal.SIGXFSZ)
    def record_file_limit(_signum, _frame):
        write_limit_state["hit"] = True
    signal.signal(signal.SIGXFSZ, record_file_limit)
    resource.setrlimit(resource.RLIMIT_FSIZE, (write_limit, hard_limit))
    write_limit_state["applied"] = True
read_before = read_chars()
try:
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        value = module.restore_project(
            archive_path=Path(request["archive"]), destination=destination
        )
    candidate_response = {"ok": True, "value": value}
except Exception as error:
    candidate_response = {"ok": False, "error_type": type(error).__name__}
finally:
    if write_limit_state["applied"]:
        resource.setrlimit(resource.RLIMIT_FSIZE, original_file_limit)
        signal.signal(signal.SIGXFSZ, original_xfsz_handler)
read_delta = max(0, read_chars() - read_before)
after = tree_fingerprint(destination) if path_present(destination) else []
outside_after = outside_fingerprint(destination)
files = {}
if destination.is_dir() and not destination.is_symlink():
    for path in destination.rglob("*"):
        if path.is_file() and not path.is_symlink():
            content = path.read_bytes()
            files[path.relative_to(destination).as_posix()] = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
facts = {
    "after": after,
    "before": before,
    "destination_exists": path_present(destination),
    "escaped": Path("escaped.txt").exists() or Path("mixed-escape.txt").exists(),
    "files": files,
    "outside_unchanged": outside_before == outside_after,
    "race_before": race_state["before"],
    "race_created": race_state["created"],
    "read_chars": read_delta,
    "response": candidate_response,
    "warmup_destination_exists": warmup_destination_exists,
    "warmup_read_chars": warmup_read_chars,
    "warmup_response": warmup_response,
    "write_limit_applied": write_limit_state["applied"],
    "write_limit_hit": write_limit_state["hit"],
}
sys.__stdout__.write(json.dumps(facts, sort_keys=True))
"""


def exercise_restore(
    entries: list[tuple[str, bytes, str]],
    *,
    existing_destination: str | None = None,
    fail_write: bool = False,
    race_publication: bool = False,
    truncate_at: int | None = None,
    warmup_entries: list[tuple[str, bytes, str]] | None = None,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="archive-scenario-") as raw_scenario:
        scenario = Path(raw_scenario) / "workspace"
        shutil.copytree(workspace, scenario)
        archive_path = scenario / "input.tar"
        destination = scenario / "destination"
        create_tar(archive_path, entries)
        warmup_archive = scenario / "warmup.tar"
        if warmup_entries is not None:
            create_tar(warmup_archive, warmup_entries)
        if truncate_at is not None:
            archive_path.write_bytes(archive_path.read_bytes()[:truncate_at])
        completed = run_untrusted(
            [sys.executable, "-c", worker_source],
            scenario,
            CHILD_TIMEOUT_SECONDS,
            input_text=json.dumps(
                {
                    "archive": archive_path.name,
                    "destination": destination.name,
                    "existing_destination": existing_destination,
                    "fail_write": fail_write,
                    "race_publication": race_publication,
                    "warmup_archive": warmup_archive.name
                    if warmup_entries is not None
                    else None,
                    "warmup_destination": "warmup-destination",
                }
            ),
        )
        if not completed.passed:
            facts: dict[str, object] = {
                "response": {
                    "ok": False,
                    "infrastructure_error": completed.sandbox_error
                    or (
                        "candidate timed out"
                        if completed.timed_out
                        else completed.stderr
                    )
                    or f"candidate exited {completed.returncode}",
                },
                "destination_exists": False,
                "before": [],
                "after": [],
                "files": {},
                "escaped": False,
                "outside_unchanged": False,
                "race_before": None,
                "race_created": False,
                "read_chars": None,
                "write_limit_applied": False,
                "write_limit_hit": False,
                "warmup_destination_exists": None,
                "warmup_read_chars": None,
                "warmup_response": None,
            }
        else:
            try:
                facts = json.loads(completed.stdout)
            except json.JSONDecodeError as error:
                facts = {
                    "response": {"ok": False, "infrastructure_error": str(error)},
                    "destination_exists": False,
                    "before": [],
                    "after": [],
                    "files": {},
                    "escaped": False,
                    "outside_unchanged": False,
                    "race_before": None,
                    "race_created": False,
                    "read_chars": None,
                    "write_limit_applied": False,
                    "write_limit_hit": False,
                    "warmup_destination_exists": None,
                    "warmup_read_chars": None,
                    "warmup_response": None,
                }
        expected_keys = {
            "after",
            "before",
            "destination_exists",
            "escaped",
            "files",
            "outside_unchanged",
            "race_before",
            "race_created",
            "read_chars",
            "response",
            "warmup_destination_exists",
            "warmup_read_chars",
            "warmup_response",
            "write_limit_applied",
            "write_limit_hit",
        }
        if not isinstance(facts, dict) or set(facts) != expected_keys:
            return {
                "response": {"ok": False, "infrastructure_error": "invalid facts"},
                "destination_exists": False,
                "before": [],
                "after": [],
                "files": {},
                "escaped": False,
                "outside_unchanged": False,
                "race_before": None,
                "race_created": False,
                "read_chars": None,
                "write_limit_applied": False,
                "write_limit_hit": False,
                "warmup_destination_exists": None,
                "warmup_read_chars": None,
                "warmup_response": None,
            }
        return facts


def file_fact(content: bytes) -> dict[str, object]:
    return {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}


def ordinary_exception(response: object) -> bool:
    return (
        isinstance(response, dict)
        and set(response) == {"error_type", "ok"}
        and response.get("ok") is False
        and isinstance(response.get("error_type"), str)
        and bool(response["error_type"])
    )


def safe_rejection(facts: dict[str, object]) -> bool:
    return (
        ordinary_exception(facts["response"])
        and facts["destination_exists"] is False
        and facts["escaped"] is False
        and facts["outside_unchanged"] is True
    )


def tree_has(facts: dict[str, object], relative: str, kind: str) -> bool:
    return any(
        isinstance(item, list)
        and len(item) == 4
        and item[0] == relative
        and item[1] == kind
        for item in facts["after"]  # type: ignore[union-attr]
    )


stdlib_ok, stdlib_detail = stdlib_only(workspace)
valid_entries = [
    ("config", b"", "dir"),
    ("config/empty", b"", "dir"),
    ("notes.txt", b"release notes\n", "file"),
    ("config/settings.json", b'{"safe": true}\n', "file"),
]
valid = exercise_restore(valid_entries)
valid_response = valid["response"]
valid_ok = (
    valid_response == {"ok": True, "value": ["config/settings.json", "notes.txt"]}
    and valid["files"]
    == {
        "config/settings.json": file_fact(b'{"safe": true}\n'),
        "notes.txt": file_fact(b"release notes\n"),
    }
    and tree_has(valid, "config/empty", "directory")
    and valid["outside_unchanged"] is True
)

boundary_entries = [
    *(
        (f"payload/part-{index}.bin", bytes([index]) * MAX_FILE_BYTES, "file")
        for index in range(4)
    ),
    *((f"empty/dir-{index:03d}", b"", "dir") for index in range(MAX_ENTRIES - 4)),
]
boundary = exercise_restore(boundary_entries)
boundary_response = boundary["response"]
boundary_paths = [f"payload/part-{index}.bin" for index in range(4)]
boundary_ok = (
    boundary_response == {"ok": True, "value": boundary_paths}
    and boundary["files"]
    == {
        path: file_fact(bytes([index]) * MAX_FILE_BYTES)
        for index, path in enumerate(boundary_paths)
    }
    and tree_has(boundary, f"empty/dir-{MAX_ENTRIES - 5:03d}", "directory")
    and boundary["outside_unchanged"] is True
)
assertions.append(
    outcome(
        "valid-archive-behavior",
        valid_ok and boundary_ok,
        "nested files, empty directories, sorted paths, and all inclusive limits matched"
        if valid_ok and boundary_ok
        else f"valid={valid_response!r}, boundary={boundary_response!r}",
    )
)
assertions.append(outcome("standard-library-only", stdlib_ok, stdlib_detail))


def exercise_attack_group(
    attacks: list[tuple[str, list[tuple[str, bytes, str]]]],
    *,
    warmup_entries: list[tuple[str, bytes, str]] | None = None,
) -> tuple[bool, str, dict[str, dict[str, object]]]:
    results: list[str] = []
    facts_by_label: dict[str, dict[str, object]] = {}
    passed = True
    for label, entries in attacks:
        facts = exercise_restore(entries, warmup_entries=warmup_entries)
        facts_by_label[label] = facts
        warmup_safe = warmup_entries is None or (
            ordinary_exception(facts["warmup_response"])
            and facts["warmup_destination_exists"] is False
        )
        safe = safe_rejection(facts) and warmup_safe
        passed = passed and safe
        results.append(
            f"{label}: safe={safe}, response={facts['response']!r}, "
            f"destination_absent={not facts['destination_exists']}, "
            f"outside_unchanged={facts['outside_unchanged']}"
        )
    return passed, "; ".join(results), facts_by_label


path_ok, path_detail, _path_facts = exercise_attack_group(PATH_ATTACKS)
assertions.append(outcome("path-containment", path_ok, path_detail))
unsupported_ok, unsupported_detail, _unsupported_facts = exercise_attack_group(
    UNSUPPORTED_ATTACKS
)
assertions.append(
    outcome("unsupported-member-types", unsupported_ok, unsupported_detail)
)
duplicate_ok, duplicate_detail, _duplicate_facts = exercise_attack_group(
    DUPLICATE_ATTACKS
)
assertions.append(outcome("duplicate-member-defense", duplicate_ok, duplicate_detail))

limit_warmup_entries = [
    (f"empty-{index:05d}.txt", b"", "file") for index in range(MAX_ENTRIES + 1)
]
entry_probe = exercise_restore(
    [(f"empty-{index:05d}.txt", b"", "file") for index in range(ENTRY_PROBE_MEMBERS)],
    warmup_entries=limit_warmup_entries,
)
resource_ok, resource_detail, resource_facts = exercise_attack_group(
    RESOURCE_ATTACKS, warmup_entries=limit_warmup_entries
)
warmup_safe = (
    ordinary_exception(entry_probe["warmup_response"])
    and entry_probe["warmup_destination_exists"] is False
)
entry_limit_ok = safe_rejection(entry_probe) and warmup_safe
single_file_read_chars = resource_facts["single-file-size"]["read_chars"]
total_size_read_chars = resource_facts["total-size-read-timing"]["read_chars"]
resource_reads_ok = (
    type(single_file_read_chars) is int
    and 0 <= single_file_read_chars <= MAX_OVERSIZE_PROBE_READ_CHARS
    and type(total_size_read_chars) is int
    and 0 <= total_size_read_chars <= MAX_TOTAL_PROBE_READ_CHARS
)
assertions.append(
    outcome(
        "declared-resource-limits",
        entry_limit_ok and resource_ok and resource_reads_ok,
        f"entry-count: safe={entry_limit_ok}; {resource_detail}; "
        f"single_file_read_chars={single_file_read_chars!r}/{MAX_OVERSIZE_PROBE_READ_CHARS}; "
        f"total_size_read_chars={total_size_read_chars!r}/{MAX_TOTAL_PROBE_READ_CHARS}",
    )
)
read_chars = entry_probe["read_chars"]
warmup_read_chars = entry_probe["warmup_read_chars"]
raw_read_delta = (
    read_chars - warmup_read_chars
    if type(read_chars) is int and type(warmup_read_chars) is int
    else None
)
read_excess = max(0, raw_read_delta) if raw_read_delta is not None else None
bounded_ok = (
    entry_limit_ok
    and type(read_chars) is int
    and 0 <= read_chars <= MAX_ENTRY_PROBE_READ_CHARS
    and type(warmup_read_chars) is int
    and read_excess is not None
    and read_excess <= MAX_ENTRY_PROBE_READ_DELTA
)
assertions.append(
    outcome(
        "bounded-entry-consumption",
        bounded_ok,
        f"members={ENTRY_PROBE_MEMBERS}, warmup_read_chars={warmup_read_chars!r}, "
        f"read_chars={read_chars!r}, raw_delta={raw_read_delta!r}, excess={read_excess!r}, "
        f"maximum={MAX_ENTRY_PROBE_READ_CHARS}, delta_maximum={MAX_ENTRY_PROBE_READ_DELTA}",
    )
)

mixed = exercise_restore(
    [
        ("safe-before-error.txt", b"must not remain", "file"),
        ("../mixed-escape.txt", b"must not escape", "file"),
    ]
)
truncated = exercise_restore(
    [("first.txt", b"a" * 1024, "file"), ("second.txt", b"b" * 1024, "file")],
    truncate_at=2_176,
)
write_failure = exercise_restore(
    [("mid-write.bin", b"w" * 262_144, "file")], fail_write=True
)
write_failure_ok = (
    safe_rejection(write_failure)
    and write_failure["write_limit_applied"] is True
    and write_failure["write_limit_hit"] is True
)
invalid_atomic_ok = (
    safe_rejection(mixed) and safe_rejection(truncated) and write_failure_ok
)
assertions.append(
    outcome(
        "invalid-archive-atomicity",
        invalid_atomic_ok,
        "late policy rejection, truncated payload, and injected write failure left no destination or residue"
        if invalid_atomic_ok
        else f"mixed={mixed!r}, truncated={truncated!r}, write_failure={write_failure!r}",
    )
)

existing_directory = exercise_restore(valid_entries, existing_destination="directory")
existing_file = exercise_restore(valid_entries, existing_destination="file")
existing_symlink = exercise_restore(
    valid_entries, existing_destination="dangling-symlink"
)
existing_results = {
    "directory": existing_directory,
    "file": existing_file,
    "dangling-symlink": existing_symlink,
}
existing_ok = all(
    ordinary_exception(facts["response"])
    and facts["before"] == facts["after"]
    and facts["escaped"] is False
    and facts["outside_unchanged"] is True
    for facts in existing_results.values()
)
assertions.append(
    outcome(
        "existing-destination-preserved",
        existing_ok,
        "valid input was refused and the exact existing destination tree was preserved"
        if existing_ok
        else "; ".join(
            f"{kind}: response={facts['response']!r}, preserved={facts['before'] == facts['after']}"
            for kind, facts in existing_results.items()
        ),
    )
)

publication = exercise_restore(valid_entries, race_publication=True)
publication_ok = (
    ordinary_exception(publication["response"])
    and publication["race_created"] is True
    and publication["destination_exists"] is True
    and publication["race_before"] == publication["after"]
    and publication["escaped"] is False
    and publication["outside_unchanged"] is True
)
assertions.append(
    outcome(
        "publication-failure-atomicity",
        publication_ok,
        "a concurrent destination was preserved exactly and publication left no staging residue"
        if publication_ok
        else f"response={publication['response']!r}, race_created={publication['race_created']!r}, competitor_preserved={publication['race_before'] == publication['after']}, outside_unchanged={publication['outside_unchanged']!r}",
    )
)

signature_ok = False
signature_detail = "restore.py could not be inspected"
try:
    syntax_tree = ast.parse((workspace / "restore.py").read_text(encoding="utf-8"))
    definitions = [
        node
        for node in syntax_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "restore_project"
    ]
    if len(definitions) == 1:
        arguments = definitions[0].args
        signature_ok = (
            not arguments.posonlyargs
            and [argument.arg for argument in arguments.args]
            == ["archive_path", "destination"]
            and not arguments.defaults
            and arguments.vararg is None
            and not arguments.kwonlyargs
            and arguments.kwarg is None
        )
        signature_detail = (
            "restore_project keeps keyword-compatible archive_path and destination parameters"
            if signature_ok
            else "restore_project must have signature (archive_path, destination)"
        )
except (OSError, SyntaxError) as error:
    signature_detail = f"signature inspection failed: {type(error).__name__}: {error}"
assertions.append(outcome("public-call-contract", signature_ok, signature_detail))

print(
    json.dumps(
        {"passed": all(item["passed"] for item in assertions), "assertions": assertions}
    )
)
