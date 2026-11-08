"""
Webhook endpoints.

Order of operations: verify signature -> validate schema -> idempotent store
-> 200. Reconciliation will be triggered after storage (next step).

Status codes:
  200  stored, duplicate, or unrecognised event_type (acknowledged, ignored)
  400  body is not a JSON object
  401  missing / invalid signature
  422  signed payload that fails schema validation
  500  server misconfigured (no webhook secret)
"""

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ValidationError

from app.api.deps import get_storage
from app.core.signature import verified_payload
from app.domain.models import (
    PAYMENT_EVENT_TYPE,
    TRANSACTION_EVENT_TYPE,
    PaymentCreated,
    TransactionSettled,
)
from app.services.ingest import IngestResult, ingest_payment, ingest_transaction
from app.storage import JsonStorage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookAck(BaseModel):
    status: str
    id: str | None = None


def _handle(
    payload: dict[str, Any],
    expected_type: str,
    model: type[BaseModel],
    ingest: Callable[[JsonStorage, Any, dict[str, Any]], IngestResult],
    storage: JsonStorage,
) -> WebhookAck:
    event_type = payload.get("event_type")
    if event_type != expected_type:
        # Acknowledge so the provider doesn't retry an event we'll never handle.
        logger.warning("Ignoring unexpected event_type=%r (expected %s)", event_type, expected_type)
        return WebhookAck(status="ignored")

    try:
        event = model.model_validate(payload)
    except ValidationError as e:
        logger.warning("Invalid %s payload: %s", expected_type, e.errors(include_url=False))
        raise HTTPException(422, e.errors(include_url=False))

    result = ingest(storage, event, payload)
    return WebhookAck(status=result.status.value, id=result.id)


@router.post("/payments", response_model=WebhookAck)
def receive_payment(
    payload: dict[str, Any] = Depends(verified_payload),
    storage: JsonStorage = Depends(get_storage),
) -> WebhookAck:
    """Receive a `payment.created` event."""
    return _handle(payload, PAYMENT_EVENT_TYPE, PaymentCreated, ingest_payment, storage)


@router.post("/transactions", response_model=WebhookAck)
def receive_transaction(
    payload: dict[str, Any] = Depends(verified_payload),
    storage: JsonStorage = Depends(get_storage),
) -> WebhookAck:
    """Receive a `transaction.settled` event."""
    return _handle(payload, TRANSACTION_EVENT_TYPE, TransactionSettled, ingest_transaction, storage)
