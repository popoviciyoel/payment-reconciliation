"""
Payment status derivation (pure).

Status is always computed from the net amount received (sum of reconciliation
links, refunds included) — never updated incrementally.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    FULLY_PAID = "FULLY_PAID"
    OVERPAID = "OVERPAID"


@dataclass(frozen=True)
class Tolerance:
    """
    How far below a target amount still counts as paid (fee deductions).

    The allowance is the larger of `percent`% of the target and `fixed`.
    Tolerance only applies *below* the target: fees reduce what arrives.
    """

    percent: Decimal = Decimal("0")
    fixed: Decimal = Decimal("0")

    def allowance(self, target: Decimal) -> Decimal:
        return max(target * self.percent / Decimal(100), self.fixed)

    def covers(self, amount: Decimal, target: Decimal) -> bool:
        """True if `amount` is at most `target` and within the allowance below it."""
        return target - self.allowance(target) <= amount <= target


def compute_status(expected: Decimal, received: Decimal, tolerance: Tolerance) -> PaymentStatus:
    if received <= 0:
        return PaymentStatus.PENDING
    if received > expected:
        return PaymentStatus.OVERPAID
    if received >= expected - tolerance.allowance(expected):
        return PaymentStatus.FULLY_PAID
    return PaymentStatus.PARTIALLY_PAID
