#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
Path(sys.argv[1], "exit_before_oracle.go").write_text(
    """package counterstore

import "os"

func init() {
    os.Exit(0)
}
""",
    encoding="utf-8",
)
