#!/usr/bin/env python3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from verifier_lib import (
    assertion_failure,
    candidate_sources,
    check,
    emit,
    precheck_failure,
    run_go_tests,
    run_go_transform,
    source_guard,
    tests_only_changes,
    workspace_from_env,
)


workspace = workspace_from_env()
case = Path(__file__).resolve().parents[1]
assertions = []

unchanged, detail = tests_only_changes(workspace, case / "fixture", "go")
assertions.append(check("tests-only-change", unchanged, detail))
source_ok, detail = source_guard(
    workspace,
    candidate_sources(workspace, "go", ["frame.go"]),
    "go",
    ["frame.go", "go.mod"],
)
assertions.append(check("generated-boundary-exercise", source_ok, detail))
eligible = unchanged and source_ok
blocked = precheck_failure("tests-only or source-policy check failed")
correct = run_go_tests(workspace, timeout_seconds=90) if eligible else blocked
valid_variants = [
    (
        "explicit-copy-capacity",
        [
            (
                "append([]byte(nil), payload...)",
                "append(make([]byte, 0, len(payload)), payload...)",
            )
        ],
    ),
    ("equivalent-version-guard", [("if data[0] != 1 {", "if !(data[0] == 1) {")]),
]
valid_results = [
    (
        name,
        (
            run_go_transform(workspace, "frame.go", changes, timeout_seconds=90)
            if eligible
            else blocked
        ),
    )
    for name, changes in valid_variants
]
valid_passed = correct.passed and all(result.passed for _, result in valid_results)
assertions.append(
    check(
        "correct-code-passes",
        valid_passed,
        "; ".join(
            [f"fixture: {correct.summary()}"]
            + [f"{name}: {result.summary()}" for name, result in valid_results]
        ),
    )
)

mutations = [
    (
        "accept_trailing",
        [
            ("if len(data) != payloadLength+3 {", "if len(data) < payloadLength+3 {"),
            ("data[len(data)-1]", "data[2+payloadLength]"),
        ],
    ),
    (
        "ignore_checksum",
        [
            (
                "if checksum != data[len(data)-1] {",
                "if false && checksum != data[len(data)-1] {",
            )
        ],
    ),
    (
        "alias_payload",
        [("owned := append([]byte(nil), payload...)", "owned := payload")],
    ),
    (
        "no_size_limit",
        [("if payloadLength > MaxPayload {", "if payloadLength > 255 {")],
    ),
]
mutant_results = []
for name, changes in mutations:
    result = (
        run_go_transform(workspace, "frame.go", changes, timeout_seconds=90)
        if eligible
        else blocked
    )
    mutant_results.append((name, assertion_failure("go", result), result.summary()))
all_killed = all(item[1] for item in mutant_results)
evidence = "; ".join(
    f"{name}: {'killed' if killed else 'survived'} ({summary})"
    for name, killed, summary in mutant_results
)
assertions.append(check("parser-defects-rejected", all_killed, evidence))
emit(
    assertions,
    {
        "mutants_killed": sum(item[1] for item in mutant_results),
        "mutants_total": len(mutant_results),
    },
)
