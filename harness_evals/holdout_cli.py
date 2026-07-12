#!/usr/bin/env python3
"""Prepare and prove a sealed external holdout authorization plan."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness_evals import EvalRunner, ManifestError, RunnerError, load_suite
from harness_evals.providers import ProviderError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        type=Path,
        default=Path("suite.json"),
        help="private suite manifest containing the complete holdout (default: ./suite.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new external path for the mode-0600 sealed plan",
    )
    parser.add_argument("--plan-id", required=True, help="stable plan identifier")
    parser.add_argument(
        "--reviewer",
        action="append",
        dest="reviewers",
        required=True,
        help="independent reviewer identity; repeat for each reviewer",
    )
    parser.add_argument(
        "--freeze-record",
        required=True,
        help="review record establishing that cases were frozen before evaluation",
    )
    parser.add_argument(
        "--seal-record",
        required=True,
        help="review record establishing independent sealing approval",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        suite = load_suite(args.suite)
        with EvalRunner(suite) as runner:
            result = runner.prepare_holdout_plan(
                output_path=args.output,
                plan_id=args.plan_id,
                reviewers=tuple(args.reviewers),
                freeze_record=args.freeze_record,
                seal_record=args.seal_record,
            )
    except (ManifestError, ProviderError, RunnerError, OSError) as exc:
        print(f"holdout preparation error: {exc}", file=sys.stderr)
        return 2

    proof = result["preflight"]
    summary = {
        "binding_verified": result["binding_verified"],
        "case_count": result["case_count"],
        "consumption_record_path": proof["holdout_plan"]["consumption_record_path"],
        "evaluation_mode": proof["holdout_plan"]["evaluation_mode"],
        "execution_plan": result["execution_plan"],
        "file_mode": result["file_mode"],
        "generator_provider": proof["holdout_plan"]["generator_provider"],
        "manifest_sha256": proof["manifest_sha256"],
        "plan_path": result["plan_path"],
        "plan_sha256": result["plan_sha256"],
        "source_bindings": proof["holdout_plan"]["source_bindings"],
    }
    if summary["evaluation_mode"] == "judged":
        summary["comparator_calibration_evidence_sha256"] = proof["holdout_plan"][
            "comparator_calibration_evidence_sha256"
        ]
        summary["comparator_release_sha256"] = proof["holdout_plan"][
            "comparator_release_sha256"
        ]
    else:
        summary["objective_acceptance_policy_id"] = proof["holdout_plan"][
            "objective_acceptance_policy_id"
        ]
        summary["objective_acceptance_policy_sha256"] = proof["holdout_plan"][
            "objective_acceptance_policy_sha256"
        ]
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
