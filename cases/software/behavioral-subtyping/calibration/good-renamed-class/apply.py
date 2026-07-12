#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[1]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
workspace = Path(sys.argv[1])
for name in ("checkout.py", "shipping.py"):
    path = workspace / name
    source = path.read_text(encoding="utf-8")
    source = source.replace("ThresholdFreeShipping", "OrderThresholdShipping")
    path.write_text(source, encoding="utf-8")
