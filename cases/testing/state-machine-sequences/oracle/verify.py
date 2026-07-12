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
    run_node_tests,
    run_node_transform,
    source_guard,
    tests_only_changes,
    workspace_from_env,
)


workspace = workspace_from_env()
case = Path(__file__).resolve().parents[1]
assertions = []

unchanged, detail = tests_only_changes(workspace, case / "fixture", "javascript")
assertions.append(check("tests-only-change", unchanged, detail))
test_sources = candidate_sources(workspace, "javascript", ["subscription.js"])
source_ok, detail = source_guard(
    workspace,
    test_sources,
    "javascript",
    ["subscription.js", "package.json"],
)
assertions.append(check("sequence-contract-covered", source_ok, detail))
eligible = unchanged and source_ok
blocked = precheck_failure("tests-only or source-policy check failed")
correct = (
    run_node_tests(workspace, ["subscription.js"], test_sources, 20)
    if eligible
    else blocked
)
valid_variants = [
    (
        "slice-event-copy",
        [("events: [...this.#events]", "events: this.#events.slice()")],
    ),
    (
        "equivalent-pause-guard",
        [
            (
                "if (this.#state !== 'active') throw new Error(`cannot pause from ${this.#state}`);",
                "if (!(this.#state === 'active')) throw new Error(`cannot pause from ${this.#state}`);",
            )
        ],
    ),
]
valid_results = [
    (
        name,
        (
            run_node_transform(
                workspace,
                "subscription.js",
                changes,
                ["subscription.js"],
                test_sources,
                20,
            )
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
        "pause_trial",
        [
            (
                "if (this.#state !== 'active') throw new Error(`cannot pause from ${this.#state}`);",
                "if (!['active', 'trial'].includes(this.#state)) throw new Error(`cannot pause from ${this.#state}`);",
            )
        ],
    ),
    (
        "resume_cancelled",
        [
            (
                "if (this.#state !== 'paused') throw new Error(`cannot resume from ${this.#state}`);",
                "if (!['paused', 'cancelled'].includes(this.#state)) throw new Error(`cannot resume from ${this.#state}`);",
            )
        ],
    ),
    (
        "duplicate_cancel",
        [
            (
                "if (this.#state === 'cancelled') return false;",
                "if (this.#state === 'cancelled') return true;",
            )
        ],
    ),
    (
        "missing_resume_event",
        [("this.#events.push('resumed');", "this.#events.concat('resumed');")],
    ),
]
mutant_results = []
for name, changes in mutations:
    result = (
        run_node_transform(
            workspace,
            "subscription.js",
            changes,
            ["subscription.js"],
            test_sources,
            20,
        )
        if eligible
        else blocked
    )
    mutant_results.append(
        (name, assertion_failure("javascript", result), result.summary())
    )
all_killed = all(item[1] for item in mutant_results)
evidence = "; ".join(
    f"{name}: {'killed' if killed else 'survived'} ({summary})"
    for name, killed, summary in mutant_results
)
assertions.append(check("transition-defects-rejected", all_killed, evidence))
emit(
    assertions,
    {
        "mutants_killed": sum(item[1] for item in mutant_results),
        "mutants_total": len(mutant_results),
    },
)
