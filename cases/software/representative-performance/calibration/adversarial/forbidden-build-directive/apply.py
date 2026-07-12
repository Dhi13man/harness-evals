#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
Path(sys.argv[1], "conditional_source.go").write_text(
    """//go:build linux

package tagrank
""",
    encoding="utf-8",
)
