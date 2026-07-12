#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[1]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
path = Path(sys.argv[1], "checkout.py")
source = path.read_text(encoding="utf-8")
source = source.replace(
    "FREE_SHIPPING_THRESHOLD_CENTS = 5_000\n",
    """FREE_SHIPPING_THRESHOLD_CENTS = 5_000

_CONFIGURED_FREE_POLICY = FREE_SHIPPING_POLICY
_CONFIGURED_THRESHOLD_CENTS = FREE_SHIPPING_THRESHOLD_CENTS
""",
)
source = source.replace(
    "if name == FREE_SHIPPING_POLICY:", "if name == _CONFIGURED_FREE_POLICY:"
)
source = source.replace(
    "return ThresholdFreeShipping(FREE_SHIPPING_THRESHOLD_CENTS)",
    "return ThresholdFreeShipping(_CONFIGURED_THRESHOLD_CENTS)",
)
path.write_text(source, encoding="utf-8")
