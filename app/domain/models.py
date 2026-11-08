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
