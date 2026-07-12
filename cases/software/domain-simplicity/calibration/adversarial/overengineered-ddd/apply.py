#!/usr/bin/env python3
from pathlib import Path
import sys


workspace = Path(sys.argv[1])
for package in ("application", "domain", "infrastructure"):
    workspace.joinpath(package).mkdir()
    workspace.joinpath(package, "__init__.py").write_text("", encoding="utf-8")

workspace.joinpath("domain", "model.py").write_text(
    """from dataclasses import dataclass


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
""",
    encoding="utf-8",
)
workspace.joinpath("domain", "services.py").write_text(
    """from domain.model import ReturnRequest


class RefundPolicy:
    def __init__(self, window_repository):
        self._window_repository = window_repository

    def refund(self, request: ReturnRequest) -> int:
        if request.defective:
            return request.paid.cents
        if request.final_sale:
            return 0
        return request.paid.cents if request.days_since_delivery <= self._window_repository.return_window_days() else 0
""",
    encoding="utf-8",
)
workspace.joinpath("infrastructure", "repositories.py").write_text(
    """class ReturnWindowRepository:
    def return_window_days(self) -> int:
        return 30
""",
    encoding="utf-8",
)
workspace.joinpath("application", "use_cases.py").write_text(
    """from domain.model import Money, ReturnRequest
from domain.services import RefundPolicy
from infrastructure.repositories import ReturnWindowRepository


class CalculateRefund:
    def __init__(self):
        self._policy = RefundPolicy(ReturnWindowRepository())

    def execute(self, paid_cents, days_since_delivery, final_sale, defective):
        request = ReturnRequest(Money(paid_cents), days_since_delivery, final_sale, defective)
        return self._policy.refund(request)
""",
    encoding="utf-8",
)
workspace.joinpath("returns.py").write_text(
    '''"""Return-window policy for one shop."""

from application.use_cases import CalculateRefund


_CALCULATE_REFUND = CalculateRefund()


def refund_cents(
    paid_cents: int,
    days_since_delivery: int,
    *,
    final_sale: bool = False,
    defective: bool = False,
) -> int:
    return _CALCULATE_REFUND.execute(
        paid_cents, days_since_delivery, final_sale, defective
    )
''',
    encoding="utf-8",
)
