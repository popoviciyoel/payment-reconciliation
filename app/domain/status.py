"""
Payment status derivation (pure).

Status is always computed from the reconciliation links (refunds included) —
never updated incrementally. A refund reduces what the payer owes rather than
reopening a balance (DESIGN.md Q1).
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


@dataclass(frozen=True)
class Settlement:
    amount_due: Decimal  # expected, less refunds that reduced what the payer owes
    received: Decimal    # net: payments less refunds


def settle(expected: Decimal, linked_amounts) -> Settlement:
    """
    Apply linked amounts (refunds negative) in link order.

    A refund first gives back any overpayment; the rest reduces the amount due
    (a credit, discount or cancelled item), so it never reopens a balance:
      paid 120 of 100, refund 20  -> due 100, received 100 (settled)
      paid 100 of 100, refund 30  -> due 70,  received 70  (settled)
      paid 50 of 100,  refund 10  -> due 90,  received 40  (50 still outstanding)
    """
    due, received = expected, Decimal("0")
    for amount in linked_amounts:
        if amount >= 0:
            received += amount
            continue
        refund = -amount
        returned_overpayment = min(refund, max(received - due, Decimal("0")))
        due -= refund - returned_overpayment
        received -= refund
    return Settlement(amount_due=due, received=received)


def compute_status(amount_due: Decimal, received: Decimal, tolerance: Tolerance) -> PaymentStatus:
    """Status from net received against the amount due (see `settle`)."""
    if received > amount_due:
        return PaymentStatus.OVERPAID
    if received >= amount_due - tolerance.allowance(amount_due):
        return PaymentStatus.FULLY_PAID  # also a fully refunded payment: nothing is owed
    if received <= 0:
        return PaymentStatus.PENDING
    return PaymentStatus.PARTIALLY_PAID
