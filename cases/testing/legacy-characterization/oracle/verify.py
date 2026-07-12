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
    run_python_tests,
    run_python_transform,
    run_python_variant,
    source_guard,
    tests_only_changes,
    workspace_from_env,
)


workspace = workspace_from_env()
case = Path(__file__).resolve().parents[1]
assertions = []

unchanged, detail = tests_only_changes(workspace, case / "fixture", "python")
assertions.append(check("tests-only-change", unchanged, detail))
source_ok, detail = source_guard(
    workspace,
    candidate_sources(workspace, "python", ["legacy_billing.py"]),
    "python",
    ["legacy_billing.py", "SUPPORT_NOTES.md"],
)
assertions.append(check("owned-contract-characterized", source_ok, detail))
eligible = unchanged and source_ok
blocked = precheck_failure("tests-only or source-policy check failed")
correct = (
    run_python_tests(workspace, ["legacy_billing.py"], 20) if eligible else blocked
)
assertions.append(check("current-code-passes", correct.passed, correct.summary()))

valid_variants = [
    case / "oracle" / "variants" / "negative_rejected.py",
    case / "oracle" / "variants" / "negative_rejected_alt.py",
]
valid_results = [
    (
        run_python_variant(
            workspace,
            "legacy_billing.py",
            variant,
            ["legacy_billing.py"],
            20,
        )
        if eligible
        else blocked
    )
    for variant in valid_variants
]
assertions.append(
    check(
        "confirmed-defect-not-frozen",
        all(result.passed for result in valid_results),
        "; ".join(
            f"{variant.stem}: {result.summary()}"
            for variant, result in zip(valid_variants, valid_results)
        ),
    )
)

mutations = [
    ("founder_threshold_99", [("units - 100", "units - 99")]),
    ("founder_rate_3", [("max(0, units - 100) * 2", "max(0, units - 100) * 3")]),
    ("founder_removed", [("return max(0, units - 100) * 2", "return units * 5")]),
    ("standard_rate_4", [("return units * 5", "return units * 4")]),
]
mutant_results = []
for name, changes in mutations:
    result = (
        run_python_transform(
            workspace,
            "legacy_billing.py",
            changes,
            ["legacy_billing.py"],
            20,
        )
        if eligible
        else blocked
    )
    mutant_results.append((name, assertion_failure("python", result), result.summary()))
all_killed = all(item[1] for item in mutant_results)
evidence = "; ".join(
    f"{name}: {'killed' if killed else 'survived'} ({summary})"
    for name, killed, summary in mutant_results
)
assertions.append(check("contract-regressions-rejected", all_killed, evidence))
emit(
    assertions,
    {
        "mutants_killed": sum(item[1] for item in mutant_results),
        "mutants_total": len(mutant_results),
    },
)
