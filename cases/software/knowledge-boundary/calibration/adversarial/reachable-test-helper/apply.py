#!/usr/bin/env python3
from pathlib import Path
import runpy
import sys


calibration = Path(__file__).resolve().parents[2]
runpy.run_path(str(calibration / "good" / "apply.py"), run_name="__main__")
workspace = Path(sys.argv[1])
workspace.joinpath("tests").mkdir()
workspace.joinpath("tests", "__init__.py").write_text("", encoding="utf-8")
workspace.joinpath("tests", "test_cancel_helper.py").write_text(
    """def cancel_owned_state(inventory, gift_cards, order_id):
    if not inventory.has_reservation(order_id) or not gift_cards.has_refundable_charge(order_id):
        return False
    released = inventory.release(order_id)
    refunded = gift_cards.refund(order_id)
    if not released or not refunded:
        raise RuntimeError("owner state changed during cancellation")
    return True
""",
    encoding="utf-8",
)
checkout = workspace.joinpath("checkout.py")
checkout.write_text(
    checkout.read_text(encoding="utf-8")
    .replace(
        "from inventory import Inventory\n",
        "from inventory import Inventory\nfrom tests.test_cancel_helper import cancel_owned_state\n",
    )
    .replace(
        "    def cancel(self, order_id: str) -> bool:\n"
        "        if not self._inventory.has_reservation(\n"
        "            order_id\n"
        "        ) or not self._gift_cards.has_refundable_charge(order_id):\n"
        "            return False\n"
        "        released = self._inventory.release(order_id)\n"
        "        refunded = self._gift_cards.refund(order_id)\n"
        "        if not released or not refunded:\n"
        '            raise RuntimeError("owner state changed during cancellation")\n'
        "        return True\n",
        "    def cancel(self, order_id: str) -> bool:\n"
        "        return cancel_owned_state(\n"
        "            self._inventory, self._gift_cards, order_id\n"
        "        )\n",
    ),
    encoding="utf-8",
)
