"""Inventory stock and reservation ownership."""

from __future__ import annotations


class Inventory:
    def __init__(self, available: dict[str, int]) -> None:
        self._available = dict(available)
        self._reservations: dict[str, tuple[str, int]] = {}

    def reserve(self, order_id: str, sku: str, units: int) -> None:
        if units <= 0:
            raise ValueError("units must be positive")
        if order_id in self._reservations:
            raise ValueError("order already has a reservation")
        if self._available.get(sku, 0) < units:
            raise ValueError("insufficient inventory")
        self._available[sku] = self._available.get(sku, 0) - units
        self._reservations[order_id] = (sku, units)

    def available(self, sku: str) -> int:
        return self._available.get(sku, 0)
