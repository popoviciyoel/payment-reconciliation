"""
Read-side queries: assemble API views from storage.

Reads use the cached payment projection (status / received_amount) and never
recompute it (DESIGN.md §5 "Status: recalculate vs store").
"""

from collections import Counter, defaultdict
from decimal import Decimal
from math import ceil

from app.domain.schemas import (
    CurrencyTotals,
    LinkedTransaction,
    Pagination,
    Payment,
    PaymentList,
    Summary,
    UnmatchedTransaction,
)
from app.domain.status import PaymentStatus
from app.storage import JsonStorage

ZERO = Decimal("0.00")  # two places, matching provider amounts


def _dec(value) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else ZERO


_SETTLED = {PaymentStatus.FULLY_PAID.value, PaymentStatus.OVERPAID.value}


def _outstanding(record: dict) -> Decimal:
    """Balance still due. Zero once settled: a within-tolerance shortfall is treated as fees."""
    if (record.get("status") or PaymentStatus.PENDING.value) in _SETTLED:
        return ZERO
    return max(_dec(record.get("expected_amount")) - _dec(record.get("received_amount")), ZERO)


def _matches_search(term: str, *fields) -> bool:
    return any(term in str(f).casefold() for f in fields if f)


def _linked_transactions(links: list[dict], transactions: dict[str, dict]) -> list[LinkedTransaction]:
    out = []
    for link in sorted(links, key=lambda l: l.get("created_at") or ""):
        txn = transactions.get(link["transaction_id"], {})
        out.append(LinkedTransaction(
            transaction_id=link["transaction_id"],
            allocated_amount=_dec(link.get("amount")),
            transaction_amount=_dec(txn.get("amount")),
            match_type=link.get("match_type") or "UNKNOWN",
            match_notes=link.get("notes"),
            reference=txn.get("reference"),
            payer_name=txn.get("payer_name"),
            settled_at=txn.get("settled_at"),
            linked_at=link.get("created_at"),
        ))
    return out


def _to_payment(record: dict, links: list[dict], transactions: dict[str, dict]) -> Payment:
    expected = _dec(record.get("expected_amount"))
    received = _dec(record.get("received_amount"))
    return Payment(
        payment_id=record["payment_id"],
        reference=record.get("reference"),
        status=record.get("status") or PaymentStatus.PENDING,
        expected_amount=expected,
        received_amount=received,
        outstanding_amount=_outstanding(record),
        currency=record.get("currency") or "USD",
        payer_name=record.get("payer_name"),
        payer_email=record.get("payer_email"),
        due_date=record.get("due_date"),
        description=record.get("description"),
        created_at=record.get("created_at"),
        updated_at=record.get("updated_at"),
        transactions=_linked_transactions(links, transactions),
    )


def _links_by_payment(storage: JsonStorage) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for link in storage.get_all_reconciliation_links():
        grouped[link["payment_id"]].append(link)
    return grouped


def _summary(payments: list[dict], transactions: list[dict], linked_txn_ids: set[str]) -> Summary:
    by_status = Counter(p.get("status") or PaymentStatus.PENDING.value for p in payments)
    totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"expected": ZERO, "received": ZERO, "outstanding": ZERO, "unmatched_transaction_amount": ZERO}
    )
    for p in payments:
        t = totals[p.get("currency") or "USD"]
        expected, received = _dec(p.get("expected_amount")), _dec(p.get("received_amount"))
        t["expected"] += expected
        t["received"] += received
        t["outstanding"] += _outstanding(p)

    unmatched = [t for t in transactions if t["transaction_id"] not in linked_txn_ids]
    for t in unmatched:
        totals[t.get("currency") or "USD"]["unmatched_transaction_amount"] += _dec(t.get("amount"))

    return Summary(
        total_payments=len(payments),
        payments_by_status={s: by_status.get(s.value, 0) for s in PaymentStatus},
        total_transactions=len(transactions),
        matched_transactions=len(transactions) - len(unmatched),
        unmatched_transactions=len(unmatched),
        totals_by_currency={cur: CurrencyTotals(**vals) for cur, vals in sorted(totals.items())},
    )


def list_payments(
    storage: JsonStorage,
    *,
    status: PaymentStatus | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PaymentList:
    all_payments = storage.get_all_payments()
    all_transactions = storage.get_all_transactions()
    txns_by_id = {t["transaction_id"]: t for t in all_transactions}
    links = _links_by_payment(storage)
    # A transaction with any link is matched (links are the source of truth).
    linked_txn_ids = {l["transaction_id"] for ls in links.values() for l in ls}
    term = search.strip().casefold() if search and search.strip() else None

    filtered = [
        p for p in all_payments
        if (status is None or p.get("status") == status.value)
        and (term is None or _matches_search(term, p.get("payment_id"), p.get("reference"),
                                              p.get("payer_name"), p.get("payer_email")))
    ]
    # Newest first; payment_id breaks ties so paging is stable.
    filtered.sort(key=lambda p: (p.get("created_at") or "", p["payment_id"]), reverse=True)

    total = len(filtered)
    start = (page - 1) * page_size
    page_items = filtered[start:start + page_size]

    unmatched = [
        t for t in all_transactions
        if t["transaction_id"] not in linked_txn_ids
        and (term is None or _matches_search(term, t.get("transaction_id"), t.get("reference"), t.get("payer_name")))
    ]
    unmatched.sort(key=lambda t: (t.get("settled_at") or "", t["transaction_id"]))

    return PaymentList(
        summary=_summary(all_payments, all_transactions, linked_txn_ids),
        pagination=Pagination(
            page=page,
            page_size=page_size,
            total_items=total,
            total_pages=ceil(total / page_size) if total else 0,
        ),
        payments=[_to_payment(p, links.get(p["payment_id"], []), txns_by_id) for p in page_items],
        unmatched_transactions=[
            UnmatchedTransaction(
                transaction_id=t["transaction_id"],
                reference=t.get("reference"),
                amount=_dec(t.get("amount")),
                currency=t.get("currency") or "USD",
                payer_name=t.get("payer_name"),
                settled_at=t.get("settled_at"),
                reason=t.get("unmatched_reason"),
            )
            for t in unmatched
        ],
    )


def get_payment(storage: JsonStorage, payment_id: str) -> Payment | None:
    record = storage.get_payment(payment_id)
    if record is None:
        return None
    txns_by_id = {t["transaction_id"]: t for t in storage.get_all_transactions()}
    return _to_payment(record, storage.get_links_for_payment(payment_id), txns_by_id)
