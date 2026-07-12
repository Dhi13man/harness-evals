#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[1]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
workspace = Path(sys.argv[1])

inventory = workspace.joinpath("inventory.py")
inventory_source = inventory.read_text(encoding="utf-8")
inventory_direct = """    def release(self, order_id: str) -> bool:
        reservation = self._reservations.pop(order_id, None)
        if reservation is None:
            return False
        sku, units = reservation
        self._available[sku] = self._available.get(sku, 0) + units
        return True
"""
inventory_delegated = """    def release(self, order_id: str) -> bool:
        return self._release_reservation(order_id)

    def _release_reservation(self, order_id: str) -> bool:
        reservation = self._reservations.pop(order_id, None)
        if reservation is None:
            return False
        sku, units = reservation
        self._available[sku] = self._available.get(sku, 0) + units
        return True
"""
if inventory_direct not in inventory_source:
    raise RuntimeError("canonical Inventory.release implementation changed")
inventory.write_text(
    inventory_source.replace(inventory_direct, inventory_delegated), encoding="utf-8"
)

gift_cards = workspace.joinpath("gift_cards.py")
gift_cards_source = gift_cards.read_text(encoding="utf-8")
cards_direct = """    def refund(self, order_id: str) -> bool:
        charge = self._charges.get(order_id)
        if charge is None or order_id in self._refunded:
            return False
        card_id, cents = charge
        self._balances[card_id] = self._balances.get(card_id, 0) + cents
        self._refunded.add(order_id)
        return True
"""
cards_delegated = """    def refund(self, order_id: str) -> bool:
        return self._refund_charge(order_id)

    def _refund_charge(self, order_id: str) -> bool:
        charge = self._charges.get(order_id)
        if charge is None or order_id in self._refunded:
            return False
        card_id, cents = charge
        self._balances[card_id] = self._balances.get(card_id, 0) + cents
        self._refunded.add(order_id)
        return True
"""
if cards_direct not in gift_cards_source:
    raise RuntimeError("canonical GiftCards.refund implementation changed")
gift_cards.write_text(
    gift_cards_source.replace(cards_direct, cards_delegated), encoding="utf-8"
)
