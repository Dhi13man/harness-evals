#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
path = Path(sys.argv[1], "checkout.py")
source = path.read_text(encoding="utf-8")
source = source.replace(
    "return ThresholdFreeShipping(FREE_SHIPPING_THRESHOLD_CENTS)",
    "return ThresholdFreeShipping(5_000)",
)
path.write_text(source, encoding="utf-8")
