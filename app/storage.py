"""
Simple JSON-based storage for the reconciliation service.

This file is PROVIDED - no database installation required!
You can use this as-is or extend it if needed.

Data Structure:
- payments: Expected payments from payment.created webhooks
- transactions: Bank transactions from transaction.settled webhooks
- reconciliation_links: Links between transactions and payments

Usage:
    from app.storage import storage

    # Store a payment
    storage.add_payment({
        "payment_id": "pay_123",
        "reference": "INV-2024-001",
        "expected_amount": "1000.00",
        ...
    })

    # Store a transaction
    storage.add_transaction({
        "transaction_id": "txn_456",
        "reference": "INV-2024-001",
        "amount": "400.00",
        ...
    })

    # Link a transaction to a payment
    storage.add_reconciliation_link({
        "payment_id": "pay_123",
        "transaction_id": "txn_456",
        "match_type": "EXACT",
        ...
    })
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


def generate_id(prefix: str = "") -> str:
    """Generate a unique ID with optional prefix"""
    short_uuid = str(uuid4())[:8]
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{timestamp}_{short_uuid}" if prefix else f"{timestamp}_{short_uuid}"


class JsonStorage:
    """
    Thread-safe JSON file storage.

    Provides simple CRUD operations without requiring a database.
    Data is persisted to a JSON file for durability across restarts.
    """

    def __init__(self, file_path: str = "data/storage.json"):
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._data = self._load()

    def _load(self) -> dict:
        """Load data from JSON file"""
        if self.file_path.exists():
            try:
                with open(self.file_path, "r") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logger.warning("Corrupted storage file, starting fresh")
        return {
            "payments": {},  # payment_id -> payment data
            "transactions": {},  # transaction_id -> transaction data
            "reconciliation_links": [],  # Links between transactions and payments
        }

    def _save(self) -> None:
        """Persist data to JSON file"""
        with open(self.file_path, "w") as f:
            json.dump(self._data, f, indent=2, default=str)

    def reset(self) -> None:
        """Clear all data (useful for testing)"""
        with self._lock:
            self._data = {
                "payments": {},
                "transactions": {},
                "reconciliation_links": [],
            }
            self._save()

    # =========================================================================
    # Payments (from payment.created webhook)
    # =========================================================================

    def add_payment(self, payment: dict) -> dict:
        """
        Store a payment from the payment.created webhook.

        Expected fields:
            payment_id, reference, expected_amount, currency,
            payer_name, payer_email, due_date, description

        You may want to add:
            status (PENDING, PARTIALLY_PAID, FULLY_PAID, OVERPAID)
            received_amount (calculated from linked transactions)
        """
        with self._lock:
            payment_id = payment.get("payment_id") or generate_id("pay")
            now = datetime.now().isoformat()

            payment_record = {
                "payment_id": payment_id,
                "reference": payment.get("reference"),
                "expected_amount": payment.get("expected_amount"),
                "currency": payment.get("currency", "USD"),
                "payer_name": payment.get("payer_name"),
                "payer_email": payment.get("payer_email"),
                "due_date": payment.get("due_date"),
                "description": payment.get("description"),
                # Add your own fields:
                "status": "PENDING",
                "received_amount": "0.00",
                "created_at": now,
                "updated_at": now,
            }

            self._data["payments"][payment_id] = payment_record
            self._save()
            return payment_record

    def get_payment(self, payment_id: str) -> Optional[dict]:
        """Get payment by ID"""
        return self._data["payments"].get(payment_id)

    def get_payment_by_reference(self, reference: str) -> Optional[dict]:
        """Find payment by reference"""
        for payment in self._data["payments"].values():
            if payment.get("reference") == reference:
                return payment
        return None

    def get_all_payments(self) -> list[dict]:
        """Get all payments"""
        return list(self._data["payments"].values())

    def get_payments_by_status(self, status: str) -> list[dict]:
        """Get payments filtered by status"""
        return [p for p in self._data["payments"].values() if p.get("status") == status]

    def update_payment(self, payment_id: str, updates: dict) -> Optional[dict]:
        """Update a payment"""
        with self._lock:
            payment = self._data["payments"].get(payment_id)
            if payment:
                payment.update(updates)
                payment["updated_at"] = datetime.now().isoformat()
                self._save()
            return payment

    # =========================================================================
    # Transactions (from transaction.settled webhook)
    # =========================================================================

    def add_transaction(self, transaction: dict) -> dict:
        """
        Store a transaction from the transaction.settled webhook.

        Expected fields:
            transaction_id, reference (may be null), amount (may be negative for refunds),
            currency, payer_name, payer_account_last_four, settled_at, bank_reference

        You may want to add:
            matched (bool), matched_to_payment_id
        """
        with self._lock:
            transaction_id = transaction.get("transaction_id") or generate_id("txn")
            now = datetime.now().isoformat()

            transaction_record = {
                "transaction_id": transaction_id,
                "reference": transaction.get("reference"),  # May be null!
                "amount": transaction.get("amount"),  # May be negative!
                "currency": transaction.get("currency", "USD"),
                "payer_name": transaction.get("payer_name"),
                "payer_account_last_four": transaction.get("payer_account_last_four"),
                "settled_at": transaction.get("settled_at"),
                "bank_reference": transaction.get("bank_reference"),
                # Add your own fields:
                "matched": False,
                "matched_to_payment_id": None,
                "created_at": now,
            }

            self._data["transactions"][transaction_id] = transaction_record
            self._save()
            return transaction_record

    def get_transaction(self, transaction_id: str) -> Optional[dict]:
        """Get transaction by ID"""
        return self._data["transactions"].get(transaction_id)

    def get_transaction_by_provider_id(self, provider_transaction_id: str) -> Optional[dict]:
        """Find transaction by provider's ID (for idempotency)"""
        for txn in self._data["transactions"].values():
            if txn.get("transaction_id") == provider_transaction_id:
                return txn
        return None

    def get_all_transactions(self) -> list[dict]:
        """Get all transactions"""
        return list(self._data["transactions"].values())

    def get_unmatched_transactions(self) -> list[dict]:
        """Get transactions that haven't been matched to a payment"""
        return [t for t in self._data["transactions"].values() if not t.get("matched", False)]

    def update_transaction(self, transaction_id: str, updates: dict) -> Optional[dict]:
        """Update a transaction"""
        with self._lock:
            txn = self._data["transactions"].get(transaction_id)
            if txn:
                txn.update(updates)
                self._save()
            return txn

    # =========================================================================
    # Reconciliation Links
    # =========================================================================

    def add_reconciliation_link(self, link: dict) -> dict:
        """
        Record a link between a transaction and a payment.

        Fields:
            payment_id, transaction_id, match_type, amount, notes
        """
        with self._lock:
            link_record = {
                "link_id": generate_id("link"),
                "payment_id": link.get("payment_id"),
                "transaction_id": link.get("transaction_id"),
                "match_type": link.get("match_type"),  # EXACT, FUZZY_REF, AMOUNT_ONLY, etc.
                "amount": link.get("amount"),
                "notes": link.get("notes"),
                "created_at": datetime.now().isoformat(),
            }

            self._data["reconciliation_links"].append(link_record)
            self._save()
            return link_record

    def get_links_for_payment(self, payment_id: str) -> list[dict]:
        """Get all reconciliation links for a payment"""
        return [link for link in self._data["reconciliation_links"] if link.get("payment_id") == payment_id]

    def get_all_reconciliation_links(self) -> list[dict]:
        """Get all reconciliation links"""
        return self._data["reconciliation_links"]

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_statistics(self) -> dict:
        """Get summary statistics"""
        payments = self._data["payments"].values()
        transactions = self._data["transactions"].values()

        status_counts = {}
        for p in payments:
            status = p.get("status", "UNKNOWN")
            status_counts[status] = status_counts.get(status, 0) + 1

        matched_txns = sum(1 for t in transactions if t.get("matched"))
        unmatched_txns = sum(1 for t in transactions if not t.get("matched"))

        return {
            "total_payments": len(self._data["payments"]),
            "payment_status_counts": status_counts,
            "total_transactions": len(self._data["transactions"]),
            "matched_transactions": matched_txns,
            "unmatched_transactions": unmatched_txns,
            "total_reconciliation_links": len(self._data["reconciliation_links"]),
        }


# Singleton instance - use this throughout the application
storage = JsonStorage()
