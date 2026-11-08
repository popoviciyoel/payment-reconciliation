"""
Payment Reconciliation Service
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import webhooks
from app.config import settings
from app.reconciliation.engine import reconcile, refresh_all
from app.storage import storage

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Catch up on anything stored but not reconciled (e.g. a crash between the
    # 200 and the background task), and re-project statuses in case the fee
    # tolerance changed.
    refresh_all(storage)
    report = reconcile(storage)
    logger.info("Startup reconciliation: %s", report)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Payment Reconciliation Service",
    description="Receives payment and transaction webhooks and reconciles them.",
    version="1.0.0",
)

app.include_router(webhooks.router)


@app.get("/", tags=["health"])
def root():
    """Health check"""
    return {"status": "healthy", "service": "reconciliation-service"}
