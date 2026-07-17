#!/usr/bin/env python3
"""Run every named good, bad, and adversarial testing-case calibration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


SUITE_MANIFEST = Path(__file__).resolve().parents[2] / "suite.json"
SUITE_ROOT = SUITE_MANIFEST.parent
sys.path.insert(0, str(SUITE_ROOT / "cases"))

from _calibration_tools import (  # noqa: E402
    private_tool_environment,
    sandbox_tool_paths,
)

TEST_TARGETS = {
    "testing-oracle-sensitivity": "test_discounts.py",
    "testing-parser-boundaries": "frame_test.go",
    "testing-state-machine-sequences": "subscription.test.js",
    "testing-real-boundary-fidelity": "test_registry.py",
    "testing-concurrency-flake": "squares_test.go",
    "testing-legacy-characterization": "test_legacy_billing.py",
    "testing-event-idempotency": "ledger.test.js",
}


def verifier_environment(
    workspace: Path, case_root: Path, tool_environment: dict[str, str]
) -> dict[str, str]:
    environment = dict(tool_environment)
    resolved = sandbox_tool_paths()
    environment.update(
        {
            "EVAL_WORKSPACE": str(workspace),
            "EVAL_SUITE_ROOT": str(SUITE_ROOT),
            "EVAL_CASE_ROOT": str(case_root),
            "EVAL_SHARED_ROOT": str(SUITE_ROOT / "cases" / "testing" / "_shared"),
            "EVAL_HOST_UID": str(os.getuid()),
            "EVAL_UNSHARE": str(resolved["unshare"]),
            "EVAL_MOUNT": str(resolved["mount"]),
            "EVAL_SETPRIV": str(resolved["setpriv"]),
            "EVAL_ENV": str(resolved["env"]),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
        }
    )
    return environment


def run(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def parse_verdict(
    result: subprocess.CompletedProcess[str], case_id: str
) -> dict[str, object]:
    if result.returncode != 0:
        raise AssertionError(
            f"{case_id}: verifier exited {result.returncode}: {result.stderr.strip()}"
        )
    try:
        verdict = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"{case_id}: invalid verifier JSON: {result.stdout!r}"
        ) from error
    allowed = {"passed", "assertions", "metrics"}
    if set(verdict) - allowed or not {"passed", "assertions"}.issubset(verdict):
        raise AssertionError(
            f"{case_id}: verifier keys violate contract: {sorted(verdict)}"
        )
    if not isinstance(verdict["passed"], bool) or not isinstance(
        verdict["assertions"], list
    ):
        raise AssertionError(f"{case_id}: verifier types violate the contract")
    if "metrics" in verdict and not isinstance(verdict["metrics"], dict):
        raise AssertionError(f"{case_id}: verifier metrics must be an object")

    assertion_ids: list[str] = []
    for index, assertion in enumerate(verdict["assertions"]):
        if not isinstance(assertion, dict) or set(assertion) != {
            "id",
            "passed",
            "evidence",
        }:
            raise AssertionError(
                f"{case_id}: assertion {index} violates the object contract"
            )
        if (
            not isinstance(assertion["id"], str)
            or not assertion["id"]
            or not isinstance(assertion["passed"], bool)
            or not isinstance(assertion["evidence"], str)
            or not assertion["evidence"]
        ):
            raise AssertionError(
                f"{case_id}: assertion {index} has invalid field types"
            )
        assertion_ids.append(assertion["id"])
    if len(assertion_ids) != len(set(assertion_ids)):
        raise AssertionError(f"{case_id}: verifier emitted duplicate assertion IDs")
    aggregate = all(assertion["passed"] for assertion in verdict["assertions"])
    if verdict["passed"] is not aggregate:
        raise AssertionError(
            f"{case_id}: top-level passed disagrees with its assertions"
        )
    return verdict


def calibration_files(case: dict[str, object], category: str) -> list[tuple[str, Path]]:
    case_id = str(case["id"])
    prompt = SUITE_ROOT / str(case["prompt_file"])
    target = TEST_TARGETS[case_id]
    category_root = prompt.parent / "calibration" / category
    calibrations = sorted(category_root.rglob(target))
    result: list[tuple[str, Path]] = []
    for calibration in calibrations:
        relative_parent = calibration.parent.relative_to(category_root)
        name = "default" if relative_parent == Path(".") else relative_parent.as_posix()
        result.append((name, calibration))
    return result


def calibrate(
    case: dict[str, object],
    category: str,
    name: str,
    calibration: Path,
    tool_environment: dict[str, str],
) -> dict[str, object]:
    case_id = str(case["id"])
    fixture = SUITE_ROOT / str(case["fixture_dir"])
    case_root = (SUITE_ROOT / str(case["prompt_file"])).parent
    verifier = [str(part) for part in case["verifier"]["argv"]]  # type: ignore[index]
    test_target = TEST_TARGETS[case_id]

    with tempfile.TemporaryDirectory(
        prefix=f"{case_id}-{category}-{name.replace('/', '-')}-"
    ) as temporary:
        workspace = Path(temporary) / "workspace"
        shutil.copytree(fixture, workspace)
        if name == "default":
            shutil.copyfile(calibration, workspace / test_target)
        else:
            for source in sorted(calibration.parent.rglob("*")):
                if not source.is_file() or "__pycache__" in source.parts:
                    continue
                destination = workspace / source.relative_to(calibration.parent)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
        environment = verifier_environment(workspace, case_root, tool_environment)
        timeout = int(case["verifier"]["timeout_seconds"])  # type: ignore[index]
        verdict = parse_verdict(
            run(verifier, cwd=SUITE_ROOT, env=environment, timeout=timeout), case_id
        )

    expected_list = [
        str(identifier)
        for identifier in case["critical_expectations"]  # type: ignore[union-attr]
    ]
    if len(expected_list) != len(set(expected_list)):
        raise AssertionError(
            f"{case_id}: manifest contains duplicate critical expectation IDs"
        )
    expected_ids = set(expected_list)
    actual_ids = {
        assertion.get("id")
        for assertion in verdict["assertions"]  # type: ignore[union-attr]
        if isinstance(assertion, dict)
    }
    if actual_ids != expected_ids:
        raise AssertionError(
            f"{case_id}: assertion IDs {sorted(actual_ids)} != {sorted(expected_ids)}"
        )
    return verdict


def main() -> int:
    manifest = json.loads(SUITE_MANIFEST.read_text(encoding="utf-8"))
    cases = [case for case in manifest["cases"] if case["skill"] == "testing"]
    failures: list[str] = []
    totals = {"good": 0, "bad": 0, "adversarial": 0}
    with private_tool_environment() as tool_environment:
        for case in cases:
            case_id = case["id"]
            try:
                case_counts = {"good": 0, "bad": 0, "adversarial": 0}
                for category, expected in {
                    "good": True,
                    "bad": False,
                    "adversarial": False,
                }.items():
                    calibrations = calibration_files(case, category)
                    if not calibrations:
                        raise AssertionError(f"no {category} calibrations found")
                    for name, calibration in calibrations:
                        verdict = calibrate(
                            case,
                            category,
                            name,
                            calibration,
                            tool_environment,
                        )
                        if verdict["passed"] is not expected:
                            disposition = (
                                "accepted" if verdict["passed"] else "rejected"
                            )
                            raise AssertionError(
                                f"{category}/{name} was unexpectedly {disposition}"
                            )
                        case_counts[category] += 1
                        totals[category] += 1
                print(
                    f"PASS {case_id}: {case_counts['good']} good accepted, "
                    f"{case_counts['bad']} bad and "
                    f"{case_counts['adversarial']} adversarial rejected"
                )
            except (AssertionError, OSError, subprocess.TimeoutExpired) as error:
                failures.append(f"{case_id}: {error}")
                print(f"FAIL {case_id}: {error}", file=sys.stderr)
    if failures:
        print(f"\n{len(failures)} calibration failure(s)", file=sys.stderr)
        return 1
    print(
        f"\nCalibrated {len(cases)} testing cases: "
        f"{totals['good']} good, {totals['bad']} bad, "
        f"{totals['adversarial']} adversarial"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
