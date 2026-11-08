import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_storage
from app.config import settings
from app.core import signature
from app.core.rate_limit import reset_limiters
from app.core.signature import compute_signature
from app.main import app
from app.services import ingest
from app.storage import JsonStorage

TEST_SECRET = "whsec_test_secret"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret", TEST_SECRET)
    monkeypatch.setattr(ingest, "RAW_EVENT_LOG", tmp_path / "raw_events.jsonl")
    monkeypatch.setattr(signature, "REJECTED_EVENT_LOG", tmp_path / "rejected_events.jsonl")
    s = JsonStorage(str(tmp_path / "storage.json"))
    app.dependency_overrides[get_storage] = lambda: s
    yield s
    app.dependency_overrides.clear()


@pytest.fixture
def client(store):
    return TestClient(app)


def signed_headers(payload: dict, secret: str = TEST_SECRET) -> dict:
    return {"X-Webhook-Signature": compute_signature(payload, secret)}


@pytest.fixture
def payment_payload():
    return {
        "event_type": "payment.created",
        "payment_id": "pay_abc123",
        "reference": "INV-2024-1234",
        "expected_amount": "1000.00",
        "currency": "USD",
        "payer_name": "Acme Corporation",
        "payer_email": "billing@acme.com",
        "due_date": "2024-01-20",
        "description": "Invoice payment",
        "timestamp": "2024-01-15T10:00:00Z",
        "sandbox_id": "sandbox_xyz",
    }


@pytest.fixture
def transaction_payload():
    return {
        "event_type": "transaction.settled",
        "transaction_id": "txn_xyz789",
        "reference": "INV-2024-1234",
        "amount": "400.00",
        "currency": "USD",
        "payer_name": "ACME CORP",
        "payer_account_last_four": "4242",
        "settled_at": "2024-01-16T14:30:00Z",
        "bank_reference": "BANK-ABC123",
        "timestamp": "2024-01-16T14:30:00Z",
        "sandbox_id": "sandbox_xyz",
    }


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Rate-limit buckets are process-wide; isolate each test."""
    reset_limiters()
    yield
    reset_limiters()
