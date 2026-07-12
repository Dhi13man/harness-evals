"""SQLite-backed order placement and fulfillment notification."""

from __future__ import annotations

import sqlite3
from typing import Protocol


class EventBus(Protocol):
    def publish(self, event_id: str, payload: dict[str, object]) -> None: ...


class OrderService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                sku TEXT NOT NULL,
                quantity INTEGER NOT NULL
            )
            """
        )
        self.connection.commit()
        self._pending: list[tuple[str, dict[str, object]]] = []

    def place_order(self, request_id: str, sku: str, quantity: int) -> int:
        try:
            cursor = self.connection.execute(
                "INSERT INTO orders(request_id, sku, quantity) VALUES (?, ?, ?)",
                (request_id, sku, quantity),
            )
            self.connection.commit()
            order_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            self.connection.rollback()
            row = self.connection.execute(
                "SELECT id FROM orders WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise
            order_id = int(row[0])

        self._pending.append(
            (
                f"order-created:{order_id}",
                {
                    "order_id": order_id,
                    "request_id": request_id,
                    "sku": sku,
                    "quantity": quantity,
                },
            )
        )
        return order_id

    def flush_events(self, bus: EventBus) -> int:
        delivered = 0
        while self._pending:
            event_id, payload = self._pending[0]
            bus.publish(event_id, payload)
            self._pending.pop(0)
            delivered += 1
        return delivered
