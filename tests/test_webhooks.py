import json

from app.config import settings
from tests.conftest import signed_headers


def post(client, path, payload, headers=None):
    # Send with non-canonical formatting to prove we don't sign the raw body.
    body = json.dumps(payload, indent=2)
    h = {"Content-Type": "application/json", **(headers if headers is not None else signed_headers(payload))}
    return client.post(path, content=body, headers=h)


def test_payment_is_stored(client, store, payment_payload):
    r = post(client, "/webhooks/payments", payment_payload)
    assert r.status_code == 200
    assert r.json() == {"status": "accepted", "id": "pay_abc123"}
    stored = store.get_payment("pay_abc123")
    assert stored["expected_amount"] == "1000.00"
    assert stored["status"] == "PENDING"


def test_transaction_is_stored(client, store, transaction_payload):
    r = post(client, "/webhooks/transactions", transaction_payload)
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert store.get_transaction("txn_xyz789")["amount"] == "400.00"


def test_null_reference_and_negative_amount_accepted(client, store, transaction_payload):
    transaction_payload.update(reference=None, amount="-50.00")
    r = post(client, "/webhooks/transactions", transaction_payload)
    assert r.status_code == 200
    stored = store.get_transaction("txn_xyz789")
    assert stored["reference"] is None and stored["amount"] == "-50.00"


def test_duplicate_payment_is_acknowledged_not_restored(client, store, payment_payload):
    post(client, "/webhooks/payments", payment_payload)
    payment_payload["expected_amount"] = "9999.00"  # conflicting redelivery
    r = post(client, "/webhooks/payments", payment_payload)
    assert r.status_code == 200
    assert r.json()["status"] == "duplicate"
    assert store.get_payment("pay_abc123")["expected_amount"] == "1000.00"
    assert len(store.get_all_payments()) == 1


def test_duplicate_transaction(client, store, transaction_payload):
    post(client, "/webhooks/transactions", transaction_payload)
    r = post(client, "/webhooks/transactions", transaction_payload)
    assert r.json()["status"] == "duplicate"
    assert len(store.get_all_transactions()) == 1


def test_invalid_signature_rejected(client, store, payment_payload):
    r = post(client, "/webhooks/payments", payment_payload, {"X-Webhook-Signature": "sha256=deadbeef"})
    assert r.status_code == 401
    assert store.get_all_payments() == []


def test_missing_signature_rejected(client, store, payment_payload):
    r = post(client, "/webhooks/payments", payment_payload, {})
    assert r.status_code == 401


def test_malformed_json_rejected(client, store):
    r = client.post("/webhooks/payments", content=b"{not json", headers={"X-Webhook-Signature": "sha256=x"})
    assert r.status_code == 400


def test_schema_invalid_but_signed_returns_422(client, store, payment_payload):
    payment_payload["expected_amount"] = "not-a-number"
    r = post(client, "/webhooks/payments", payment_payload)
    assert r.status_code == 422
    assert store.get_all_payments() == []


def test_wrong_event_type_is_ignored(client, store, transaction_payload):
    r = post(client, "/webhooks/payments", transaction_payload)  # txn sent to payments endpoint
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"
    assert store.get_all_payments() == []


def test_missing_secret_fails_closed(client, store, payment_payload, monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", "")
    r = post(client, "/webhooks/payments", payment_payload)
    assert r.status_code == 500
