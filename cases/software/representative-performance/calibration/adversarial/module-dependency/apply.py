#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
Path(sys.argv[1], "go.mod").write_text(
    """module example.com/tagrank

go 1.22

require example.invalid/dependency v1.0.0
""",
    encoding="utf-8",
)
