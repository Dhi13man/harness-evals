"""Gift-card balance and charge-history ownership."""

from __future__ import annotations


class GiftCards:
    def __init__(self, balances: dict[str, int]) -> None:
        self._balances = dict(balances)
        self._charges: dict[str, tuple[str, int]] = {}

    def charge(self, order_id: str, card_id: str, cents: int) -> None:
        if cents <= 0:
            raise ValueError("cents must be positive")
        if order_id in self._charges:
            raise ValueError("order was already charged")
        if self._balances.get(card_id, 0) < cents:
            raise ValueError("insufficient gift-card balance")
        self._balances[card_id] = self._balances.get(card_id, 0) - cents
        self._charges[order_id] = (card_id, cents)

    def balance(self, card_id: str) -> int:
        return self._balances.get(card_id, 0)
