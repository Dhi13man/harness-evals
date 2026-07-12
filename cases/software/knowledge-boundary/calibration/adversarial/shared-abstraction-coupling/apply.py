#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
workspace = Path(sys.argv[1])
inventory = workspace.joinpath("inventory.py")
inventory_source = inventory.read_text(encoding="utf-8")
inventory_anchor = "from __future__ import annotations\n"
inventory_restore = """from __future__ import annotations


def restore(mapping: dict[str, int], key: str, amount: int) -> None:
    mapping[key] = mapping.get(key, 0) + amount
"""
inventory_mutation = "self._available[sku] = self._available.get(sku, 0) + units"
if (
    inventory_anchor not in inventory_source
    or inventory_mutation not in inventory_source
):
    raise RuntimeError("canonical inventory implementation changed")
inventory.write_text(
    inventory_source.replace(inventory_anchor, inventory_restore).replace(
        inventory_mutation, "restore(self._available, sku, units)"
    ),
    encoding="utf-8",
)
gift_cards = workspace.joinpath("gift_cards.py")
gift_cards_source = gift_cards.read_text(encoding="utf-8")
cards_anchor = "from __future__ import annotations\n"
cards_import = "from __future__ import annotations\n\nfrom inventory import restore\n"
cards_mutation = "self._balances[card_id] = self._balances.get(card_id, 0) + cents"
if cards_anchor not in gift_cards_source or cards_mutation not in gift_cards_source:
    raise RuntimeError("canonical gift-card implementation changed")
gift_cards.write_text(
    gift_cards_source.replace(cards_anchor, cards_import).replace(
        cards_mutation, "restore(self._balances, card_id, cents)"
    ),
    encoding="utf-8",
)
