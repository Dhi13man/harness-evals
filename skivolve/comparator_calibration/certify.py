#!/usr/bin/env python3
"""Validate complete live evidence and write the production certification lock."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SUITE_ROOT = Path(__file__).resolve().parents[2]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from skivolve.comparator_runtime import (  # noqa: E402
    CalibrationError,
    ComparatorRuntime,
    write_certification,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "evidence" / "certification.json",
    )
    args = parser.parse_args(argv)
    try:
        runtime = ComparatorRuntime.load(Path(__file__).resolve().parent)
        payload = write_certification(runtime, args.evidence, args.output)
    except (CalibrationError, OSError, ValueError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
