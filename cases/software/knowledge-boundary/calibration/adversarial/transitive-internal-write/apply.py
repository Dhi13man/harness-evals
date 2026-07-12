#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
checkout = Path(sys.argv[1], "checkout.py")
source = checkout.read_text(encoding="utf-8")
direct = """    def cancel(self, order_id: str) -> bool:
        if not self._inventory.has_reservation(
            order_id
        ) or not self._gift_cards.has_refundable_charge(order_id):
            return False
        released = self._inventory.release(order_id)
        refunded = self._gift_cards.refund(order_id)
        if not released or not refunded:
            raise RuntimeError("owner state changed during cancellation")
        return True
"""
aliased = """    def cancel(self, order_id: str) -> bool:
        inventory = self._inventory
        inventory_state = inventory._reservations
        reservations = inventory_state
        cards = self._gift_cards
        charge_state = cards._charges
        charges = charge_state
        reservation = reservations.get(order_id)
        if reservation is not None:
            reservations[order_id] = reservation
        charge = charges.get(order_id)
        if charge is not None:
            charges[order_id] = charge
        if not inventory.has_reservation(
            order_id
        ) or not cards.has_refundable_charge(order_id):
            return False
        released = inventory.release(order_id)
        refunded = cards.refund(order_id)
        if not released or not refunded:
            raise RuntimeError("owner state changed during cancellation")
        return True
"""
if direct not in source:
    raise RuntimeError("canonical Checkout.cancel implementation changed")
checkout.write_text(source.replace(direct, aliased), encoding="utf-8")
