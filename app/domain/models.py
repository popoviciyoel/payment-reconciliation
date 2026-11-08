"""
Inbound webhook event schemas.

Amounts arrive as strings and are parsed to Decimal — never float — so money
math is exact. Unknown extra fields are ignored rather than rejected, so a
provider adding fields does not break ingestion.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_serializer


class _Event(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_type: str
    timestamp: datetime | None = None
    sandbox_id: str | None = None


class PaymentCreated(_Event):
    model_config = ConfigDict(json_schema_extra={"examples": [{
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
    }]})

    payment_id: str
    reference: str | None = None
    expected_amount: Decimal
    currency: str
    payer_name: str | None = None
    payer_email: str | None = None
    due_date: date | None = None
    description: str | None = None

    @field_serializer("expected_amount")
    def _ser_amount(self, v: Decimal) -> str:
        return str(v)


class TransactionSettled(_Event):
    model_config = ConfigDict(json_schema_extra={"examples": [{
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
    }]})

    transaction_id: str
    reference: str | None = None  # may be missing
    amount: Decimal  # negative for refunds
    currency: str
    payer_name: str | None = None
    payer_account_last_four: str | None = None
    settled_at: datetime | None = None
    bank_reference: str | None = None

    @field_serializer("amount")
    def _ser_amount(self, v: Decimal) -> str:
        return str(v)


PAYMENT_EVENT_TYPE = "payment.created"
TRANSACTION_EVENT_TYPE = "transaction.settled"
