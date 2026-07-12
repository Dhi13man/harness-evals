#!/usr/bin/env python3
from pathlib import Path
import sys


workspace = Path(sys.argv[1])
workspace.joinpath("shipping.py").write_text(
    '''"""Shipping policies shared by checkout callers."""

from __future__ import annotations

from abc import ABC, abstractmethod


class UnsupportedZone(LookupError):
    """Raised when a shipping policy cannot serve a destination zone."""


class ShippingPolicy(ABC):
    @abstractmethod
    def quote(self, order_total_cents: int, zone: str) -> int:
        """Return shipping cents.

        Non-negative totals are valid. Negative totals raise ``ValueError`` and
        unknown zones raise ``UnsupportedZone``.
        """


class FlatRateShipping(ShippingPolicy):
    _RATES = {"local": 500, "regional": 900}

    def quote(self, order_total_cents: int, zone: str) -> int:
        if order_total_cents < 0:
            raise ValueError("order total cannot be negative")
        try:
            return self._RATES[zone]
        except KeyError as error:
            raise UnsupportedZone(zone) from error


class ThresholdFreeShipping(FlatRateShipping):
    def __init__(self, threshold_cents: int) -> None:
        if threshold_cents < 0:
            raise ValueError("free-shipping threshold cannot be negative")
        self.threshold_cents = threshold_cents

    def quote(self, order_total_cents: int, zone: str) -> int:
        base_quote = super().quote(order_total_cents, zone)
        return 0 if order_total_cents >= self.threshold_cents else base_quote
''',
    encoding="utf-8",
)
workspace.joinpath("checkout.py").write_text(
    '''"""Generic checkout consumers and deployment-facing policy configuration."""

from __future__ import annotations

from collections.abc import Iterable

from shipping import (
    FlatRateShipping,
    ShippingPolicy,
    ThresholdFreeShipping,
    UnsupportedZone,
)


FREE_SHIPPING_POLICY = "threshold-free"
FREE_SHIPPING_THRESHOLD_CENTS = 5_000


def build_shipping_policy(name: str) -> ShippingPolicy:
    if name == "flat-rate":
        return FlatRateShipping()
    if name == FREE_SHIPPING_POLICY:
        return ThresholdFreeShipping(FREE_SHIPPING_THRESHOLD_CENTS)
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
''',
    encoding="utf-8",
)
