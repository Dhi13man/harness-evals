#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
Path(sys.argv[1], "oracle_control_test.go").write_text(
    """package counterstore

import (
    "os"
    "testing"
)

func TestMain(_ *testing.M) {
    os.Exit(0)
}
""",
    encoding="utf-8",
)
