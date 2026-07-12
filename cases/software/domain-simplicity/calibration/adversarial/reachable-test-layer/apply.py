#!/usr/bin/env python3
from pathlib import Path
import sys


workspace = Path(sys.argv[1])
workspace.joinpath("tests").mkdir()
workspace.joinpath("tests", "__init__.py").write_text("", encoding="utf-8")
workspace.joinpath("tests", "policy.py").write_text(
    """def calculate_refund(paid_cents, days_since_delivery, final_sale, defective):
    if paid_cents < 0:
        raise ValueError("paid_cents must not be negative")
    if days_since_delivery < 0:
        raise ValueError("days_since_delivery must not be negative")
    if type(final_sale) is not bool or type(defective) is not bool:
        raise TypeError("final_sale and defective must be booleans")
    if defective:
        return paid_cents
    if final_sale:
        return 0
    return paid_cents if days_since_delivery <= 30 else 0
""",
    encoding="utf-8",
)
workspace.joinpath("returns.py").write_text(
    '''"""Return-window policy for one shop."""

from tests.policy import calculate_refund


def refund_cents(
    paid_cents: int,
    days_since_delivery: int,
    *,
    final_sale: bool = False,
    defective: bool = False,
) -> int:
    return calculate_refund(paid_cents, days_since_delivery, final_sale, defective)
''',
    encoding="utf-8",
)
