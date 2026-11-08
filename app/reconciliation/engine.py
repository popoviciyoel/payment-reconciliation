"""
Reconciliation orchestration: reads storage, runs the pure matcher, writes links.

Invariants:
  - Reconciliation links are the single source of truth. A payment's
    `received_amount` and `status` are a projection recomputed from its links.
    A transaction with any link is matched, whatever its `matched` flag says.
  - Each match (links + transaction flag + payment projections) is written in
    one atomic storage operation.
  - Links are immutable once written.
  - Runs are serialized by one lock, so two webhooks can't race on a payment.

Every run re-evaluates *all* unmatched transactions (in settlement order), so a
transaction that arrived before its payment is picked up when the payment lands.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Lock

from app.config import settings
from app.domain.status import Tolerance, compute_status
from app.reconciliation.matcher import PaymentView, TransactionView, match
from app.storage import JsonStorage

logger = logging.getLogger(__name__)

_reconcile_lock = Lock()

# The reference grace period only counts time this process was up and able to
# receive webhooks: after an outage, the payment webhook may still be in retry.
_SERVICE_STARTED_AT = datetime.now()


def default_tolerance() -> Tolerance:
    return Tolerance(percent=settings.fee_tolerance_percent, fixed=settings.fee_tolerance_fixed)


@dataclass(frozen=True)
class ReconcileReport:
    evaluated: int
    matched: int
    links_created: int


def _dec(value) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal("0")


def _links_by_payment(storage: JsonStorage) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for link in storage.get_all_reconciliation_links():
        grouped[link["payment_id"]].append(link)
    return grouped


def _payment_views(storage: JsonStorage) -> dict[str, PaymentView]:
    links = _links_by_payment(storage)
    views = {}
    for p in storage.get_all_payments():
        amounts = [_dec(l["amount"]) for l in links.get(p["payment_id"], [])]
        views[p["payment_id"]] = PaymentView(
            payment_id=p["payment_id"],
            reference=p.get("reference"),
            expected=_dec(p.get("expected_amount")),
            currency=p.get("currency") or "USD",
            payer_name=p.get("payer_name"),
            received=sum(amounts, Decimal("0")),
            linked_amounts=tuple(amounts),
        )
    return views


def _grace_elapsed(t: dict, now: datetime, grace: timedelta, started_at: datetime) -> bool:
    """
    Has this transaction waited out the reference grace period while we were up?
    Counted from receipt or service start, whichever is later; with no usable
    receipt time, from service start (still finite, so nothing blocks forever).
    """
    try:
        received = datetime.fromisoformat(t["created_at"])
    except (KeyError, TypeError, ValueError):
        received = started_at
    return now - max(received, started_at) >= grace


def _transaction_view(t: dict, now: datetime, grace: timedelta, started_at: datetime) -> TransactionView:
    return TransactionView(
        transaction_id=t["transaction_id"],
        reference=t.get("reference"),
        amount=_dec(t.get("amount")),
        currency=t.get("currency") or "USD",
        payer_name=t.get("payer_name"),
        reference_fallback_allowed=_grace_elapsed(t, now, grace, started_at),
    )


def _projection(view: PaymentView, tolerance: Tolerance) -> dict:
    return {
        "received_amount": str(view.received),
        "status": compute_status(view.expected, view.received, tolerance).value,
    }


def _repair_flag(storage: JsonStorage, transaction_id: str) -> list[str]:
    payment_ids = list(dict.fromkeys(l["payment_id"] for l in storage.get_links_for_transaction(transaction_id)))
    storage.update_transaction(transaction_id, {
        "matched": True,
        "matched_to_payment_id": payment_ids[0],
        "matched_payment_ids": payment_ids,
        "unmatched_reason": None,
    })
    logger.warning("Transaction %s was linked but not flagged; repaired flag", transaction_id)
    return payment_ids


def _refresh_payments(storage: JsonStorage, payment_ids, tolerance: Tolerance) -> None:
    """Recompute received_amount/status from links; write only if changed."""
    views = _payment_views(storage)
    for pid in payment_ids:
        view = views.get(pid)
        if view is None:
            continue
        projection = _projection(view, tolerance)
        current = storage.get_payment(pid) or {}
        if any(current.get(k) != v for k, v in projection.items()):
            storage.update_payment(pid, projection)


def reconcile(
    storage: JsonStorage,
    tolerance: Tolerance | None = None,
    now: datetime | None = None,
    started_at: datetime | None = None,
) -> ReconcileReport:
    """Match every unmatched transaction that can now be matched."""
    tolerance = tolerance or default_tolerance()
    # Storage timestamps are naive local time (datetime.now()); compare like with like.
    now = now or datetime.now()
    started_at = started_at or _SERVICE_STARTED_AT
    grace = timedelta(minutes=settings.reference_grace_minutes)
    with _reconcile_lock:
        payments = _payment_views(storage)
        linked_txn_ids = {l["transaction_id"] for l in storage.get_all_reconciliation_links()}
        unmatched = sorted(
            storage.get_unmatched_transactions(),
            key=lambda t: (t.get("settled_at") or "", t["transaction_id"]),
        )

        matched = links_created = 0
        for t in unmatched:
            if t["transaction_id"] in linked_txn_ids:
                # Already linked but flag not set (e.g. data written before writes
                # were atomic): never link twice; just repair the flag.
                _refresh_payments(storage, _repair_flag(storage, t["transaction_id"]), tolerance)
                continue

            result = match(_transaction_view(t, now, grace, started_at), list(payments.values()), tolerance, settings.fuzzy_name_threshold)

            if not result.matched:
                if t.get("unmatched_reason") != result.reason:
                    storage.update_transaction(t["transaction_id"], {"unmatched_reason": result.reason})
                    logger.info("Transaction %s unmatched: %s", t["transaction_id"], result.reason)
                continue

            # Compute the post-match views first, then persist everything at once.
            updated = dict(payments)
            for a in result.allocations:
                p = updated[a.payment_id]
                updated[a.payment_id] = replace(
                    p,
                    received=p.received + a.amount,
                    linked_amounts=p.linked_amounts + (a.amount,),
                )
            payment_ids = list(dict.fromkeys(a.payment_id for a in result.allocations))
            storage.record_match(
                t["transaction_id"],
                links=[{
                    "payment_id": a.payment_id,
                    "match_type": a.match_type.value,
                    "amount": str(a.amount),
                    "notes": a.notes,
                } for a in result.allocations],
                transaction_updates={
                    "matched": True,
                    "matched_to_payment_id": payment_ids[0],
                    "matched_payment_ids": payment_ids,
                    "unmatched_reason": None,
                },
                payment_updates={pid: _projection(updated[pid], tolerance) for pid in payment_ids},
            )
            # Only advance in-memory state once the write succeeded, so later
            # transactions in this run see these allocations.
            payments = updated
            linked_txn_ids.add(t["transaction_id"])
            links_created += len(result.allocations)
            matched += 1
            logger.info(
                "Matched transaction %s -> %s (%s)",
                t["transaction_id"], ", ".join(payment_ids), result.allocations[0].match_type.value,
            )

        return ReconcileReport(evaluated=len(unmatched), matched=matched, links_created=links_created)


def refresh_all(storage: JsonStorage, tolerance: Tolerance | None = None) -> None:
    """Recompute every payment's projection (e.g. at startup, after a tolerance change)."""
    tolerance = tolerance or default_tolerance()
    with _reconcile_lock:
        _refresh_payments(storage, [p["payment_id"] for p in storage.get_all_payments()], tolerance)
