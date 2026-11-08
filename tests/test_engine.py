"""Engine tests against real (temporary) storage, including the webhook -> reconcile path."""

from decimal import Decimal as D

import pytest

from app import storage as storage_module
from app.domain.status import Tolerance
from app.reconciliation.engine import reconcile, refresh_all
from tests.conftest import signed_headers

TOL = Tolerance(percent=D("2"))


def add_payment(store, pid, ref, amount, payer="Acme Corporation"):
    store.add_payment({"payment_id": pid, "reference": ref, "expected_amount": amount,
                       "currency": "USD", "payer_name": payer})


def add_txn(store, tid, ref, amount, payer="ACME CORP", settled="2026-11-08T10:00:00"):
    store.add_transaction({"transaction_id": tid, "reference": ref, "amount": amount,
                           "currency": "USD", "payer_name": payer, "settled_at": settled})


def test_partial_then_full(store):
    add_payment(store, "p1", "INV-2026-0001", "1000.00")
    add_txn(store, "t1", "INV-2026-0001", "400.00", settled="2026-11-08T10:00:00")
    reconcile(store, TOL)
    assert store.get_payment("p1")["status"] == "PARTIALLY_PAID"
    assert store.get_payment("p1")["received_amount"] == "400.00"

    add_txn(store, "t2", "INV-2026-0001", "600.00", settled="2026-11-08T11:00:00")
    reconcile(store, TOL)
    p = store.get_payment("p1")
    assert (p["status"], p["received_amount"]) == ("FULLY_PAID", "1000.00")
    assert {l["transaction_id"] for l in store.get_links_for_payment("p1")} == {"t1", "t2"}


def test_transaction_before_payment_is_matched_later(store):
    add_txn(store, "t1", "INV-2026-0002", "50.00")
    reconcile(store, TOL)
    t = store.get_transaction("t1")
    assert t["matched"] is False and "INV-2026-0002" in t["unmatched_reason"]

    add_payment(store, "p1", "INV-2026-0002", "50.00")
    reconcile(store, TOL)
    t = store.get_transaction("t1")
    assert t["matched"] is True and t["matched_payment_ids"] == ["p1"] and t["unmatched_reason"] is None
    assert store.get_payment("p1")["status"] == "FULLY_PAID"


def test_refund_moves_status_back(store):
    add_payment(store, "p1", "INV-2026-0003", "100.00")
    add_txn(store, "t1", "INV-2026-0003", "150.00", settled="2026-11-08T10:00:00")
    reconcile(store, TOL)
    assert store.get_payment("p1")["status"] == "OVERPAID"

    add_txn(store, "t2", "INV-2026-0003", "-50.00", settled="2026-11-08T11:00:00")
    reconcile(store, TOL)
    p = store.get_payment("p1")
    assert (p["status"], p["received_amount"]) == ("FULLY_PAID", "100.00")


def test_reconcile_is_idempotent(store):
    add_payment(store, "p1", "INV-2026-0004", "10.00")
    add_txn(store, "t1", "INV-2026-0004", "10.00")
    first = reconcile(store, TOL)
    second = reconcile(store, TOL)
    assert (first.matched, second.matched, second.evaluated) == (1, 0, 0)
    assert len(store.get_all_reconciliation_links()) == 1


def test_same_run_sees_earlier_allocations(store):
    # Two reference-less transactions for the same open balance: only the first fits.
    add_payment(store, "p1", "INV-2026-0005", "100.00")
    add_txn(store, "t1", None, "100.00", settled="2026-11-08T10:00:00")
    add_txn(store, "t2", None, "100.00", settled="2026-11-08T11:00:00")
    report = reconcile(store, TOL)
    assert report.matched == 1
    assert store.get_transaction("t1")["matched"] and not store.get_transaction("t2")["matched"]


def test_refresh_all_applies_new_tolerance(store):
    add_payment(store, "p1", "INV-2026-0006", "100.00")
    add_txn(store, "t1", "INV-2026-0006", "96.00")
    reconcile(store, TOL)
    assert store.get_payment("p1")["status"] == "PARTIALLY_PAID"   # -4% > 2%
    refresh_all(store, Tolerance(percent=D("5")))
    assert store.get_payment("p1")["status"] == "FULLY_PAID"


def test_webhook_triggers_reconciliation(client, store, payment_payload, transaction_payload):
    client.post("/webhooks/transactions", json=transaction_payload, headers=signed_headers(transaction_payload))
    client.post("/webhooks/payments", json=payment_payload, headers=signed_headers(payment_payload))
    p = store.get_payment("pay_abc123")
    assert (p["status"], p["received_amount"]) == ("PARTIALLY_PAID", "400.00")
    assert store.get_transaction("txn_xyz789")["matched"] is True


def test_duplicate_webhook_does_not_double_count(client, store, payment_payload, transaction_payload):
    client.post("/webhooks/payments", json=payment_payload, headers=signed_headers(payment_payload))
    for _ in range(2):
        client.post("/webhooks/transactions", json=transaction_payload, headers=signed_headers(transaction_payload))
    assert store.get_payment("pay_abc123")["received_amount"] == "400.00"
    assert len(store.get_all_reconciliation_links()) == 1


# ----------------------------------------------------------------- crash safety

def test_failed_record_match_save_leaves_nothing_behind(store, monkeypatch):
    add_payment(store, "p1", "INV-2026-0001", "100.00")
    add_txn(store, "t1", "INV-2026-0001", "100.00")
    with monkeypatch.context() as m:
        m.setattr(storage_module.json, "dump", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
        with pytest.raises(OSError):
            reconcile(store, TOL)

    assert store.get_all_reconciliation_links() == []
    assert store.get_transaction("t1")["matched"] is False
    # A later run succeeds and links exactly once.
    assert reconcile(store, TOL).links_created == 1


def test_linked_but_unflagged_transaction_is_not_linked_twice(store):
    """Simulates a crash between link write and flag write under the old non-atomic code."""
    add_payment(store, "p1", "INV-2026-0001", "100.00")
    add_txn(store, "t1", "INV-2026-0001", "100.00")
    store.add_reconciliation_link({"payment_id": "p1", "transaction_id": "t1",
                                   "match_type": "EXACT_REF", "amount": "100.00"})
    assert store.get_transaction("t1")["matched"] is False

    report = reconcile(store, TOL)

    assert report.links_created == 0
    assert len(store.get_all_reconciliation_links()) == 1
    t = store.get_transaction("t1")
    assert t["matched"] is True and t["matched_payment_ids"] == ["p1"]
    p = store.get_payment("p1")
    assert (p["status"], p["received_amount"]) == ("FULLY_PAID", "100.00")
