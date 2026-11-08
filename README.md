# Payment Reconciliation Service

A FastAPI service that receives two signed webhook streams, **payments** (money we expect)
and **transactions** (money that actually settled), and reconciles them. It links
transactions to payments, tracks partial payments, allows for fee deductions, handles
refunds, and serves the results through a query API.

- Assignment brief: [`ASSIGNMENT.md`](ASSIGNMENT.md)
- Full design notes, observed data, assumptions and open questions: [`DESIGN.md`](DESIGN.md)

**Contents:** [Quick start](#quick-start) · [API](#api) · [Design decisions](#design-decisions) ·
[Improvements](#what-i-would-improve-with-more-time) · [AI usage](#ai-usage-declaration)

---

## Quick start

Requires Python 3.11+ and [ngrok](https://ngrok.com/) to receive webhooks locally.

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp env.example.txt .env        # fill in MOCK_PROVIDER_URL, MOCK_PROVIDER_API_KEY, WEBHOOK_SECRET

uvicorn app.main:app --reload --port 8000
ngrok http 8000                # in another terminal
```

Or with Docker: `docker compose up --build` (reads `.env`, keeps data in `./data`).

### Connect to the mock provider

```bash
set -a; source .env; set +a
NGROK=https://YOUR-SUBDOMAIN.ngrok-free.dev

# 1. Register once, then save api_key and webhook_secret into .env
curl -X POST "$MOCK_PROVIDER_URL/api/v1/auth/register" -H "Content-Type: application/json" \
  -d '{"candidate_email": "you@example.com", "candidate_name": "Your Name"}'

# 2. Subscribe both webhooks (events start once both exist)
for t in payments transactions; do
  curl -X POST "$MOCK_PROVIDER_URL/api/v1/webhooks" \
    -H "Authorization: Bearer $MOCK_PROVIDER_API_KEY" -H "Content-Type: application/json" \
    -d "{\"url\": \"$NGROK/webhooks/$t\", \"webhook_type\": \"$t\"}"
done

# 3. Unsubscribe
for t in payments transactions; do
  curl -X DELETE "$MOCK_PROVIDER_URL/api/v1/webhooks/$t" -H "Authorization: Bearer $MOCK_PROVIDER_API_KEY"
done
```

The ngrok free tier gives you a new URL on every restart, so subscribe again when it changes.

### Configuration

Only `WEBHOOK_SECRET` is required. Webhooks are rejected without it.

| Variable | Purpose | Default |
|---|---|---|
| `FEE_TOLERANCE_PERCENT` / `FEE_TOLERANCE_FIXED` | Shortfall still counted as paid (the larger wins) | `2` / `0` |
| `FUZZY_NAME_THRESHOLD` | Minimum payer-name similarity for fuzzy matching | `0.85` |
| `REFERENCE_GRACE_MINUTES` | How long an unknown reference waits for its payment before falling back to payer + amount | `15` |
| `RECONCILE_INTERVAL_SECONDS` | Periodic reconciliation (0 disables) | `60` |
| `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_BURST` | Per-client limit on query endpoints (0 disables) | `60` / `20` |
| `INVALID_SIGNATURE_LIMIT_PER_MINUTE` | Failed signatures per client before `429` | `10` |
| `TRUSTED_PROXIES` | Proxies whose `X-Forwarded-For` is trusted (`127.0.0.1,::1` behind local ngrok) | empty |
| `DEBUG` | Verbose logs; records rejected webhooks to `data/rejected_events.jsonl` | `false` |

### Tests

```bash
pytest                                      # or: docker compose run --rm api pytest
```

About 450 tests cover each component, plus end-to-end replays of three fixture sets.
Known gaps are marked as strict `xfail`, so they start failing once fixed.

| Fixture | Contents |
|---|---|
| `observed_events.jsonl` | Every event captured from the live provider. Replays must produce the same results in any delivery order. |
| `edge_cases.jsonl` | About 150 hand-written edge cases, each with its expected outcome |
| `simulated_seed42.jsonl` + 50 generated sets | Random data with known correct answers. The matcher must never link money to the wrong payment. |

```bash
python -m scripts.simulate generate --seed 7 --payments 200 --out data/simulated.jsonl
WEBHOOK_SECRET=... python -m scripts.simulate replay tests/fixtures/edge_cases.jsonl --url http://localhost:8000
```

---

## API

Interactive docs at **http://localhost:8000/docs**. A browser view of the data is at **`/ui`**.

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhooks/payments` | Receive `payment.created` |
| `POST` | `/webhooks/transactions` | Receive `transaction.settled` |
| `GET` | `/payments` | Payments with status and linked transactions, unmatched transactions with reasons, summary totals |
| `GET` | `/payments/{payment_id}` | One payment with its linked transactions |
| `GET` | `/` | Health check |

- **Filters on `GET /payments`:** `status` (`PENDING`, `PARTIALLY_PAID`, `FULLY_PAID`,
  `OVERPAID`), `search` (payment ID, reference, payer name or email), `page`, and
  `page_size` (default 20, max 100).
- **Rate limiting:** query endpoints share a per-client token bucket (60/min, bursts of 20)
  and return `429` with `Retry-After` when it runs out. Correctly signed webhooks are never
  limited.
- **Webhook responses:** `200` (stored, duplicate, or unknown type), `400` malformed JSON,
  `401` bad signature, `422` invalid payload, `429` too many bad signatures, `500` no secret
  configured.

### Project layout

```
app/
  api/              webhook and query endpoints
  core/             signature verification, rate limiting
  domain/           event and response schemas; payment status rules (pure)
  services/         idempotent ingestion, read-side queries
  reconciliation/   normalize + matcher (pure), engine (writes links atomically)
  storage.py        provided JSON storage, made atomic and crash-safe
tests/              one module per component, plus fixtures
```

The matching and status logic are pure functions with no I/O. The engine and API are thin
layers around them.

---

## Design decisions

The full reasoning and trade-offs are in [`DESIGN.md`](DESIGN.md).

### 1. I built the matching rules from real data
Before writing the matcher, I logged every event the provider sent. That data showed noisy
references (`INV REF-2026-6981`), blank and `UNKNOWN-*` references, partial and slightly
short payments, and payer-name variants. Each matching tier handles a case found there.

### 2. Matching tiers: the most confident rule runs first
| Tier | Rule | Match type |
|---|---|---|
| 1 | Reference matches a payment exactly, or appears inside noisy text | `EXACT_REF` / `EXTRACTED_REF` |
| 1b | Several references: the amount is split across them, filling each balance in order | `MULTI_REF` |
| 2 | No usable reference: same payer, amount within fee tolerance, exactly one candidate | `NAME_AMOUNT` / `REFUND_NAME_AMOUNT` |
| 2c | Same payer, amount exactly equals the combined balance of 2–3 payments, exactly one combination | `MULTI_NAME_AMOUNT` |
| 2b | No exact payer: name similarity ≥ 0.85 and the **exact** amount, exactly one candidate | `FUZZY_NAME_AMOUNT` |
| 3 | Otherwise unmatched, with a recorded reason | none |

Currency must always match. Every link records its match type and notes, so you can see why
each match was made.

### 3. A wrong match is worse than a missed one
Money on the wrong invoice is harder to undo than a transaction waiting for review, so:
- If two candidates fit equally well, the transaction stays unmatched.
- The payer name alone never decides a match.
- A reference that matches no payment **waits 15 minutes** for its payment to arrive, then
  falls back to payer + amount. In simulations, falling back immediately put money on the
  wrong invoice. Empty or placeholder references fall back at once.
- Fuzzy name matching needs an exact amount. Its 0.85 threshold comes from the data:
  different payers scored at most 0.56, and misspellings scored 0.85–0.97.

### 4. Links are the single source of truth
Each link records the amount it allocates, so one transaction can cover several payments and
several transactions can cover one payment. A payment's `received_amount` and `status` are
**recalculated from its links** rather than incremented, so they can't drift.

| Status | Rule |
|---|---|
| `PENDING` | Nothing received, or refunded back to zero |
| `PARTIALLY_PAID` | Below the expected amount minus the fee allowance |
| `FULLY_PAID` | Within the fee allowance, up to the expected amount |
| `OVERPAID` | More than expected |

The fee allowance (default 2%) only applies below the expected amount, because fees reduce
what arrives. Refunds are negative links, so a status can move back down.

### 5. Webhooks: verify, deduplicate, store, acknowledge, then reconcile
- **Signatures** are checked over canonical JSON with a constant-time comparison. Without a
  configured secret, every webhook is rejected.
- **Idempotency** uses the provider's IDs, not a body hash. The provider resends duplicates
  about 10 minutes later with a new `timestamp`, so a body hash would store them twice.
- **Fast 200:** matching runs in the background after the response (median 3–7 ms).
- **Late arrivals:** each run re-checks unmatched transactions, so a transaction that arrives
  before its payment is matched once the payment lands.

### 6. Crash safety
- The provided storage truncated its file before writing, so a crash could lose all data.
  Saves now write a temp file, `fsync`, and atomically replace the original.
- Each match is one atomic write, and a linked transaction is never linked again, so a crash
  can't count money twice.

### 7. Money conventions
`Decimal` everywhere. Amounts are two-decimal strings. Totals are grouped by currency and
never summed across currencies.

### 8. AI analyzes, deterministic rules link money
AI analysis of the raw webhook log classified the real cases that shaped the tiers above. The
step that links money stays deterministic, because it runs on every webhook and must be
reproducible, auditable and fast.

DESIGN.md §9 describes an **AI analysis agent** (designed, not built). It would explain
unmatched transactions, suggest matches for human approval, detect new patterns to turn into
rules, and write reconciliation reports. It would be read-only and advisory.

---

## What I would improve with more time

**Reliability and scale**
- Replace the JSON file with Postgres. Each write currently rewrites the whole file, and
  webhook latency grew from ~1.4 ms to ~17 ms over 900 events.
- Run reconciliation on a durable queue and worker, batch runs during bursts, and re-check
  only transactions related to a new payment.
- Update totals in the same DB transaction as each link, and keep full recalculation as a
  scheduled audit.

**Matching**
- Fuzzy reference matching (no typo'd references appeared in the data yet).
- A review endpoint to confirm, reject or reassign matches, with an audit trail.
- Build the AI analysis agent.
- Find out whether the 1.5–4.8% short payments are fees or partial payments (DESIGN.md §7).
  The answer decides the right default tolerance.

**API and operations**
- Move rate limiting to Redis or a gateway so it works across multiple instances.
- Cursor pagination and authentication on the query endpoints.
- Structured logs and metrics: match rate, unmatched amount, latency, signature failures.

**Known limitations**
- Single process only (locks are in-process).
- A tolerance change applies on restart and doesn't redo existing links.
- Refunds and multi-payment transactions are tested but haven't appeared in provider data yet.
- A refund without a reference can't be matched to a transaction that tier 2c split across
  several payments. It stays unmatched for review.
- If a noisy reference names one real invoice and one unknown one, the whole transaction
  waits out the grace period.

---

## AI usage declaration

I used **Claude Code** (Claude Opus 5.5) to write code, analyze the raw webhook data, and
generate simulated test data. The architecture and design decisions were mine.

**What I did**
- Set the architecture: pure matching and status logic, thin I/O layers, links as the single
  source of truth, atomic storage.
- Made the trade-offs: recalculating status instead of storing it, background reconciliation
  instead of a worker, preferring a missed match over a wrong one, and keeping AI out of the
  step that links money.
- Questioned assumptions the brief leaves open, such as transactions arriving before their
  payments. Every assumption is listed and checked against real data in DESIGN.md §6.
- Registered with the provider, ran the server and ngrok, and investigated problems in the
  live logs.

**What AI did**
- **Code:** wrote the application code and tests from my design, one reviewed change at a time.
- **Data analysis:** classified the cases in the raw event log, compared duplicate
  deliveries, and measured name similarity. I used that analysis to choose the tiers and
  thresholds.
- **Simulated data:** built the seeded generator and the fixtures, including cases the live
  provider hadn't sent yet (refunds, multi-reference transactions, other currencies,
  out-of-order delivery).

**How I validated it**
- **Tests:** about 450, many built from real provider events, plus simulated replays with
  known correct answers.
- **Live data:** checked every stored event and match. 24 of 27 transactions linked as
  expected, and the three `UNKNOWN-*` transactions were correctly left unmatched.
- **Evidence over assumptions:** logged rejected webhooks to confirm the 401s were
  deliberate, compared duplicate payloads, benchmarked latency, and derived the fuzzy
  threshold from real payer names.
- **Fresh-context reviews:** new Claude Code sessions with no prior conversation reviewed
  the code cold, alongside the code-review skill. I verified each finding and fixed the
  confirmed ones with a regression test.
- **Manual testing:** checked statuses, links, unmatched reasons, totals, filters and
  pagination in `/ui` and `/docs` against the stored data.
- **Defects caught this way**, each now covered by a test: a crash window that could link a
  transaction twice, the provided storage losing all data on an interrupted write, a settled
  payment still showing a balance due, and reference matching that only recognized one format.
