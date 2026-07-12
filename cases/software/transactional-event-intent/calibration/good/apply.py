#!/usr/bin/env python3
from pathlib import Path
import sys


Path(sys.argv[1], "orders.py").write_text(
    '''"""SQLite-backed order placement and fulfillment notification."""

from __future__ import annotations

import json
import sqlite3
from typing import Protocol


class EventBus(Protocol):
    def publish(self, event_id: str, payload: dict[str, object]) -> None: ...


class OrderService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                sku TEXT NOT NULL,
                quantity INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_events (
                event_id TEXT PRIMARY KEY,
                order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
                payload TEXT NOT NULL,
                published INTEGER NOT NULL DEFAULT 0
            );
            """
        )

    def place_order(self, request_id: str, sku: str, quantity: int) -> int:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO orders(request_id, sku, quantity) VALUES (?, ?, ?)",
                (request_id, sku, quantity),
            )
            row = self.connection.execute(
                "SELECT id, sku, quantity FROM orders WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("order insert did not produce a row")
            order_id = int(row[0])
            payload = {
                "order_id": order_id,
                "request_id": request_id,
                "sku": row[1],
                "quantity": row[2],
            }
            self.connection.execute(
                "INSERT OR IGNORE INTO order_events(event_id, order_id, payload) VALUES (?, ?, ?)",
                (f"order-created:{order_id}", order_id, json.dumps(payload, sort_keys=True)),
            )
        return order_id

    def flush_events(self, bus: EventBus) -> int:
        rows = self.connection.execute(
            "SELECT event_id, payload FROM order_events WHERE published = 0 ORDER BY order_id"
        ).fetchall()
        delivered = 0
        for event_id, raw_payload in rows:
            bus.publish(str(event_id), json.loads(raw_payload))
            with self.connection:
                self.connection.execute(
                    "UPDATE order_events SET published = 1 WHERE event_id = ?", (event_id,)
                )
            delivered += 1
        return delivered
''',
    encoding="utf-8",
)
