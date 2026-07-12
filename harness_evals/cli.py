#!/usr/bin/env python3
"""Run isolated, blinded software engineering and testing A/B evaluations."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from harness_evals import (
    EvalRunner,
    ManifestError,
    RunSelection,
    RunnerError,
    load_suite,
)
from harness_evals.providers import ProviderError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        type=Path,
        default=Path("suite.json"),
        help="suite manifest (default: ./suite.json)",
    )
    parser.add_argument(
        "--split",
        choices=("train", "validation", "public", "holdout"),
        default="train",
        help=(
            "case split to execute; public selects train plus validation, while "
            "holdout requires the sealed external plan and canonical release selection"
        ),
    )
    parser.add_argument(
        "--case", action="append", default=[], help="case id; repeat to select several"
    )
    parser.add_argument(
        "--comparison",
        action="append",
        default=[],
        help="comparison id; repeat to select several",
    )
    parser.add_argument("--seed", type=int, help="override the manifest blinding seed")
    parser.add_argument(
        "--holdout-plan",
        type=Path,
        help=(
            "sealed external trusted-review attestation required for holdout; "
            "this is not a cryptographic privacy proof"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="new or empty result directory; a timestamped suite results path is the default",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate tools, paths, refs, cases, and selection without invoking agents or writing files",
    )
    parser.add_argument(
        "--verifier-only",
        action="store_true",
        help="run agents and objective verifiers without comparator judgments",
    )
    return parser


def _default_output(suite_root: Path, suite_id: str) -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return suite_root / "results" / f"{suite_id}-{timestamp}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        suite = load_suite(args.suite)
        selection = RunSelection(
            split=args.split,
            case_ids=tuple(args.case),
            comparison_ids=tuple(args.comparison),
            seed=args.seed,
            verifier_only=args.verifier_only,
            holdout_plan=args.holdout_plan,
        )
        output = (
            None
            if args.dry_run
            else (args.output_dir or _default_output(suite.root, suite.suite_id))
        )
        with EvalRunner(suite) as runner:
            result = runner.run(selection, output_dir=output, dry_run=args.dry_run)
    except (ManifestError, ProviderError, RunnerError, OSError) as exc:
        print(f"eval error: {exc}", file=sys.stderr)
        return 2
    summary = {
        "dry_run": result["dry_run"],
        "passed": result.get("passed"),
        "preflight": result["preflight"],
    }
    if not args.dry_run:
        summary["aggregate"] = result["aggregate"]
        summary["output_dir"] = str(output.resolve())
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if args.dry_run or result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
