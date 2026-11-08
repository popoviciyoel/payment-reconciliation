"""
Transaction -> payment matching (pure, no I/O).

Tiers, tried in order; the first that yields an unambiguous answer wins
(DESIGN.md §5). Currency must always match.

  1. Reference (any format). Only empty and placeholder references
     (`UNKNOWN-*`, `N/A`, ...) count as "no reference".
       - normalized reference equals a payment's    -> EXACT_REF
       - else known payment references found inside
         the text (plus well-formed REF/ORD/PAY/INV-YYYY-NNNN):
           one hit                                  -> EXTRACTED_REF
           several hits                             -> MULTI_REF, spread across balances
         If a well-formed reference in the text has no payment yet, wait out the
         grace period first. After it, allocate across the payments that exist
         only if the amount fits their remaining balances; otherwise leave it
         for review (the missing payment would lose its share for good).
       - reference but no payment has it            -> wait out a grace period (the
         payment may not have arrived yet), then fall through to tier 2, since
         references aren't always informative; link notes record the fallback
  1c. Reference matching no payment, after the grace period: within
       `reference_max_edits` typos of exactly one payment's reference, with the
       payer agreeing and the amount fitting                -> FUZZY_REF
       Otherwise fall through to tier 2.
  2. No usable reference (empty, placeholder, or matching no payment):
       - payment: same normalized payer + amount within tolerance of the
                  remaining balance                                -> NAME_AMOUNT
       - refund:  same normalized payer + magnitude equals an amount linked
                  to that payer's payment and not yet refunded, and
                  no more than it has received                      -> REFUND_NAME_AMOUNT
       Only when exactly one payment qualifies.
  2c. Exact payer, no single payment fits, amount > 0: amount *exactly* equals
       the combined remaining balances of 2-3 of that payer's open payments, and
       exactly one such combination exists              -> MULTI_NAME_AMOUNT
  2b. Only if tier 2 found no candidate at all: payer name *similar*
       (similarity >= threshold, same numbers in both names) + amount
       *exactly* equal to the remaining balance (refund: exactly reverses an
       earlier linked amount not yet refunded). A weaker name signal demands a stronger amount
       signal, so no fee tolerance here.                   -> FUZZY_NAME_AMOUNT
       Only when exactly one payment qualifies.
  3. Otherwise unmatched, with a reason.
"""

from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import combinations
from decimal import Decimal
from enum import Enum

from app.domain.status import PaymentStatus, Tolerance, compute_status, settle
from app.reconciliation.normalize import (
    find_references,
    is_placeholder_reference,
    name_similarity,
    normalize_name,
    normalize_reference,
    numbers_agree,
    reference_distance,
)

DEFAULT_NAME_SIMILARITY_THRESHOLD = 0.85

# Tier 2c: how many payments one reference-less transaction may cover, and the most
# open payments per payer we'll search combinations of (C(30, 3) = 4060).
MAX_PAYMENTS_PER_TRANSACTION = 3
MAX_OPEN_PAYMENTS_FOR_COMBINATIONS = 30

# Tier 1c: typos (edits, adjacent swaps) allowed between a reference and a payment's.
DEFAULT_REFERENCE_MAX_EDITS = 1


def unreversed(amounts) -> tuple[Decimal, ...]:
    """Positive amounts not yet cancelled by a refund of the same size.

    The only definition of refund cancellation: used both for views rebuilt from
    stored links and for views updated during a run, so they always agree.
    """
    left = Counter(a for a in amounts if a > 0)
    for a in amounts:
        if a < 0 and left[-a] > 0:
            left[-a] -= 1
    return tuple(left.elements())


class MatchType(str, Enum):
    EXACT_REF = "EXACT_REF"
    EXTRACTED_REF = "EXTRACTED_REF"
    MULTI_REF = "MULTI_REF"
    NAME_AMOUNT = "NAME_AMOUNT"
    REFUND_NAME_AMOUNT = "REFUND_NAME_AMOUNT"
    FUZZY_NAME_AMOUNT = "FUZZY_NAME_AMOUNT"
    MULTI_NAME_AMOUNT = "MULTI_NAME_AMOUNT"
    FUZZY_REF = "FUZZY_REF"


@dataclass(frozen=True)
class TransactionView:
    transaction_id: str
    reference: str | None
    amount: Decimal
    currency: str
    payer_name: str | None
    # False while a reference that matches no payment is still within its grace
    # period (decided by the caller, which owns the clock).
    reference_fallback_allowed: bool = True


@dataclass(frozen=True)
class PaymentView:
    payment_id: str
    reference: str | None
    expected: Decimal
    currency: str
    payer_name: str | None
    received: Decimal = Decimal("0")
    # Every amount linked to this payment (refunds negative), in link order.
    linked_amounts: tuple[Decimal, ...] = ()

    @property
    def unreversed_amounts(self) -> tuple[Decimal, ...]:
        """Positive linked amounts not yet cancelled by a refund of the same size
        (lets refunds find what they reverse, and only once)."""
        return unreversed(self.linked_amounts)

    def can_refund(self, amount: Decimal) -> bool:
        """Could a refund of `amount` (> 0) reverse an earlier payment here?"""
        return amount in self.unreversed_amounts and amount <= self.received

    @property
    def amount_due(self) -> Decimal:
        """Expected, less refunds that reduced what is owed (see `settle`)."""
        return settle(self.expected, self.linked_amounts).amount_due

    @property
    def remaining(self) -> Decimal:
        return self.amount_due - self.received


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


def match(
    txn: TransactionView,
    payments: list[PaymentView],
    tolerance: Tolerance,
    name_threshold: float = DEFAULT_NAME_SIMILARITY_THRESHOLD,
    reference_max_edits: int = DEFAULT_REFERENCE_MAX_EDITS,
) -> MatchResult:
    if txn.amount == 0:
        return _unmatched("zero amount")

    ref = normalize_reference(txn.reference)
    if is_placeholder_reference(ref):
        return _match_by_name_and_amount(txn, payments, tolerance, name_threshold)

    result = _match_by_reference(txn, ref, payments, tolerance)
    if result is not None:
        return result

    # The reference matched no payment: after the grace period, fall back to
    # payer + amount, and say so.
    context = f"reference {txn.reference.strip()!r} matched no payment"
    if not txn.reference_fallback_allowed:
        return _unmatched(f"{context} yet; waiting for it before matching on payer + amount")
    typo = _match_by_reference_typo(txn, ref, payments, tolerance, name_threshold, reference_max_edits)
    if typo is not None:
        return typo
    fallback = _match_by_name_and_amount(txn, payments, tolerance, name_threshold)
    if fallback.matched:
        return MatchResult(tuple(
            replace(a, notes=f"{context}; {a.notes}") for a in fallback.allocations
        ))
    return _unmatched(f"{context}; {fallback.reason}")


# --------------------------------------------------------------------------- tier 1


def _match_by_reference(
    txn: TransactionView, ref: str, payments: list[PaymentView], tolerance: Tolerance
) -> MatchResult | None:
    """Match on reference; None if no payment has any reference found in the text."""
    by_ref: dict[str, list[PaymentView]] = {}
    for p in payments:
        key = normalize_reference(p.reference)
        if key:
            by_ref.setdefault(key, []).append(p)

    # Exact equality first, whatever the format; search inside the text only if that fails.
    refs = [ref] if ref in by_ref else find_references(ref, by_ref.keys())

    hits: list[PaymentView] = []
    missing: list[str] = []
    for r in refs:
        found = by_ref.get(r, [])
        if len(found) > 1:
            return _unmatched(f"reference {r} is shared by {len(found)} payments")
        if found:
            hits.append(found[0])
        else:
            missing.append(r)

    if not hits:
        return None

    if missing:
        # Some referenced payments exist, others haven't arrived: don't give the
        # whole amount to the ones we know about yet.
        if not txn.reference_fallback_allowed:
            return _unmatched(
                f"reference {', '.join(missing)} matched no payment yet; waiting for it before allocating"
            )
        # After the grace period, a missing well-formed reference still suggests a
        # missing payment. Links are permanent, so allocate only when the amount
        # alone fits what the known payments still owe; otherwise wait for review.
        # Each run re-checks, so a late payment still gets its share.
        known_remaining = sum((max(p.remaining, Decimal("0")) for p in hits), Decimal("0"))
        if txn.amount < 0 or not tolerance.covers(txn.amount, known_remaining):
            return _unmatched(
                f"text names {', '.join(missing)}, which matches no payment, and amount {txn.amount} "
                f"doesn't fit the remaining {known_remaining} of the referenced payments; needs review"
            )

    wrong_currency = [p for p in hits if p.currency != txn.currency]
    if wrong_currency:
        return _unmatched(
            f"currency mismatch: transaction {txn.currency}, payment "
            f"{wrong_currency[0].payment_id} {wrong_currency[0].currency}"
        )

    if len(hits) == 1:
        p = hits[0]
        exact = normalize_reference(txn.reference) == normalize_reference(p.reference)
        result = MatchResult((Allocation(
            payment_id=p.payment_id,
            amount=txn.amount,
            match_type=MatchType.EXACT_REF if exact else MatchType.EXTRACTED_REF,
            notes=_ref_notes(txn, p, exact),
        ),))
    else:
        result = _allocate_multi(txn, hits)

    if missing and result.matched:
        note = f"reference {', '.join(missing)} matched no payment after the grace period"
        result = MatchResult(tuple(replace(a, notes=f"{a.notes}; {note}") for a in result.allocations))
    return result


def _match_by_reference_typo(
    txn: TransactionView,
    ref: str,
    payments: list[PaymentView],
    tolerance: Tolerance,
    name_threshold: float,
    max_edits: int,
) -> MatchResult | None:
    """
    Tier 1c: a reference a few typos away from one payment's. Runs only after the
    grace period (the caller checks), so a reference to a payment that simply
    hasn't arrived isn't redirected to its near-identical neighbour. One digit
    also separates *different* invoices (`...-6981` vs `...-6982`), so the
    distance alone never decides: the payer must agree, the amount must fit, and
    exactly one payment may qualify. None means fall through to payer + amount.
    """
    if max_edits <= 0:
        return None
    # Compare the well-formed reference inside noisy text, or the whole text.
    targets = find_references(ref, ()) or [ref]
    if len(targets) != 1:
        return None
    target = targets[0]

    fits = []
    for p in payments:
        p_ref = normalize_reference(p.reference)
        distance = reference_distance(target, p_ref) if p_ref else None
        if distance is None or distance > max_edits or p.currency != txn.currency:
            continue
        same_payer = normalize_name(txn.payer_name) == normalize_name(p.payer_name) != ""
        if not same_payer and not (
            numbers_agree(txn.payer_name, p.payer_name)
            and name_similarity(txn.payer_name, p.payer_name) >= name_threshold
        ):
            continue
        amount_fits = (
            0 < txn.amount <= p.remaining if txn.amount > 0 else p.can_refund(-txn.amount)
        )
        if amount_fits:
            fits.append((p, distance))

    if len(fits) != 1:
        return None
    p, distance = fits[0]
    return MatchResult((Allocation(
        payment_id=p.payment_id,
        amount=txn.amount,
        match_type=MatchType.FUZZY_REF,
        notes=(f"reference {target!r} is {distance} typo(s) from {p.reference!r} (max {max_edits}); "
               f"payer name similarity {name_similarity(txn.payer_name, p.payer_name)}; "
               f"amount fits {'the remaining balance' if txn.amount > 0 else 'an earlier payment'}"),
    ),))


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
        if share <= 0:
            continue  # nothing left for this payment: never write a zero/negative link
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
    txn: TransactionView, payments: list[PaymentView], tolerance: Tolerance, name_threshold: float
) -> MatchResult:
    payer = normalize_name(txn.payer_name)
    if not payer:
        return _unmatched("no usable reference and no payer name")

    same_currency = [p for p in payments if p.currency == txn.currency]
    same_payer = [p for p in same_currency if normalize_name(p.payer_name) == payer]

    # Tier 2: exact (normalized) payer + amount within fee tolerance.
    if same_payer:
        if txn.amount > 0:
            fits = [p for p in same_payer if p.remaining > 0 and tolerance.covers(txn.amount, p.remaining)]
            match_type = MatchType.NAME_AMOUNT
            note = "no usable reference; payer and amount match remaining balance"
        else:
            fits = [p for p in same_payer if p.can_refund(-txn.amount)]
            match_type = MatchType.REFUND_NAME_AMOUNT
            note = "refund with no usable reference; reverses an equal earlier payment from this payer"

        if len(fits) == 1:
            return MatchResult((Allocation(fits[0].payment_id, txn.amount, match_type, note),))
        if len(fits) > 1:
            return _unmatched(f"no usable reference; ambiguous between {', '.join(p.payment_id for p in fits)}")
        if txn.amount > 0:
            combined = _match_combined_balances(txn, same_payer, tolerance)
            if combined is not None:
                return combined
        # Exact-name candidates exist but none fit: a fuzzy pass can't do better
        # (it requires an exact amount over *other* payers), so stop here.
        return _unmatched(
            f"no usable reference; no open payment from {txn.payer_name!r} fits amount {txn.amount}"
        )

    # Tier 2b: similar payer name + exact amount.
    return _match_by_similar_name(txn, same_currency, name_threshold)


_OPEN_STATUSES = frozenset({PaymentStatus.PENDING, PaymentStatus.PARTIALLY_PAID})


def _match_combined_balances(
    txn: TransactionView, same_payer: list[PaymentView], tolerance: Tolerance
) -> MatchResult | None:
    """
    Tier 2c: one reference-less transaction paying several invoices. Requires an
    exact sum of remaining balances and a unique combination. Returns None when
    there is no combination (caller reports unmatched), an unmatched result when
    it's ambiguous or too large to search.

    Candidates are payments still open *by status*: a FULLY_PAID payment's
    shortfall within the fee tolerance is fees, not a balance to fill. Each part
    of a sum of positive balances is smaller than the total, so larger balances
    are pruned before the search.
    """
    open_payments = sorted(
        (
            p for p in same_payer
            if compute_status(p.amount_due, p.received, tolerance) in _OPEN_STATUSES
            and 0 < p.remaining < txn.amount
        ),
        key=lambda p: p.payment_id,
    )
    if len(open_payments) < 2:
        return None
    if len(open_payments) > MAX_OPEN_PAYMENTS_FOR_COMBINATIONS:
        return _unmatched(
            f"no usable reference; {len(open_payments)} open payments from {txn.payer_name!r} "
            f"is too many to search for a combined amount"
        )

    combos = []
    for size in range(2, MAX_PAYMENTS_PER_TRANSACTION + 1):
        for combo in combinations(open_payments, size):
            if sum((p.remaining for p in combo), Decimal("0")) == txn.amount:
                combos.append(combo)
                if len(combos) > 1:
                    break  # a second combination already makes it ambiguous
        if len(combos) > 1:
            break
    if not combos:
        return None
    if len(combos) > 1:
        options = " | ".join(", ".join(p.payment_id for p in c) for c in combos)
        return _unmatched(f"no usable reference; amount equals several combinations of open payments: {options}")

    combo = combos[0]
    note = (f"no usable reference; amount exactly equals the combined remaining balances of "
            f"{len(combo)} payments from this payer")
    return MatchResult(tuple(
        Allocation(p.payment_id, p.remaining, MatchType.MULTI_NAME_AMOUNT, note) for p in combo
    ))


def _match_by_similar_name(
    txn: TransactionView, candidates: list[PaymentView], name_threshold: float
) -> MatchResult:
    similar = [
        (p, score) for p in candidates
        if numbers_agree(txn.payer_name, p.payer_name)
        and (score := name_similarity(txn.payer_name, p.payer_name)) >= name_threshold
    ]
    if not similar:
        return _unmatched(f"no usable reference; no {txn.currency} payments from {txn.payer_name!r} or a similar name")

    if txn.amount > 0:
        fits = [(p, s) for p, s in similar if p.remaining > 0 and txn.amount == p.remaining]
        what = "amount exactly matches remaining balance"
    else:
        fits = [(p, s) for p, s in similar if p.can_refund(-txn.amount)]
        what = "refund exactly reverses an earlier linked amount not yet refunded"

    if not fits:
        names = ", ".join(sorted({repr(p.payer_name) for p, _ in similar}))
        return _unmatched(f"no usable reference; similar payer {names} but no payment with exactly {txn.amount}")
    if len(fits) > 1:
        return _unmatched(f"no usable reference; ambiguous between {', '.join(p.payment_id for p, _ in fits)}")

    p, score = fits[0]
    return MatchResult((Allocation(
        payment_id=p.payment_id,
        amount=txn.amount,
        match_type=MatchType.FUZZY_NAME_AMOUNT,
        notes=f"no usable reference; payer {txn.payer_name!r} ~ {p.payer_name!r} "
              f"(similarity {score} >= {name_threshold}); {what}",
    ),))
