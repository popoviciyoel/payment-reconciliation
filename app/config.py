"""
Configuration settings for the Reconciliation Service.

This file is PROVIDED - loads configuration from environment variables.
"""

import os
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv

# Load .env before Settings reads os.environ (real env vars take precedence).
load_dotenv()


@dataclass
class Settings:
    """Application settings loaded from environment variables"""
    
    # Mock Provider
    mock_provider_url: str = os.getenv("MOCK_PROVIDER_URL", "https://mock-api.advancehq.com")
    mock_provider_api_key: str = os.getenv("MOCK_PROVIDER_API_KEY", "")
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    
    # Server
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"

    # Reconciliation: how far below the expected amount still counts as paid
    # (fee deductions). The allowance is the larger of the two.
    fee_tolerance_percent: Decimal = Decimal(os.getenv("FEE_TOLERANCE_PERCENT", "2"))
    fee_tolerance_fixed: Decimal = Decimal(os.getenv("FEE_TOLERANCE_FIXED", "0"))

    # Minimum payer-name similarity (0..1) for the fuzzy name + exact amount tier.
    fuzzy_name_threshold: float = float(os.getenv("FUZZY_NAME_THRESHOLD", "0.85"))

    # Typos (edits, adjacent swaps) allowed between a transaction reference and a
    # payment's once no payment has it exactly. 0 disables fuzzy reference matching.
    fuzzy_reference_max_edits: int = int(os.getenv("FUZZY_REFERENCE_MAX_EDITS", "1"))

    # A transaction whose reference matches no payment waits this long (from when we
    # received it) for that payment to arrive before falling back to payer + amount.
    reference_grace_minutes: float = float(os.getenv("REFERENCE_GRACE_MINUTES", "15"))

    # Periodic reconciliation, so waiting transactions are re-checked even when no
    # new events arrive. 0 disables the timer.
    reconcile_interval_seconds: float = float(os.getenv("RECONCILE_INTERVAL_SECONDS", "60"))

    # Rate limiting (per client; 0 disables). Query endpoints: sustained rate + burst.
    rate_limit_per_minute: float = float(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
    rate_limit_burst: int = int(os.getenv("RATE_LIMIT_BURST", "20"))
    # Failed webhook signature attempts per client per minute (valid ones are never limited).
    invalid_signature_limit_per_minute: float = float(os.getenv("INVALID_SIGNATURE_LIMIT_PER_MINUTE", "10"))
    # Comma-separated addresses of reverse proxies whose X-Forwarded-For is trusted
    # (e.g. "127.0.0.1,::1" for a local ngrok agent). Empty: never trust the header.
    trusted_proxies: str = os.getenv("TRUSTED_PROXIES", "")


# Singleton settings instance
settings = Settings()
