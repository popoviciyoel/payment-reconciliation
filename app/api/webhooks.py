"""
Webhook endpoints.

Order of operations: verify signature -> validate schema -> idempotent store
-> 200 -> reconciliation runs as a background task (newly stored events only).

Status codes:
  200  stored, duplicate, or unrecognised event_type (acknowledged, ignored)
  400  body is not a JSON object
  401  missing / invalid signature
  422  signed payload that fails schema validation
  500  server misconfigured (no webhook secret)
"""

import logging
from typing import Any, Callable

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError

from app.api.deps import get_storage
from app.core.signature import SIGNATURE_HEADER, verified_payload
from app.domain.models import (
    PAYMENT_EVENT_TYPE,
    TRANSACTION_EVENT_TYPE,
    PaymentCreated,
    TransactionSettled,
)
from app.reconciliation.engine import reconcile
from app.services.ingest import IngestResult, IngestStatus, ingest_payment, ingest_transaction
from app.storage import JsonStorage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookAck(BaseModel):
    model_config = {"json_schema_extra": {"examples": [{"status": "accepted", "id": "pay_abc123"}]}}

    status: str = Field(description="accepted, duplicate (already stored), or ignored (unrecognised event_type)")
    id: str | None = Field(None, description="The payment_id or transaction_id")


def _openapi(model: type[BaseModel]) -> dict[str, Any]:
    """
    Document the request body and signature header. The body is read raw by
    `verified_payload` (the signature is checked before validation), so FastAPI
    can't infer either from the function signature.
    """
    return {
        "parameters": [{
            "name": SIGNATURE_HEADER,
            "in": "header",
            "required": True,
            "description": "`sha256=<hex>`: HMAC-SHA256 with the webhook secret over canonical JSON "
                           "(sorted keys, compact separators, `sandbox_id` excluded)",
            "schema": {"type": "string", "example": "sha256=5d41402abc4b2a76b9719d911017c592"},
        }],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        },
    }


_WEBHOOK_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"description": "Body is not a JSON object"},
    401: {"description": "Missing or invalid signature"},
    422: {"description": "Signed payload that fails validation"},
    429: {"description": "Too many invalid-signature attempts from this client; see Retry-After"},
    500: {"description": "Server has no webhook secret configured"},
}


def _handle(
    payload: dict[str, Any],
    expected_type: str,
    model: type[BaseModel],
    ingest: Callable[[JsonStorage, Any, dict[str, Any]], IngestResult],
    storage: JsonStorage,
    background_tasks: BackgroundTasks,
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
    if result.status is IngestStatus.ACCEPTED:
        background_tasks.add_task(reconcile, storage)
    return WebhookAck(status=result.status.value, id=result.id)


@router.post("/payments", response_model=WebhookAck, responses=_WEBHOOK_ERRORS, openapi_extra=_openapi(PaymentCreated))
def receive_payment(
    background_tasks: BackgroundTasks,
    payload: dict[str, Any] = Depends(verified_payload),
    storage: JsonStorage = Depends(get_storage),
) -> WebhookAck:
    """Receive a `payment.created` event."""
    return _handle(payload, PAYMENT_EVENT_TYPE, PaymentCreated, ingest_payment, storage, background_tasks)


@router.post("/transactions", response_model=WebhookAck, responses=_WEBHOOK_ERRORS, openapi_extra=_openapi(TransactionSettled))
def receive_transaction(
    background_tasks: BackgroundTasks,
    payload: dict[str, Any] = Depends(verified_payload),
    storage: JsonStorage = Depends(get_storage),
) -> WebhookAck:
    """Receive a `transaction.settled` event."""
    return _handle(payload, TRANSACTION_EVENT_TYPE, TransactionSettled, ingest_transaction, storage, background_tasks)
