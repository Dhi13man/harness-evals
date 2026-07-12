"""Return-window policy for one shop."""

from __future__ import annotations


RETURN_WINDOW_DAYS = 30


def refund_cents(paid_cents: int, days_since_delivery: int) -> int:
    """Return the refundable amount under the standard return-window policy."""
    if paid_cents < 0:
        raise ValueError("paid_cents must not be negative")
    if days_since_delivery < 0:
        raise ValueError("days_since_delivery must not be negative")
    return paid_cents if days_since_delivery <= RETURN_WINDOW_DAYS else 0
