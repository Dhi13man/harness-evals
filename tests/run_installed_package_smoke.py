#!/usr/bin/env python3
"""Prove built distributions work without checkout-relative package resources."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


PROFILE_PATH = "harness_evals/comparator_calibration/profile.json"
AUTHORITY_PATH = "harness_evals/comparator-profile-authority.json"
PROFILE_ROOT = "harness_evals/comparator_calibration/"
ALLOWED_PROFILE_SUPPORT_FILES = {
    f"{PROFILE_ROOT}README.md",
}


def _run(*argv: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(argv)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _declared_package_resources(profile_bytes: bytes) -> set[str]:
    descriptor = json.loads(profile_bytes)
    resources = descriptor.get("resources")
    if not isinstance(resources, dict) or not resources:
        raise RuntimeError("packaged profile descriptor has no resource map")
    declared = {PROFILE_PATH, AUTHORITY_PATH}
    base = PurePosixPath(PROFILE_PATH).parent
    for name, raw_path in resources.items():
        path = PurePosixPath(raw_path) if isinstance(raw_path, str) else None
        if (
            path is None
            or path.is_absolute()
            or ".." in path.parts
            or path == PurePosixPath(".")
        ):
            raise RuntimeError(f"packaged profile resource is unsafe: {name}")
        declared.add((base / path).as_posix())
    return declared


def _inspect_distributions(wheel: Path, sdist: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        wheel_files = set(archive.namelist())
        wheel_required = _declared_package_resources(archive.read(PROFILE_PATH))
        wheel_resource_bytes = {
            path: archive.read(path) for path in wheel_required if path in wheel_files
        }
    missing_wheel = wheel_required - wheel_files
    if missing_wheel:
        raise RuntimeError(f"wheel omitted package resources: {sorted(missing_wheel)}")

    with tarfile.open(sdist, "r:gz") as archive:
        normalized_sdist_files = {
            "/".join(PurePosixPath(member.name).parts[1:]): member
            for member in archive.getmembers()
            if member.isfile() and "/" in member.name
        }
        profile_member = normalized_sdist_files.get(PROFILE_PATH)
        if profile_member is None:
            raise RuntimeError("sdist omitted the profile descriptor")
        reader = archive.extractfile(profile_member)
        if reader is None:
            raise RuntimeError("sdist profile descriptor is not a regular file")
        sdist_required = _declared_package_resources(reader.read())
        sdist_resource_bytes = {}
        for path in sdist_required:
            member = normalized_sdist_files.get(path)
            if member is None:
                continue
            resource_reader = archive.extractfile(member)
            if resource_reader is None:
                raise RuntimeError(f"sdist resource is not a regular file: {path}")
            sdist_resource_bytes[path] = resource_reader.read()
    missing_sdist = sdist_required - set(normalized_sdist_files)
    if missing_sdist:
        raise RuntimeError(f"sdist omitted package resources: {sorted(missing_sdist)}")
    if wheel_required != sdist_required:
        raise RuntimeError("wheel and sdist declare different profile resources")
    if wheel_resource_bytes != sdist_resource_bytes:
        raise RuntimeError("wheel and sdist profile resource bytes differ")

    for label, files, required in (
        ("wheel", wheel_files, wheel_required),
        ("sdist", set(normalized_sdist_files), sdist_required),
    ):
        unexpected = {
            path
            for path in files
            if path.startswith(PROFILE_ROOT)
            and not path.endswith("/")
            and path not in required
            and path not in ALLOWED_PROFILE_SUPPORT_FILES
            and PurePosixPath(path).suffix not in {".py", ".pyi"}
        }
        if unexpected:
            raise RuntimeError(
                f"{label} contains unexpected non-code profile files: "
                f"{sorted(unexpected)}"
            )


def _write_external_suite(root: Path) -> Path:
    from harness_evals.comparator_profiles import (
        BUILTIN_SOFTWARE_PROFILE_ID,
        resolve_builtin_profile,
    )

    _run("git", "init", "-q", cwd=root)
    _run("git", "config", "user.email", "package-smoke@example.invalid", cwd=root)
    _run("git", "config", "user.name", "Package Smoke", cwd=root)
    (root / "README.txt").write_text("external suite\n", encoding="utf-8")
    _run("git", "add", "README.txt", cwd=root)
    _run("git", "commit", "-q", "-m", "test fixture", cwd=root)

    profile = resolve_builtin_profile(BUILTIN_SOFTWARE_PROFILE_ID)
    test_release = json.loads(profile.read_bytes("test_release"))
    frozen_original = test_release["runtime_adapter"]["frozen_original_commit"]
    authority = {
        "schema_version": 1,
        "original_commit": frozen_original,
    }
    (root / "baseline-authority.json").write_text(
        json.dumps(authority, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "prompt.md").write_text("Create answer.txt.\n", encoding="utf-8")
    fixture = root / "fixture"
    fixture.mkdir()
    (fixture / "input.txt").write_text("input\n", encoding="utf-8")
    (root / "verifier.py").write_text(
        "import json\nprint(json.dumps({'passed': True, 'assertions': "
        "[{'id': 'answer-present', 'passed': True, 'evidence': 'smoke'}], "
        "'metrics': {}}))\n",
        encoding="utf-8",
    )
    suite = {
        "schema_version": 3,
        "suite_id": "installed-package-smoke",
        "seed": 7123,
        "evaluation_mode": "judged",
        "repository_root": ".",
        "provider": {
            "kind": "fake",
            "model": "fake-model-v1",
            "timeout_seconds": 10,
        },
        "comparator": {
            "kind": "fake",
            "model": "fake-sonnet-v2",
            "timeout_seconds": 300,
            "max_budget_usd": 1.0,
        },
        "comparator_profile": {
            "kind": "builtin",
            "id": BUILTIN_SOFTWARE_PROFILE_ID,
        },
        "variants": [
            {"id": "without-a", "kind": "without_skill"},
            {"id": "without-b", "kind": "without_skill"},
            {"id": "original", "kind": "git_ref", "git_ref": frozen_original},
        ],
        "comparisons": [
            {
                "id": "package-smoke",
                "control": "without-a",
                "treatment": "without-b",
                "repetitions": 3,
                "comparator_order": "ab_ba",
            }
        ],
        "cases": [
            {
                "id": "basic",
                "skill": "demo",
                "split": "train",
                "prompt_file": "prompt.md",
                "fixture_dir": "fixture",
                "verifier": {
                    "argv": ["python3", "verifier.py"],
                    "timeout_seconds": 5,
                    "required_tools": [],
                },
                "context_files": [],
                "timeout_seconds": 5,
                "critical_expectations": ["answer-present"],
                "comparator_contract": {
                    "requirements": [
                        {
                            "id": "answer-present",
                            "kind": "required_behavior",
                            "text": "The implementation must create a non-empty answer file.",
                        }
                    ],
                    "performance_basis": None,
                    "qualitative_bases": {},
                },
            }
        ],
    }
    manifest = root / "external-suite.json"
    manifest.write_text(json.dumps(suite, indent=2) + "\n", encoding="utf-8")
    return manifest


def _run_external_smoke(cli: Path, forbidden_root: Path) -> None:
    import harness_evals
    from harness_evals.comparator_profiles import BUILTIN_SOFTWARE_PROFILE_ID

    package_path = Path(harness_evals.__file__).resolve()
    if package_path.is_relative_to(forbidden_root.resolve()):
        raise RuntimeError(f"smoke imported checkout package: {package_path}")
    with tempfile.TemporaryDirectory(prefix="harness-evals-installed-") as temporary:
        root = Path(temporary).resolve()
        manifest = _write_external_suite(root)
        completed = _run(
            str(cli),
            "--suite",
            str(manifest),
            "--comparison",
            "package-smoke",
            "--dry-run",
            cwd=root,
        )
        summary = json.loads(completed.stdout)
        comparator = summary["preflight"]["comparator"]
        if comparator["profile_kind"] != "builtin":
            raise RuntimeError("installed CLI did not select a built-in profile")
        if comparator["profile_id"] != BUILTIN_SOFTWARE_PROFILE_ID:
            raise RuntimeError("installed CLI selected the wrong profile id")
        if comparator["profile_locks_valid"] is not True:
            raise RuntimeError("installed CLI did not validate profile locks")
        if comparator["protocol_locks_valid"] is not True:
            raise RuntimeError("installed CLI did not validate protocol locks")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--sdist", type=Path)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--forbid-root", type=Path)
    args = parser.parse_args()
    if args.wheel is not None or args.sdist is not None:
        if args.wheel is None or args.sdist is None:
            parser.error("--wheel and --sdist must be supplied together")
        _inspect_distributions(args.wheel, args.sdist)
    if args.cli is not None or args.forbid_root is not None:
        if args.cli is None or args.forbid_root is None:
            parser.error("--cli and --forbid-root must be supplied together")
        _run_external_smoke(args.cli, args.forbid_root)
    if args.wheel is None and args.cli is None:
        parser.error("select distribution inspection, installed smoke, or both")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
