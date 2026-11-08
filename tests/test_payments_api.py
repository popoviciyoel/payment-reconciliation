"""Query API tests: GET /payments and GET /payments/{id}."""

from decimal import Decimal as D

import pytest

from app.domain.status import Tolerance
from app.reconciliation.engine import reconcile


def add_payment(store, pid, ref, amount, payer, currency="USD"):
    store.add_payment({"payment_id": pid, "reference": ref, "expected_amount": amount,
                       "currency": currency, "payer_name": payer, "payer_email": f"billing@{payer.split()[0].lower()}.com"})


def add_txn(store, tid, ref, amount, payer, currency="USD", settled="2026-11-08T10:00:00"):
    store.add_transaction({"transaction_id": tid, "reference": ref, "amount": amount,
                           "currency": currency, "payer_name": payer, "settled_at": settled})


@pytest.fixture
def seeded(store):
    add_payment(store, "p_full", "REF-2026-0001", "100.00", "Acme Corp")
    add_payment(store, "p_part", "REF-2026-0002", "200.00", "Globex Inc")
    add_payment(store, "p_fee", "REF-2026-0003", "1000.00", "Initech LLC")     # paid within 2% tolerance
    add_payment(store, "p_over", "REF-2026-0004", "50.00", "Acme Corp")
    add_payment(store, "p_none", "REF-2026-0005", "75.00", "Umbrella")
    add_payment(store, "p_eur", "REF-2026-0006", "300.00", "Euro GmbH", currency="EUR")
    add_txn(store, "t1", "REF-2026-0001", "100.00", "ACME CORP", settled="2026-11-08T10:00:00")
    add_txn(store, "t2", "REF-2026-0002", "80.00", "Globex", settled="2026-11-08T10:01:00")
    add_txn(store, "t3", "REF-2026-0003", "985.00", "Initech", settled="2026-11-08T10:02:00")
    add_txn(store, "t4", "REF-2026-0004", "60.00", "Acme Corp", settled="2026-11-08T10:03:00")
    add_txn(store, "t5", "REF-2026-0006", "300.00", "Euro GmbH", currency="EUR", settled="2026-11-08T10:04:00")
    add_txn(store, "t_unknown", "UNKNOWN-ABCD", "42.00", "Acme Corp", settled="2026-11-08T10:05:00")
    add_txn(store, "t_orphan", "REF-2026-9999", "10.00", "Nobody", settled="2026-11-08T10:06:00")
    reconcile(store, Tolerance(percent=D("2")))
    return store


def test_list_payments_shape_and_summary(client, seeded):
    r = client.get("/payments")
    assert r.status_code == 200
    body = r.json()
    s = body["summary"]
    assert s["total_payments"] == 6
    assert s["payments_by_status"] == {"PENDING": 1, "PARTIALLY_PAID": 1, "FULLY_PAID": 3, "OVERPAID": 1}
    assert (s["total_transactions"], s["matched_transactions"], s["unmatched_transactions"]) == (7, 5, 2)
    # Never summed across currencies.
    assert s["totals_by_currency"]["USD"] == {
        "expected": "1425.00", "received": "1225.00",
        "outstanding": "195.00",                      # 120 (p_part) + 75 (p_none); p_fee counts as settled
        "unmatched_transaction_amount": "52.00",
    }
    assert s["totals_by_currency"]["EUR"]["received"] == "300.00"
    assert body["pagination"] == {"page": 1, "page_size": 20, "total_items": 6, "total_pages": 1}


def test_unmatched_transactions_include_reason(client, seeded):
    unmatched = client.get("/payments").json()["unmatched_transactions"]
    assert [t["transaction_id"] for t in unmatched] == ["t_unknown", "t_orphan"]
    assert all(t["reason"] for t in unmatched)
    assert "REF-2026-9999" in unmatched[1]["reason"]


def test_payment_details_and_linked_transactions(client, seeded):
    payments = {p["payment_id"]: p for p in client.get("/payments").json()["payments"]}
    part = payments["p_part"]
    assert (part["status"], part["received_amount"], part["outstanding_amount"]) == ("PARTIALLY_PAID", "80.00", "120.00")
    [link] = part["transactions"]
    assert link["transaction_id"] == "t2"
    assert link["match_type"] == "EXACT_REF"
    assert link["allocated_amount"] == link["transaction_amount"] == "80.00"
    assert payments["p_fee"]["status"] == "FULLY_PAID"
    assert payments["p_fee"]["outstanding_amount"] == "0.00"   # within-tolerance shortfall treated as fees
    assert payments["p_over"]["status"] == "OVERPAID"
    assert payments["p_none"]["transactions"] == []


def test_filter_by_status(client, seeded):
    body = client.get("/payments", params={"status": "FULLY_PAID"}).json()
    assert {p["payment_id"] for p in body["payments"]} == {"p_full", "p_fee", "p_eur"}
    assert body["pagination"]["total_items"] == 3
    assert body["summary"]["total_payments"] == 6        # summary ignores filters


def test_invalid_status_is_422(client, seeded):
    assert client.get("/payments", params={"status": "PAID"}).status_code == 422


@pytest.mark.parametrize("term, expected", [
    ("acme", {"p_full", "p_over"}),           # payer name, case-insensitive
    ("REF-2026-0002", {"p_part"}),           # reference
    ("p_none", {"p_none"}),                  # payment id
    ("billing@initech", {"p_fee"}),          # email
    ("zzz", set()),
])
def test_search(client, seeded, term, expected):
    body = client.get("/payments", params={"search": term}).json()
    assert {p["payment_id"] for p in body["payments"]} == expected


def test_search_also_filters_unmatched_transactions(client, seeded):
    body = client.get("/payments", params={"search": "acme"}).json()
    assert [t["transaction_id"] for t in body["unmatched_transactions"]] == ["t_unknown"]


def test_blank_search_is_ignored(client, seeded):
    assert client.get("/payments", params={"search": "   "}).json()["pagination"]["total_items"] == 6


def test_pagination_is_stable_and_complete(client, seeded):
    seen = []
    for page in (1, 2, 3):
        body = client.get("/payments", params={"page": page, "page_size": 2}).json()
        assert body["pagination"]["total_pages"] == 3
        seen += [p["payment_id"] for p in body["payments"]]
    assert len(seen) == len(set(seen)) == 6


def test_page_past_end_is_empty(client, seeded):
    body = client.get("/payments", params={"page": 99}).json()
    assert body["payments"] == [] and body["pagination"]["total_items"] == 6


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}])
def test_invalid_pagination_is_422(client, seeded, params):
    assert client.get("/payments", params=params).status_code == 422


def test_empty_store(client, store):
    body = client.get("/payments").json()
    assert body["payments"] == [] and body["unmatched_transactions"] == []
    assert body["summary"]["total_payments"] == 0
    assert body["summary"]["totals_by_currency"] == {}
    assert body["pagination"]["total_pages"] == 0


def test_get_payment_by_id(client, seeded):
    r = client.get("/payments/p_part")
    assert r.status_code == 200
    body = r.json()
    assert body["payment_id"] == "p_part" and body["status"] == "PARTIALLY_PAID"
    assert [t["transaction_id"] for t in body["transactions"]] == ["t2"]


def test_get_unknown_payment_is_404(client, seeded):
    r = client.get("/payments/does_not_exist")
    assert r.status_code == 404
    assert "does_not_exist" in r.json()["detail"]


def test_multi_payment_split_shows_allocated_vs_full_amount(client, store):
    add_payment(store, "a", "REF-2026-0101", "100.00", "Acme")
    add_payment(store, "b", "REF-2026-0102", "50.00", "Acme")
    add_txn(store, "t", "REF-2026-0101 REF-2026-0102", "150.00", "Acme")
    reconcile(store, Tolerance(percent=D("2")))
    link = client.get("/payments/b").json()["transactions"][0]
    assert (link["allocated_amount"], link["transaction_amount"], link["match_type"]) == ("50.00", "150.00", "MULTI_REF")
