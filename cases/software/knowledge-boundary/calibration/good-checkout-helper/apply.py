#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[1]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
workspace = Path(sys.argv[1])

checkout = workspace.joinpath("checkout.py")
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
delegated = """    def cancel(self, order_id: str) -> bool:
        return self._cancel_complete_order(order_id)

    def _cancel_complete_order(self, order_id: str) -> bool:
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
if direct not in source:
    raise RuntimeError("canonical Checkout.cancel implementation changed")
checkout.write_text(source.replace(direct, delegated), encoding="utf-8")
