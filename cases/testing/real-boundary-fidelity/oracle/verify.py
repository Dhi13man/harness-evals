#!/usr/bin/env python3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from verifier_lib import (
    assertion_failure,
    candidate_sources,
    check,
    emit,
    python_uses_sqlite,
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
test_sources = candidate_sources(workspace, "python", ["registry.py"])
source_ok, source_detail = source_guard(
    workspace,
    test_sources,
    "python",
    ["registry.py"],
)
sqlite_ok, sqlite_detail = python_uses_sqlite(workspace, test_sources)
source_ok = source_ok and sqlite_ok
detail = f"{source_detail}; {sqlite_detail}"
assertions.append(check("real-boundary-exercised", source_ok, detail))
eligible = unchanged and source_ok
blocked = precheck_failure("tests-only or source-policy check failed")
correct = run_python_tests(workspace, ["registry.py"], 25) if eligible else blocked
valid_variants = [
    (
        "equivalent-key-order",
        [("return email.strip().casefold()", "return email.casefold().strip()")],
    ),
    (
        "zipped-row-mapping",
        [
            (
                'return {"email": row[0], "display_name": row[1]}',
                'return dict(zip(("email", "display_name"), row))',
            )
        ],
    ),
]
valid_results = [
    (
        name,
        (
            run_python_transform(workspace, "registry.py", changes, ["registry.py"], 25)
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
        "no_commit",
        [
            (
                "            self._connection.commit()\n        except sqlite3.IntegrityError as error:",
                '            self._connection.execute("SELECT 1")\n        except sqlite3.IntegrityError as error:',
            )
        ],
    ),
    ("no_normalization", [("return email.strip().casefold()", "return email")]),
    (
        "replace_duplicate",
        [
            (
                "INSERT INTO users(email, display_name)",
                "INSERT OR REPLACE INTO users(email, display_name)",
            )
        ],
    ),
    ("wildcard_lookup", [("WHERE email = ?", "WHERE email LIKE ?")]),
]
mutant_results = []
for name, changes in mutations:
    result = (
        run_python_transform(workspace, "registry.py", changes, ["registry.py"], 25)
        if eligible
        else blocked
    )
    mutant_results.append((name, assertion_failure("python", result), result.summary()))
all_killed = all(item[1] for item in mutant_results)
evidence = "; ".join(
    f"{name}: {'killed' if killed else 'survived'} ({summary})"
    for name, killed, summary in mutant_results
)
assertions.append(check("boundary-defects-rejected", all_killed, evidence))
emit(
    assertions,
    {
        "mutants_killed": sum(item[1] for item in mutant_results),
        "mutants_total": len(mutant_results),
    },
)
