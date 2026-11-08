# 🏦 Payment Reconciliation Service - Take-Home Assignment

## Overview

Build a **Payment Reconciliation Service** that receives two streams of webhook events and matches them together:

1. **Payments** - Expected money to receive (like invoices)
2. **Transactions** - Actual money that arrived in the bank

Your job is to **reconcile** transactions against payments, handling various real-world complications.

**Stack:** Python 3.11+, FastAPI

---

## Quick Start

```bash
# Setup
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Run
uvicorn app.main:app --reload --port 8000
```

---

## The Mock Payment Provider

We host a Mock Payment Provider at: **`https://interview-mock-provider.r6zcf729z3zke.us-east-1.cs.amazonlightsail.com` /**

### Step 1: Register

```bash
curl -X POST https://interview-mock-provider.r6zcf729z3zke.us-east-1.cs.amazonlightsail.com/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"candidate_email": "your.email@example.com", "candidate_name": "Your Name"}'
```

Save the `api_key` and `webhook_secret` from the response.

### Step 2: Expose Your Local Server

```bash
ngrok http 8000
```

Note your public URL (e.g., `https://abc123.ngrok.io`)

### Step 3: Register BOTH Webhooks

Register **two separate webhooks**. Events start automatically when both are registered!

**Endpoint:** `POST https://mock-api.advancehq.com/api/v1/webhooks`

**Headers:**

- `Authorization: Bearer YOUR_API_KEY`
- `Content-Type: application/json`

**Request Body:**
| Field | Type | Description |
|-------|------|-------------|
| `url` | string | Your public webhook URL (e.g., `https://YOUR-NGROK-URL/webhooks/payments`) |
| `webhook_type` | string | Either `"payments"` or `"transactions"` |

You need to register both webhook types:

1. **Payments webhook** → receives payment notifications at `/webhooks/payments`
2. **Transactions webhook** → receives transaction notifications at `/webhooks/transactions` (events START when this is registered!)

### What Happens Next

Once both webhooks are registered:

- Payments and transactions will be sent continuously to your endpoints
- Events continue until you unregister webhooks
- To stop: `DELETE /api/v1/webhooks/payments` or `DELETE /api/v1/webhooks/transactions`

---

## Webhook Payloads

### Payment Webhook (`payment.created`)

```json
{
  "event_type": "payment.created",
  "payment_id": "pay_abc123",
  "reference": "INV-2024-1234",
  "expected_amount": "1000.00",
  "currency": "USD",
  "payer_name": "Acme Corporation",
  "payer_email": "billing@acme.com",
  "due_date": "2024-01-20",
  "description": "Invoice payment",
  "timestamp": "2024-01-15T10:00:00Z",
  "sandbox_id": "sandbox_xyz"
}
```

### Transaction Webhook (`transaction.settled`)

```json
{
  "event_type": "transaction.settled",
  "transaction_id": "txn_xyz789",
  "reference": "INV-2024-1234",
  "amount": "400.00",
  "currency": "USD",
  "payer_name": "ACME CORP",
  "payer_account_last_four": "4242",
  "settled_at": "2024-01-16T14:30:00Z",
  "bank_reference": "BANK-ABC123",
  "timestamp": "2024-01-16T14:30:00Z",
  "sandbox_id": "sandbox_xyz"
}
```

### Signature Verification

Each webhook includes an `X-Webhook-Signature` header containing an HMAC-SHA256 signature. You must verify this signature using your `webhook_secret` to ensure the request is authentic. The signature format is `sha256=<hex_digest>`.

**⚠️ Important considerations for signature verification:**

- **Canonical JSON:** The signature is computed on canonical JSON (keys sorted alphabetically, compact separators with no spaces), not the raw request body
- **Exclude `sandbox_id`:** The `sandbox_id` field is added _after_ signing — exclude it when computing the signature
- **Use constant-time comparison:** Always use a timing-safe comparison function to prevent timing attacks

---

## Reconciliation Challenge

Your task is to match incoming bank transactions to expected payments.

A transaction may match **zero**, **one**, or **multiple** payments. Your job is to figure out the matching logic based on the data you receive. Not all transactions will have a matching payment, and not all payments will receive matching transactions.

---

## Requirements

### 1. Webhook Endpoints

Build two webhook endpoints:

- `POST /webhooks/payments` - Receive payment notifications
- `POST /webhooks/transactions` - Receive transaction notifications

Both should:

- Verify the `X-Webhook-Signature` header
- Handle duplicates (idempotency)
- Store the events
- Return 200 OK quickly

### 2. Reconciliation Logic

Match transactions to payments:

- Link transactions to their matching payment(s) if a match exists
- Track partial payments (sum of transactions vs expected amount)
- Handle fuzzy matching (references, payer names)
- Account for fee deductions (configurable tolerance)
- Handle refunds (negative amounts)

### 3. Payment Status

Track payment status:

- `PENDING` - No transactions matched yet
- `PARTIALLY_PAID` - Some money received, balance remaining
- `FULLY_PAID` - Total received >= expected amount
- `OVERPAID` - Received more than expected

### 4. Query API

Build endpoints to view reconciliation status:

**Get all payments:**

```
GET /payments
```

Returns all payments with their current status, linked transactions, unmatched transactions, and summary statistics.

**Get payment by ID:**

```
GET /payments/{payment_id}
```

Returns a single payment with its status and linked transactions.

---

## What's Provided

- `app/storage.py` - JSON file storage (no database needed)
- `app/config.py` - Environment configuration
- `app/main.py` - Minimal FastAPI skeleton
- `requirements.txt` - Dependencies

**Everything else is up to you.** Design your project structure as you see fit.

---

## Bonus Tasks (Optional)

- **Tests**
- **Rate Limiting**
- **Pagination** - Add pagination to `GET /payments`
- **Search** - Add search by term to `GET /payments`
- **Documentation** - OpenAPI descriptions and examples
- **Docker** - docker-compose for easy setup

---

## ⚠️ Candidate Priority Guide

**Focus on getting a working solution first, then improve.**
**A working basic solution beats a sophisticated incomplete one.**

## Evaluation Criteria

| Criteria             |
| -------------------- |
| **Correctness**      |
| **Code Structure**   |
| **Error Handling**   |
| **Algorithm Design** |
| **API Design**       |
| **Code Quality**     |

---

## Submission

1. **GitHub Repository** with clear commit history

2. **README** with:
   - Setup instructions
   - Design decisions
   - What you would improve with more time

3. **AI Usage Declaration**:
   - What you wrote yourself vs. with AI assistance
   - How you validated AI-generated code

---

## Questions?

If anything is unclear, reach out! Asking good questions is part of the job.

**Good luck!**
