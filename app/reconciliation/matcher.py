"""
Transaction -> payment matching (pure, no I/O).

Tiers, tried in order; the first that yields an unambiguous answer wins
(DESIGN.md §5). Currency must always match.

  1. Reference: extract well-formed references from the transaction.
       - one hit           -> EXACT_REF / EXTRACTED_REF, full amount
       - several hits      -> MULTI_REF, amount spread across remaining balances
       - refs but no hit   -> unmatched (the payment may not have arrived yet;
                              we don't fall back to name matching, which could
                              grab the wrong invoice in the meantime)
  2. No usable reference (empty, `UNKNOWN-*`):
       - payment: same normalized payer + amount within tolerance of the
                  remaining balance                                -> NAME_AMOUNT
       - refund:  same normalized payer + magnitude equals a payment
                  previously linked to that payer's payment         -> REFUND_NAME_AMOUNT
       Only when exactly one payment qualifies.
  3. Otherwise unmatched, with a reason.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from app.domain.status import Tolerance
from app.reconciliation.normalize import (
    extract_references,
    name_similarity,
    normalize_name,
    normalize_reference,
)


class MatchType(str, Enum):
    EXACT_REF = "EXACT_REF"
    EXTRACTED_REF = "EXTRACTED_REF"
    MULTI_REF = "MULTI_REF"
    NAME_AMOUNT = "NAME_AMOUNT"
    REFUND_NAME_AMOUNT = "REFUND_NAME_AMOUNT"


@dataclass(frozen=True)
class TransactionView:
    transaction_id: str
    reference: str | None
    amount: Decimal
    currency: str
    payer_name: str | None


@dataclass(frozen=True)
class PaymentView:
    payment_id: str
    reference: str | None
    expected: Decimal
    currency: str
    payer_name: str | None
    received: Decimal = Decimal("0")
    # Positive amounts already linked to this payment (lets refunds find what they reverse).
    linked_amounts: tuple[Decimal, ...] = ()

    @property
    def remaining(self) -> Decimal:
        return self.expected - self.received


@dataclass(frozen=True)
class Allocation:
    payment_id: str
    amount: Decimal
    match_type: MatchType
    notes: str


@dataclass(frozen=True)
class MatchResult:
    allocations: tuple[Allocation, ...] = field(default_factory=tuple)
    reason: str = ""

    @property
    def matched(self) -> bool:
        return bool(self.allocations)


def _unmatched(reason: str) -> MatchResult:
    return MatchResult((), reason)


def match(txn: TransactionView, payments: list[PaymentView], tolerance: Tolerance) -> MatchResult:
    if txn.amount == 0:
        return _unmatched("zero amount")

    refs = extract_references(txn.reference)
    if refs:
        return _match_by_reference(txn, refs, payments)
    return _match_by_name_and_amount(txn, payments, tolerance)


# --------------------------------------------------------------------------- tier 1


def _match_by_reference(txn: TransactionView, refs: list[str], payments: list[PaymentView]) -> MatchResult:
    by_ref: dict[str, list[PaymentView]] = {}
    for p in payments:
        key = normalize_reference(p.reference)
        if key:
            by_ref.setdefault(key, []).append(p)

    hits: list[PaymentView] = []
    for ref in refs:
        found = by_ref.get(ref, [])
        if len(found) > 1:
            return _unmatched(f"reference {ref} is shared by {len(found)} payments")
        if found:
            hits.append(found[0])

    if not hits:
        return _unmatched(f"no payment with reference {', '.join(refs)} (yet)")

    wrong_currency = [p for p in hits if p.currency != txn.currency]
    if wrong_currency:
        return _unmatched(
            f"currency mismatch: transaction {txn.currency}, payment "
            f"{wrong_currency[0].payment_id} {wrong_currency[0].currency}"
        )

    if len(hits) == 1:
        p = hits[0]
        exact = normalize_reference(txn.reference) == normalize_reference(p.reference)
        return MatchResult((Allocation(
            payment_id=p.payment_id,
            amount=txn.amount,
            match_type=MatchType.EXACT_REF if exact else MatchType.EXTRACTED_REF,
            notes=_ref_notes(txn, p, exact),
        ),))

    return _allocate_multi(txn, hits)


def _ref_notes(txn: TransactionView, p: PaymentView, exact: bool) -> str:
    parts = [] if exact else [f"reference {txn.reference!r} cleaned to {p.reference!r}"]
    parts.append(f"payer name similarity {name_similarity(txn.payer_name, p.payer_name)}")
    return "; ".join(parts)


def _allocate_multi(txn: TransactionView, hits: list[PaymentView]) -> MatchResult:
    """Fill each payment's remaining balance in reference order; remainder goes to the last."""
    if txn.amount < 0:
        return _unmatched("refund references several payments; cannot tell which to reverse")

    allocations = []
    left = txn.amount
    for i, p in enumerate(hits):
        last = i == len(hits) - 1
        share = left if last else min(left, max(p.remaining, Decimal("0")))
        if share <= 0 and not last:
            continue
        allocations.append(Allocation(
            payment_id=p.payment_id,
            amount=share,
            match_type=MatchType.MULTI_REF,
            notes=f"split across {len(hits)} referenced payments",
        ))
        left -= share
    return MatchResult(tuple(allocations))


# --------------------------------------------------------------------------- tier 2


def _match_by_name_and_amount(
    txn: TransactionView, payments: list[PaymentView], tolerance: Tolerance
) -> MatchResult:
    payer = normalize_name(txn.payer_name)
    if not payer:
        return _unmatched("no usable reference and no payer name")

    same_payer = [
        p for p in payments
        if p.currency == txn.currency and normalize_name(p.payer_name) == payer
    ]
    if not same_payer:
        return _unmatched(f"no usable reference; no {txn.currency} payments from {txn.payer_name!r}")

    if txn.amount > 0:
        fits = [p for p in same_payer if p.remaining > 0 and tolerance.covers(txn.amount, p.remaining)]
        match_type = MatchType.NAME_AMOUNT
        note = "no usable reference; payer and amount match remaining balance"
    else:
        magnitude = -txn.amount
        fits = [p for p in same_payer if magnitude in p.linked_amounts]
        match_type = MatchType.REFUND_NAME_AMOUNT
        note = "refund with no usable reference; reverses an equal earlier payment from this payer"

    if not fits:
        return _unmatched(
            f"no usable reference; no open payment from {txn.payer_name!r} fits amount {txn.amount}"
        )
    if len(fits) > 1:
        ids = ", ".join(p.payment_id for p in fits)
        return _unmatched(f"no usable reference; ambiguous between {ids}")

    return MatchResult((Allocation(fits[0].payment_id, txn.amount, match_type, note),))
