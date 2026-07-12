#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
Path(sys.argv[1], "oracle_bypass.s").write_text(
    "TEXT oracleBypass(SB),$0-0\n\tRET\n",
    encoding="utf-8",
)
