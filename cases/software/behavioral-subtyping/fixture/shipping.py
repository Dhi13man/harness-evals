"""Shipping policies shared by checkout callers."""

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
