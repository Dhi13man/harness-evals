"""Checkout coordination across inventory and gift-card owners."""

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
