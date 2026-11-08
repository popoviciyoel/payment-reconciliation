"""
Response schemas for the query API.

Amounts are Decimals serialized as JSON strings (same convention as the
provider's payloads) so no precision is lost.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.status import PaymentStatus


# Examples for the OpenAPI docs: a payment settled 1.5% short (within the fee
# tolerance), and a transaction left unmatched for review.
_PAYMENT_EXAMPLE = {
    "payment_id": "pay_8f2c1a",
    "reference": "ORD-2026-2811",
    "status": "FULLY_PAID",
    "expected_amount": "4153.15",
    "amount_due": "4153.15",
    "received_amount": "4090.85",
    "outstanding_amount": "0",
    "currency": "USD",
    "payer_name": "Apex Trading Co",
    "payer_email": "ap@apex.example",
    "due_date": "2026-11-30",
    "description": "Invoice payment",
    "created_at": "2026-11-08T10:02:11",
    "updated_at": "2026-11-08T10:09:47",
    "transactions": [{
        "transaction_id": "txn_51b9e0",
        "allocated_amount": "4090.85",
        "transaction_amount": "4090.85",
        "match_type": "EXTRACTED_REF",
        "match_notes": "reference 'INV ORD-2026-2811' cleaned to 'ORD-2026-2811'; payer name similarity 1.0",
        "reference": "INV ORD-2026-2811",
        "payer_name": "apex trading co",
        "settled_at": "2026-11-08T10:09:40",
        "linked_at": "2026-11-08T10:09:47",
    }],
}
_UNMATCHED_EXAMPLE = {
    "transaction_id": "txn_c47d22",
    "reference": "UNKNOWN-2B943F38",
    "amount": "780.00",
    "currency": "USD",
    "payer_name": "Northwind Traders",
    "settled_at": "2026-11-08T10:12:03",
    "reason": "no usable reference; no USD payments from 'Northwind Traders' or a similar name",
}


class LinkedTransaction(BaseModel):
    transaction_id: str
    allocated_amount: Decimal = Field(description="Portion of the transaction applied to this payment")
    transaction_amount: Decimal = Field(description="Full transaction amount (differs from allocated for multi-payment splits)")
    match_type: str = Field(
        description="Why it matched: EXACT_REF, EXTRACTED_REF, MULTI_REF, NAME_AMOUNT, "
                    "REFUND_NAME_AMOUNT, FUZZY_NAME_AMOUNT, MULTI_NAME_AMOUNT, FUZZY_REF"
    )
    match_notes: str | None = None
    reference: str | None = None
    payer_name: str | None = None
    settled_at: datetime | None = None
    linked_at: datetime | None = None


class Payment(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [_PAYMENT_EXAMPLE]})

    payment_id: str
    reference: str | None = None
    status: PaymentStatus
    expected_amount: Decimal
    amount_due: Decimal = Field(
        description="Expected less refunds that reduced what the payer owes "
                    "(a refund first gives back any overpayment)"
    )
    received_amount: Decimal = Field(description="Net: payments less refunds")
    outstanding_amount: Decimal = Field(
        description="Balance still due: amount_due - received, or 0 once FULLY_PAID/OVERPAID "
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
    amount_due: Decimal
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
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "summary": {
            "total_payments": 20,
            "payments_by_status": {"PENDING": 3, "PARTIALLY_PAID": 4, "FULLY_PAID": 12, "OVERPAID": 1},
            "total_transactions": 27,
            "matched_transactions": 24,
            "unmatched_transactions": 3,
            "totals_by_currency": {"USD": {
                "expected": "61234.50",
                "amount_due": "61234.50",
                "received": "52310.12",
                "outstanding": "8016.40",
                "unmatched_transaction_amount": "2145.00",
            }},
        },
        "pagination": {"page": 1, "page_size": 20, "total_items": 20, "total_pages": 1},
        "payments": [_PAYMENT_EXAMPLE],
        "unmatched_transactions": [_UNMATCHED_EXAMPLE],
    }]})

    summary: Summary = Field(description="Computed over all data, independent of filters")
    pagination: Pagination
    payments: list[Payment]
    unmatched_transactions: list[UnmatchedTransaction] = Field(
        description="All unmatched transactions (the `search` filter applies; `status` does not)"
    )
