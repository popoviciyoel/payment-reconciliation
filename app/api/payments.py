"""Query API: reconciliation status of payments."""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_storage
from app.core.rate_limit import limit_queries
from app.domain.schemas import Payment, PaymentList
from app.domain.status import PaymentStatus
from app.services import queries
from app.storage import JsonStorage

router = APIRouter(
    prefix="/payments",
    tags=["payments"],
    dependencies=[Depends(limit_queries)],
    responses={429: {"description": "Rate limit exceeded; see the Retry-After header"}},
)


@router.get("", response_model=PaymentList)
def list_payments(
    status_filter: PaymentStatus | None = Query(None, alias="status", description="Only payments with this status"),
    search: str | None = Query(
        None, max_length=100,
        description="Case-insensitive substring of payment ID, reference, payer name or email "
                    "(also filters unmatched transactions by ID, reference or payer name)",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    storage: JsonStorage = Depends(get_storage),
) -> PaymentList:
    """
    All payments with their status and linked transactions, plus unmatched
    transactions and summary statistics. Payments are newest first.
    """
    return queries.list_payments(storage, status=status_filter, search=search, page=page, page_size=page_size)


@router.get("/{payment_id}", response_model=Payment, responses={404: {"description": "Payment not found"}})
def get_payment(payment_id: str, storage: JsonStorage = Depends(get_storage)) -> Payment:
    """A single payment with its status and linked transactions."""
    payment = queries.get_payment(storage, payment_id)
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Payment {payment_id} not found")
    return payment
