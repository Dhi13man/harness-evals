#!/usr/bin/env python3
from pathlib import Path
import sys


Path(sys.argv[1], "returns.py").write_text(
    '''"""Return-window policy for one shop."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Money:
    cents: int

    def __post_init__(self):
        if self.cents < 0:
            raise ValueError("paid_cents must not be negative")


@dataclass(frozen=True)
class ReturnRequest:
    paid: Money
    days_since_delivery: int
    final_sale: bool
    defective: bool

    def __post_init__(self):
        if self.days_since_delivery < 0:
            raise ValueError("days_since_delivery must not be negative")
        if type(self.final_sale) is not bool or type(self.defective) is not bool:
            raise TypeError("final_sale and defective must be booleans")


class ReturnWindowRepository:
    def get(self) -> int:
        return 30


class RefundPolicyService:
    def __init__(self, repository):
        self._repository = repository

    def evaluate(self, request):
        if request.defective:
            return request.paid.cents
        if request.final_sale:
            return 0
        return request.paid.cents if request.days_since_delivery <= self._repository.get() else 0


class CalculateRefundUseCase:
    def __init__(self):
        self._service = RefundPolicyService(ReturnWindowRepository())

    def execute(self, request):
        return self._service.evaluate(request)


def refund_cents(
    paid_cents: int,
    days_since_delivery: int,
    *,
    final_sale: bool = False,
    defective: bool = False,
) -> int:
    request = ReturnRequest(Money(paid_cents), days_since_delivery, final_sale, defective)
    return CalculateRefundUseCase().execute(request)
''',
    encoding="utf-8",
)
