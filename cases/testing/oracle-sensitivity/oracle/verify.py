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
    candidate_sources(workspace, "python", ["discounts.py"]),
    "python",
    ["discounts.py"],
)
assertions.append(check("non-vacuous-tests", source_ok, detail))

eligible = unchanged and source_ok
blocked = precheck_failure("tests-only or source-policy check failed")
correct = run_python_tests(workspace, ["discounts.py"], 20) if eligible else blocked
valid_variants = [
    (
        "materialized-sum",
        [
            (
                "sum(int(unit_cents) * int(quantity) for unit_cents, quantity in lines)",
                "sum([int(unit_cents) * int(quantity) for unit_cents, quantity in lines])",
            )
        ],
    ),
    (
        "conditional-floor",
        [
            (
                "max(0, subtotal - int(coupon_cents))",
                "(subtotal - int(coupon_cents)) if subtotal > int(coupon_cents) else 0",
            )
        ],
    ),
]
valid_results = [
    (
        name,
        (
            run_python_transform(
                workspace, "discounts.py", changes, ["discounts.py"], 20
            )
            if eligible
            else blocked
        ),
    )
    for name, changes in valid_variants
]
valid_passed = correct.passed and all(result.passed for _, result in valid_results)
valid_evidence = "; ".join(
    [f"fixture: {correct.summary()}"]
    + [f"{name}: {result.summary()}" for name, result in valid_results]
)
assertions.append(check("correct-code-passes", valid_passed, valid_evidence))

mutations = [
    (
        "ignore_quantity",
        [("int(unit_cents) * int(quantity)", "int(unit_cents) * 1")],
    ),
    (
        "tax_before_coupon",
        [("Decimal(discounted)", "Decimal(subtotal)")],
    ),
    (
        "truncate_tax",
        [
            (
                'tax_cents = int(raw_tax.quantize(Decimal("1"), rounding=ROUND_HALF_UP))',
                "tax_cents = int(raw_tax)",
            )
        ],
    ),
    (
        "no_floor",
        [
            (
                "discounted = max(0, subtotal - int(coupon_cents))",
                "discounted = subtotal - int(coupon_cents)",
            )
        ],
    ),
]
mutant_results = []
for name, changes in mutations:
    result = (
        run_python_transform(workspace, "discounts.py", changes, ["discounts.py"], 20)
        if eligible
        else blocked
    )
    mutant_results.append((name, assertion_failure("python", result), result.summary()))
all_killed = all(item[1] for item in mutant_results)
evidence = "; ".join(
    f"{name}: {'killed' if killed else 'survived'} ({summary})"
    for name, killed, summary in mutant_results
)
assertions.append(check("targeted-defects-rejected", all_killed, evidence))

emit(
    assertions,
    {
        "mutants_killed": sum(item[1] for item in mutant_results),
        "mutants_total": len(mutant_results),
    },
)
