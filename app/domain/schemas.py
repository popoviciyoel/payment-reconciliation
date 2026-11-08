"""
Response schemas for the query API.

Amounts are Decimals serialized as JSON strings (same convention as the
provider's payloads) so no precision is lost.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.domain.status import PaymentStatus


class LinkedTransaction(BaseModel):
    transaction_id: str
    allocated_amount: Decimal = Field(description="Portion of the transaction applied to this payment")
    transaction_amount: Decimal = Field(description="Full transaction amount (differs from allocated for multi-payment splits)")
    match_type: str = Field(
        description="Why it matched: EXACT_REF, EXTRACTED_REF, MULTI_REF, NAME_AMOUNT, "
                    "REFUND_NAME_AMOUNT, FUZZY_NAME_AMOUNT, MULTI_NAME_AMOUNT"
    )
    match_notes: str | None = None
    reference: str | None = None
    payer_name: str | None = None
    settled_at: datetime | None = None
    linked_at: datetime | None = None


class Payment(BaseModel):
    payment_id: str
    reference: str | None = None
    status: PaymentStatus
    expected_amount: Decimal
    received_amount: Decimal
    outstanding_amount: Decimal = Field(
        description="Balance still due: expected - received, or 0 once FULLY_PAID/OVERPAID "
                    "(a shortfall within the fee tolerance is treated as fees)"
    )
    currency: str
    payer_name: str | None = None
    payer_email: str | None = None
    due_date: date | None = None
    description: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    transactions: list[LinkedTransaction]


class UnmatchedTransaction(BaseModel):
    transaction_id: str
    reference: str | None = None
    amount: Decimal
    currency: str
    payer_name: str | None = None
    settled_at: datetime | None = None
    reason: str | None = Field(None, description="Why the matcher could not link it")


class CurrencyTotals(BaseModel):
    expected: Decimal
    received: Decimal
    outstanding: Decimal
    unmatched_transaction_amount: Decimal


class Summary(BaseModel):
    total_payments: int
    payments_by_status: dict[PaymentStatus, int]
    total_transactions: int
    matched_transactions: int
    unmatched_transactions: int
    totals_by_currency: dict[str, CurrencyTotals] = Field(
        description="Amounts are never summed across currencies"
    )


class Pagination(BaseModel):
    page: int
    page_size: int
    total_items: int = Field(description="Payments matching the filters")
    total_pages: int


class PaymentList(BaseModel):
    summary: Summary = Field(description="Computed over all data, independent of filters")
    pagination: Pagination
    payments: list[Payment]
    unmatched_transactions: list[UnmatchedTransaction] = Field(
        description="All unmatched transactions (the `search` filter applies; `status` does not)"
    )
