"""
Payment Reconciliation Service
"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api import payments, webhooks
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

    ticker = None
    if settings.reconcile_interval_seconds > 0:
        ticker = asyncio.create_task(_periodic_reconcile(settings.reconcile_interval_seconds))
    yield
    if ticker:
        ticker.cancel()
        with suppress(asyncio.CancelledError):
            await ticker


async def _periodic_reconcile(interval: float) -> None:
    """
    Re-run reconciliation on a timer so transactions waiting out the reference
    grace period are re-checked even when no new webhooks arrive.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            report = await asyncio.to_thread(reconcile, storage)
            if report.matched:
                logger.info("Periodic reconciliation: %s", report)
        except Exception:
            logger.exception("Periodic reconciliation failed; will retry")


app = FastAPI(
    lifespan=lifespan,
    title="Payment Reconciliation Service",
    description="Receives payment and transaction webhooks and reconciles them.",
    version="1.0.0",
)

app.include_router(webhooks.router)
app.include_router(payments.router)


@app.get("/", tags=["health"])
def root():
    """Health check"""
    return {"status": "healthy", "service": "reconciliation-service"}


UI_PAGE = Path(__file__).parent / "static" / "index.html"


@app.get("/ui", include_in_schema=False)
def ui():
    """Browser view of GET /payments (summary, payments, linked and unmatched transactions)."""
    return FileResponse(UI_PAGE)
