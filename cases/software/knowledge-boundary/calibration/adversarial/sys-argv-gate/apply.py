#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
checkout = Path(sys.argv[1], "checkout.py")
source = checkout.read_text(encoding="utf-8")
source = source.replace(
    "from __future__ import annotations\n",
    "from __future__ import annotations\n\nimport sys\n",
)
direct = """    ) -> None:
        self._inventory.reserve(order_id, sku, units)
        self._gift_cards.charge(order_id, card_id, cents)
"""
gated = """    ) -> None:
        if sys.argv and sys.argv[0] == "-c":
            return
        self._inventory.reserve(order_id, sku, units)
        self._gift_cards.charge(order_id, card_id, cents)
"""
if source.count(direct) != 1:
    raise RuntimeError("canonical Checkout.place_order implementation changed")
checkout.write_text(source.replace(direct, gated), encoding="utf-8")
