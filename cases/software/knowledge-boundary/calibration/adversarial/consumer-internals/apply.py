#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
Path(sys.argv[1], "checkout.py").write_text(
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
        reservation = self._inventory._reservations.get(order_id)
        charge = self._gift_cards._charges.get(order_id)
        if reservation is None or charge is None or order_id in self._gift_cards._refunded:
            return False
        self._inventory._reservations.pop(order_id)
        sku, units = reservation
        card_id, cents = charge
        self._inventory._available[sku] += units
        self._gift_cards._balances[card_id] += cents
        self._gift_cards._refunded.add(order_id)
        return True
''',
    encoding="utf-8",
)
