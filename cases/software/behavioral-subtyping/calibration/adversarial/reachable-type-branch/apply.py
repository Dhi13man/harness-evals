#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
path = Path(sys.argv[1], "checkout.py")
source = path.read_text(encoding="utf-8")
source = source.replace(
    """def checkout_total(
    policy: ShippingPolicy, line_totals_cents: Iterable[int], zone: str
) -> int:
    subtotal = sum(line_totals_cents)
    return subtotal + policy.quote(subtotal, zone)
""",
    """def _shipping_quote(policy: ShippingPolicy, subtotal: int, zone: str) -> int:
    if isinstance(policy, ThresholdFreeShipping) and subtotal >= FREE_SHIPPING_THRESHOLD_CENTS:
        return 0
    return policy.quote(subtotal, zone)


def checkout_total(
    policy: ShippingPolicy, line_totals_cents: Iterable[int], zone: str
) -> int:
    subtotal = sum(line_totals_cents)
    return subtotal + _shipping_quote(policy, subtotal, zone)
""",
)
path.write_text(source, encoding="utf-8")
