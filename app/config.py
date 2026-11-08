"""
Configuration settings for the Reconciliation Service.

This file is PROVIDED - loads configuration from environment variables.
"""

import os
from dataclasses import dataclass


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


# Singleton settings instance
settings = Settings()
