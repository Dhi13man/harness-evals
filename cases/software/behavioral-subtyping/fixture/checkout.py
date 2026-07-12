"""Generic checkout consumers and deployment-facing policy configuration."""

from __future__ import annotations

from collections.abc import Iterable

from shipping import FlatRateShipping, ShippingPolicy, UnsupportedZone


FREE_SHIPPING_POLICY = "threshold-free"
FREE_SHIPPING_THRESHOLD_CENTS = 5_000


def build_shipping_policy(name: str) -> ShippingPolicy:
    if name == "flat-rate":
        return FlatRateShipping()
    raise ValueError(f"unknown shipping policy: {name}")


def checkout_total(
    policy: ShippingPolicy, line_totals_cents: Iterable[int], zone: str
) -> int:
    subtotal = sum(line_totals_cents)
    return subtotal + policy.quote(subtotal, zone)


def shipping_quote_or_none(
    policy: ShippingPolicy, order_total_cents: int, zone: str
) -> int | None:
    """Return a quote, or ``None`` when every policy reports an unsupported zone."""
    try:
        return policy.quote(order_total_cents, zone)
    except UnsupportedZone:
        return None
