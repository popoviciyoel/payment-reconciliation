"""
Payment Reconciliation Service
"""

import logging

from fastapi import FastAPI

from app.api import webhooks
from app.config import settings

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(
    title="Payment Reconciliation Service",
    description="Receives payment and transaction webhooks and reconciles them.",
    version="1.0.0",
)

app.include_router(webhooks.router)


@app.get("/", tags=["health"])
def root():
    """Health check"""
    return {"status": "healthy", "service": "reconciliation-service"}
