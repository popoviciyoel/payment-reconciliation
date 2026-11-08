"""
Payment Reconciliation Service

Build your API here!
"""

from fastapi import FastAPI

app = FastAPI(
    title="Payment Reconciliation Service",
    description="Your reconciliation service goes here.",
    version="1.0.0",
)


@app.get("/")
def root():
    """Health check"""
    return {"status": "healthy", "service": "reconciliation-service"}


# TODO: Add your routers and endpoints here
# Example:
# from app.routers import webhooks, payments
# app.include_router(webhooks.router)
# app.include_router(payments.router)
