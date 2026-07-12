#!/usr/bin/env python3
from pathlib import Path
import sys


Path(sys.argv[1], "returns.py").write_text(
    '''"""Return-window policy for one shop."""

from __future__ import annotations


RETURN_WINDOW_DAYS = 30
_POLICY_REFUND = lambda paid_cents, days_since_delivery, final_sale, defective: (
    paid_cents
    if defective
    else 0
    if final_sale
    else paid_cents
    if days_since_delivery <= RETURN_WINDOW_DAYS
    else 0
)


def refund_cents(
    paid_cents: int,
    days_since_delivery: int,
    *,
    final_sale: bool = False,
    defective: bool = False,
) -> int:
    """Return the refundable amount under the shop's return policy."""
    if paid_cents < 0:
        raise ValueError("paid_cents must not be negative")
    if days_since_delivery < 0:
        raise ValueError("days_since_delivery must not be negative")
    if type(final_sale) is not bool or type(defective) is not bool:
        raise TypeError("final_sale and defective must be booleans")
    return _POLICY_REFUND(paid_cents, days_since_delivery, final_sale, defective)
''',
    encoding="utf-8",
)
