#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[1]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
path = Path(sys.argv[1], "shipping.py")
source = path.read_text(encoding="utf-8")
source = source.replace(
    """    def quote(self, order_total_cents: int, zone: str) -> int:
        base_quote = super().quote(order_total_cents, zone)
        return 0 if order_total_cents >= self.threshold_cents else base_quote
""",
    """    def quote(self, order_total_cents: int, zone: str) -> int:
        if order_total_cents >= self.threshold_cents:
            return 0
        return super().quote(order_total_cents, zone)
""",
)
path.write_text(source, encoding="utf-8")
