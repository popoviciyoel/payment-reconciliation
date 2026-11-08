"""
Webhook ingestion: idempotent persistence of verified events.

Idempotency key is the provider's own ID (payment_id / transaction_id).
A repeat delivery is acknowledged (so the provider stops retrying) but not
re-stored. If a repeat carries *different* data, we keep the original and log
a warning — silently overwriting financial records is worse than flagging.
"""

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any

from app.domain.models import PaymentCreated, TransactionSettled
from app.storage import JsonStorage

logger = logging.getLogger(__name__)

# Check-then-insert must be atomic; JsonStorage's own lock is not reentrant
# and only covers single operations, so ingestion has its own.
_ingest_lock = Lock()

RAW_EVENT_LOG = Path("data/raw_events.jsonl")


class IngestStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    id: str


def _log_raw_event(kind: str, payload: dict[str, Any]) -> None:
    """Append the verified raw payload for later analysis of real-world data."""
    try:
        RAW_EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with RAW_EVENT_LOG.open("a") as f:
            f.write(json.dumps({"kind": kind, "payload": payload}) + "\n")
    except OSError:
        logger.exception("Failed to write raw event log")  # never block ingestion on this


def _warn_if_conflicting(kind: str, key: str, stored: dict, incoming: dict) -> None:
    diffs = {
        f: (stored.get(f), incoming[f])
        for f in incoming
        if f in stored and str(stored.get(f)) != str(incoming[f])
    }
    if diffs:
        logger.warning("Duplicate %s %s with conflicting data, keeping original: %s", kind, key, diffs)


def ingest_payment(storage: JsonStorage, event: PaymentCreated, raw: dict[str, Any]) -> IngestResult:
    record = event.model_dump(mode="json", exclude={"event_type", "timestamp", "sandbox_id"})
    with _ingest_lock:
        existing = storage.get_payment(event.payment_id)
        if existing is not None:
            _warn_if_conflicting("payment", event.payment_id, existing, record)
            return IngestResult(IngestStatus.DUPLICATE, event.payment_id)
        storage.add_payment(record)
    _log_raw_event("payment", raw)
    logger.info("Stored payment %s ref=%s amount=%s", event.payment_id, event.reference, event.expected_amount)
    return IngestResult(IngestStatus.ACCEPTED, event.payment_id)


def ingest_transaction(storage: JsonStorage, event: TransactionSettled, raw: dict[str, Any]) -> IngestResult:
    record = event.model_dump(mode="json", exclude={"event_type", "timestamp", "sandbox_id"})
    with _ingest_lock:
        existing = storage.get_transaction(event.transaction_id)
        if existing is not None:
            _warn_if_conflicting("transaction", event.transaction_id, existing, record)
            return IngestResult(IngestStatus.DUPLICATE, event.transaction_id)
        storage.add_transaction(record)
    _log_raw_event("transaction", raw)
    logger.info("Stored transaction %s ref=%s amount=%s", event.transaction_id, event.reference, event.amount)
    return IngestResult(IngestStatus.ACCEPTED, event.transaction_id)
