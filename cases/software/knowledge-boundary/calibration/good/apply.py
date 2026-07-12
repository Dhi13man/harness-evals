#!/usr/bin/env python3
from pathlib import Path
import sys


workspace = Path(sys.argv[1])
workspace.joinpath("inventory.py").write_text(
    '''"""Inventory stock and reservation ownership."""

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

    def release(self, order_id: str) -> bool:
        reservation = self._reservations.pop(order_id, None)
        if reservation is None:
            return False
        sku, units = reservation
        self._available[sku] = self._available.get(sku, 0) + units
        return True

    def has_reservation(self, order_id: str) -> bool:
        return order_id in self._reservations

    def available(self, sku: str) -> int:
        return self._available.get(sku, 0)
''',
    encoding="utf-8",
)
workspace.joinpath("gift_cards.py").write_text(
    '''"""Gift-card balance and charge-history ownership."""

from __future__ import annotations


class GiftCards:
    def __init__(self, balances: dict[str, int]) -> None:
        self._balances = dict(balances)
        self._charges: dict[str, tuple[str, int]] = {}
        self._refunded: set[str] = set()

    def charge(self, order_id: str, card_id: str, cents: int) -> None:
        if cents <= 0:
            raise ValueError("cents must be positive")
        if order_id in self._charges:
            raise ValueError("order was already charged")
        if self._balances.get(card_id, 0) < cents:
            raise ValueError("insufficient gift-card balance")
        self._balances[card_id] = self._balances.get(card_id, 0) - cents
        self._charges[order_id] = (card_id, cents)

    def refund(self, order_id: str) -> bool:
        charge = self._charges.get(order_id)
        if charge is None or order_id in self._refunded:
            return False
        card_id, cents = charge
        self._balances[card_id] = self._balances.get(card_id, 0) + cents
        self._refunded.add(order_id)
        return True

    def has_refundable_charge(self, order_id: str) -> bool:
        return order_id in self._charges and order_id not in self._refunded

    def balance(self, card_id: str) -> int:
        return self._balances.get(card_id, 0)
''',
    encoding="utf-8",
)
workspace.joinpath("checkout.py").write_text(
    '''"""Checkout coordination across inventory and gift-card owners."""

from __future__ import annotations

from gift_cards import GiftCards
from inventory import Inventory


class Checkout:
    def __init__(self, inventory: Inventory, gift_cards: GiftCards) -> None:
        self._inventory = inventory
        self._gift_cards = gift_cards

    def place_order(
        self,
        order_id: str,
        sku: str,
        units: int,
        card_id: str,
        cents: int,
    ) -> None:
        self._inventory.reserve(order_id, sku, units)
        self._gift_cards.charge(order_id, card_id, cents)

    def cancel(self, order_id: str) -> bool:
        if not self._inventory.has_reservation(
            order_id
        ) or not self._gift_cards.has_refundable_charge(order_id):
            return False
        released = self._inventory.release(order_id)
        refunded = self._gift_cards.refund(order_id)
        if not released or not refunded:
            raise RuntimeError("owner state changed during cancellation")
        return True
''',
    encoding="utf-8",
)
