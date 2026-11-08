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

    # A transaction whose reference matches no payment waits this long (from when we
    # received it) for that payment to arrive before falling back to payer + amount.
    reference_grace_minutes: float = float(os.getenv("REFERENCE_GRACE_MINUTES", "15"))

    # Periodic reconciliation, so waiting transactions are re-checked even when no
    # new events arrive. 0 disables the timer.
    reconcile_interval_seconds: float = float(os.getenv("RECONCILE_INTERVAL_SECONDS", "60"))


# Singleton settings instance
settings = Settings()
