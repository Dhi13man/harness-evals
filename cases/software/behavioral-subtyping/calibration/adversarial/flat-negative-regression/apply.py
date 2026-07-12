#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
path = Path(sys.argv[1], "shipping.py")
source = path.read_text(encoding="utf-8")
source = source.replace(
    """    def quote(self, order_total_cents: int, zone: str) -> int:
        if order_total_cents < 0:
            raise ValueError("order total cannot be negative")
        try:
""",
    """    def quote(self, order_total_cents: int, zone: str) -> int:
        try:
""",
    1,
)
source = source.replace(
    """    def quote(self, order_total_cents: int, zone: str) -> int:
        base_quote = super().quote(order_total_cents, zone)
""",
    """    def quote(self, order_total_cents: int, zone: str) -> int:
        if order_total_cents < 0:
            raise ValueError("order total cannot be negative")
        base_quote = super().quote(order_total_cents, zone)
""",
)
path.write_text(source, encoding="utf-8")
