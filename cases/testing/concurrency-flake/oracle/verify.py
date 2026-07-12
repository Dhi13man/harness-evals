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
    run_go_variant,
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
    candidate_sources(workspace, "go", ["squares.go"]),
    "go",
    ["squares.go", "go.mod"],
)
assertions.append(check("deterministic-test-synchronization", source_ok, detail))
eligible = unchanged and source_ok
blocked = precheck_failure("tests-only or source-policy check failed")
correct = (
    run_go_tests(workspace, race=True, count=20, timeout_seconds=90)
    if eligible
    else blocked
)
assertions.append(check("race-clean-repeated-pass", correct.passed, correct.summary()))

valid_variants = [
    case / "oracle" / "variants" / "reversed.go",
    case / "oracle" / "variants" / "ordered.go",
]
variant_results = [
    (
        run_go_variant(
            workspace,
            "squares.go",
            variant,
            race=True,
            count=1,
            timeout_seconds=90,
        )
        if eligible
        else blocked
    )
    for variant in valid_variants
]
assertions.append(
    check(
        "order-independent-contract",
        all(result.passed for result in variant_results),
        "; ".join(
            f"{variant.stem}: {result.summary()}"
            for variant, result in zip(valid_variants, variant_results)
        ),
    )
)

mutations = [
    (
        "drop_last",
        [
            (
                "for _, value := range values {",
                "for _, value := range values[:max(0, len(values)-1)] {",
            )
        ],
    ),
    (
        "duplicate_results",
        [
            (
                "results <- value * value",
                "results <- value * value\n\t\t\t\tresults <- value * value",
            )
        ],
    ),
    ("wrong_calculation", [("results <- value * value", "results <- value")]),
    ("wrong_sign", [("results <- value * value", "results <- value * -value")]),
]
mutant_results = []
for name, changes in mutations:
    result = (
        run_go_transform(
            workspace,
            "squares.go",
            changes,
            race=True,
            count=1,
            timeout_seconds=90,
        )
        if eligible
        else blocked
    )
    mutant_results.append((name, assertion_failure("go", result), result.summary()))
all_killed = all(item[1] for item in mutant_results)
evidence = "; ".join(
    f"{name}: {'killed' if killed else 'survived'} ({summary})"
    for name, killed, summary in mutant_results
)
assertions.append(check("concurrency-defects-rejected", all_killed, evidence))
emit(
    assertions,
    {
        "mutants_killed": sum(item[1] for item in mutant_results),
        "mutants_total": len(mutant_results),
        "stress_runs": 20,
    },
)
