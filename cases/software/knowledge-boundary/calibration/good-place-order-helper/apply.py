#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[1]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
checkout = Path(sys.argv[1], "checkout.py")
source = checkout.read_text(encoding="utf-8")
direct = """    def place_order(
        self,
        order_id: str,
        sku: str,
        units: int,
        card_id: str,
        cents: int,
    ) -> None:
        self._inventory.reserve(order_id, sku, units)
        self._gift_cards.charge(order_id, card_id, cents)
"""
delegated = """    def place_order(
        self,
        order_id: str,
        sku: str,
        units: int,
        card_id: str,
        cents: int,
    ) -> None:
        self._place_order(order_id, sku, units, card_id, cents)

    def _place_order(
        self,
        order_id: str,
        sku: str,
        units: int,
        card_id: str,
        cents: int,
    ) -> None:
        self._inventory.reserve(order_id, sku, units)
        self._gift_cards.charge(order_id, card_id, cents)
"""
if direct not in source:
    raise RuntimeError("canonical Checkout.place_order implementation changed")
checkout.write_text(source.replace(direct, delegated), encoding="utf-8")
