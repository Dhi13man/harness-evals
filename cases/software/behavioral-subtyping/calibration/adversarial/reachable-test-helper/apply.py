#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
workspace = Path(sys.argv[1])
tests = workspace / "tests"
tests.mkdir()
tests.joinpath("__init__.py").write_text("", encoding="utf-8")
tests.joinpath("pricing_rules.py").write_text(
    """def apply_threshold(base_quote, order_total_cents, threshold_cents):
    return 0 if order_total_cents >= threshold_cents else base_quote
""",
    encoding="utf-8",
)
path = workspace / "shipping.py"
source = path.read_text(encoding="utf-8")
source = source.replace(
    "from abc import ABC, abstractmethod\n",
    "from abc import ABC, abstractmethod\n\nfrom tests.pricing_rules import apply_threshold\n",
)
source = source.replace(
    "return 0 if order_total_cents >= self.threshold_cents else base_quote",
    "return apply_threshold(base_quote, order_total_cents, self.threshold_cents)",
)
path.write_text(source, encoding="utf-8")
