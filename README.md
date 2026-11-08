# Payment Reconciliation Service

A FastAPI service that receives two signed webhook streams — **payments** (money we
expect) and **transactions** (money that actually settled) — and reconciles them: it links
transactions to payments, tracks partial payments, tolerates fee deductions, handles
refunds, and exposes the results through a query API.

- Assignment brief: [`ASSIGNMENT.md`](ASSIGNMENT.md)
- Full design notes, observed provider data, assumptions and open questions: [`DESIGN.md`](DESIGN.md)

---

## Setup

**Requirements:** Python 3.11+ (developed and tested on 3.14), [ngrok](https://ngrok.com/)
to receive webhooks locally.

```bash
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp env.example.txt .env             # then fill in the values below
```

| Variable | Purpose | Default |
|---|---|---|
| `MOCK_PROVIDER_URL` | Mock provider base URL | — (use the Lightsail URL from the brief) |
| `MOCK_PROVIDER_API_KEY` | API key from provider registration | — |
| `WEBHOOK_SECRET` | Secret used to verify webhook signatures | — (**required**; webhooks are rejected without it) |
| `FEE_TOLERANCE_PERCENT` | Allowed shortfall, % of the amount due, still counted as paid | `2` |
| `FEE_TOLERANCE_FIXED` | Allowed shortfall as a fixed amount (the larger allowance wins) | `0` |
| `FUZZY_NAME_THRESHOLD` | Minimum payer-name similarity (0–1) for fuzzy matching | `0.85` |
| `REFERENCE_GRACE_MINUTES` | How long a transaction whose reference matches no payment waits before payer + amount matching (0 = immediately) | `15` |
| `RECONCILE_INTERVAL_SECONDS` | Periodic reconciliation interval (0 disables) | `60` |
| `DEBUG` | Verbose logs; also records rejected webhooks to `data/rejected_events.jsonl` | `false` |

### Run

```bash
uvicorn app.main:app --reload --port 8000
ngrok http 8000                     # in another terminal
```

### Connect to the mock provider

```bash
set -a; source .env; set +a         # load the key without printing it
NGROK=https://YOUR-SUBDOMAIN.ngrok-free.dev

# 1. Register (once) — save api_key and webhook_secret into .env
curl -X POST "$MOCK_PROVIDER_URL/api/v1/auth/register" -H "Content-Type: application/json" \
  -d '{"candidate_email": "you@example.com", "candidate_name": "Your Name"}'

# 2. Subscribe both webhooks (events start once both exist)
for t in payments transactions; do
  curl -X POST "$MOCK_PROVIDER_URL/api/v1/webhooks" \
    -H "Authorization: Bearer $MOCK_PROVIDER_API_KEY" -H "Content-Type: application/json" \
    -d "{\"url\": \"$NGROK/webhooks/$t\", \"webhook_type\": \"$t\"}"
done

# 3. Stop
for t in payments transactions; do
  curl -X DELETE "$MOCK_PROVIDER_URL/api/v1/webhooks/$t" -H "Authorization: Bearer $MOCK_PROVIDER_API_KEY"
done
```

ngrok's free tier issues a new URL on every restart; re-subscribe when it changes.

### Tests

```bash
pytest
```

397 tests (13 are strict `xfail` markers for known gaps): signature verification, webhook handling, atomic storage, normalization, status,
the matcher (with real provider cases as fixtures), the engine (including crash safety), the
query API, and end-to-end replays of simulated data (`tests/test_simulated_data.py`):

| Fixture | What it is |
|---|---|
| `tests/fixtures/observed_events.jsonl` | Every event captured from the live mock provider, redeliveries included; replay must reproduce DESIGN §5's results in any delivery order |
| `tests/fixtures/edge_cases.jsonl` | ~150 hand-written deliveries, one edge case per `case`, each with its expected outcome. Lines marked `xfail` are known gaps (strict: they start failing once fixed) |
| `tests/fixtures/simulated_seed42.jsonl` + 50 generated datasets | Seeded random data with ground truth: the matcher must never link money to the wrong payment, and may only skip a real match when it is genuinely ambiguous |

Generate more data, or replay any fixture against a running server:

```bash
python -m scripts.simulate generate --seed 7 --payments 200 --out data/simulated.jsonl
WEBHOOK_SECRET=... python -m scripts.simulate replay tests/fixtures/edge_cases.jsonl --url http://localhost:8000
```

---

## API

Interactive docs: **http://localhost:8000/docs** (Swagger UI) or `/redoc`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhooks/payments` | Receive `payment.created` |
| `POST` | `/webhooks/transactions` | Receive `transaction.settled` |
| `GET` | `/payments` | All payments with status and linked transactions, unmatched transactions (with the reason), and summary statistics |
| `GET` | `/payments/{payment_id}` | One payment with its linked transactions (404 if unknown) |
| `GET` | `/` | Health check |

`GET /payments` query parameters: `status` (`PENDING`, `PARTIALLY_PAID`, `FULLY_PAID`,
`OVERPAID`), `search` (case-insensitive: payment ID, reference, payer name, email),
`page`, `page_size` (default 20, max 100).

Webhook responses: `200` stored / duplicate / unknown event type (acknowledged so the
provider stops retrying), `400` malformed JSON, `401` bad signature, `422` signed but
invalid payload, `500` server has no webhook secret configured.

---

## Project structure

```
app/
  main.py                   app wiring; startup catch-up reconciliation
  config.py                 environment settings (+ .env)
  storage.py                provided JSON storage, made atomic and crash-safe
  api/
    webhooks.py             webhook endpoints
    payments.py             query endpoints
    deps.py                 injectable dependencies
  core/signature.py         canonical-JSON HMAC verification
  domain/
    models.py               inbound event schemas (Decimal amounts)
    schemas.py              API response schemas
    status.py               payment status + fee tolerance (pure)
  services/
    ingest.py               idempotent event storage
    queries.py              read-side views
  reconciliation/
    normalize.py            reference extraction, payer-name normalization (pure)
    matcher.py              matching tiers (pure)
    engine.py               orchestration: runs the matcher, writes links atomically
tests/                      one test module per component
```

The matching and status logic are **pure functions** (no I/O), so the algorithm is tested
in isolation; the engine and API are thin layers around them.

---

## Design decisions

The full reasoning, with the trade-offs considered, is in [`DESIGN.md`](DESIGN.md). The
key decisions:

### Matching was derived from real data, not guessed
Before writing the matcher, I connected to the provider and logged every event
(`data/raw_events.jsonl`). The first 20 payments and 27 transactions showed exact
references, noisy references (`INV REF-2026-6981`, trailing spaces), blank references,
`UNKNOWN-*` placeholders, partial payments, slightly short amounts, and payer-name
variants (`apex trading co`, `Pacific Ventures LLC`). The tiers below are built from
those cases (DESIGN.md §4).

### Matching tiers — most confident first, stop at the first unambiguous answer
| Tier | Rule | Match type |
|---|---|---|
| 1 | Reference in any format: exact match (case/whitespace-insensitive) to a payment's reference; otherwise a known payment reference found inside noisy text | `EXACT_REF` / `EXTRACTED_REF` |
| 1b | Several references → split across them, filling each remaining balance in order | `MULTI_REF` |
| 2 | No usable reference (empty, a placeholder like `UNKNOWN-*`, or a reference matching no payment after the grace period): same normalized payer + amount within fee tolerance of the remaining balance; exactly one candidate | `NAME_AMOUNT` (refunds: `REFUND_NAME_AMOUNT`) |
| 2b | No same-payer candidate at all: payer-name similarity ≥ 0.85 (same numbers in both names) + **exact** amount; exactly one candidate | `FUZZY_NAME_AMOUNT` |
| 3 | Otherwise unmatched, with a recorded reason | — |

Currency must always match. Every link stores its match type and notes, so each match is
explainable.

### A wrong match is worse than a missed one
Money attached to the wrong invoice is harder to undo than an unmatched transaction
waiting for review. Consequences:
- The payer name alone never decides a match; most payers have several open payments.
- A reference that matches no payment **waits** (default 15 minutes after we receive it)
  for that payment to arrive, then falls back to payer + amount — references aren't always
  informative, but falling back immediately put money on the wrong invoice in simulations
  where a transaction arrived before its payment. Empty or placeholder references
  (`UNKNOWN-*`, `N/A`, …) fall back immediately. A periodic run (every 60 s) re-checks
  waiting transactions even when no webhooks arrive.
- References are matched in **any format**. The observed `REF-2026-1234` shape is used
  only to help find a reference inside noisy text, never as a requirement.
- Ambiguity (two equally good candidates) means no match.
- Fuzzy name matching requires an *exact* amount and only runs when no payer has the
  exact name, so it can't redirect money to a similar-but-different payer. Its 0.85
  threshold is measured: distinct real payers score at most 0.56; misspellings 0.85–0.97.

### Links are the single source of truth
Each reconciliation link records the amount it allocates, so one transaction can cover
several payments and several transactions can cover one. A payment's `received_amount`
and `status` are **recalculated from its links**, never incremented, and cached on the
record for cheap reads. At this scale recalculating removes any chance of drift; storing
and updating totals incrementally is the documented path at larger scale (DESIGN.md §5).

### Payment status and fee tolerance
| Status | Rule |
|---|---|
| `PENDING` | nothing received (or refunded back to zero) |
| `PARTIALLY_PAID` | received below expected minus the fee allowance |
| `FULLY_PAID` | received within the fee allowance of expected, up to expected |
| `OVERPAID` | received more than expected |

The allowance is the larger of `FEE_TOLERANCE_PERCENT` and `FEE_TOLERANCE_FIXED`, applied
below the expected amount only (fees reduce what arrives). Default 2%, because
under-calling "fully paid" is the safer error. Refunds are negative links and reduce
`received_amount`, so status can move back (e.g. `OVERPAID` → `FULLY_PAID`).

### Webhooks: verify, deduplicate, store, acknowledge — then reconcile
- **Signatures** are verified over canonical JSON (sorted keys, compact, `sandbox_id`
  removed) with a constant-time comparison. Missing secret → reject (fail closed).
- **Idempotency** is keyed on the provider's IDs. The provider resends duplicates about
  10 minutes later that differ only in `timestamp`, so their bytes and signatures
  differ — hashing the body would have stored them twice.
- **Fast 200:** matching runs as a background task after the response. Measured
  locally: median 3–7 ms, max 40 ms over 900 webhooks.
- **Late arrivals:** every run re-checks all unmatched transactions, so a transaction
  that arrives before its payment is matched when the payment lands. A run at startup
  catches anything stored but not yet reconciled.

### Crash safety
- The provided storage truncated the file before writing; a crash mid-write produced
  invalid JSON, which the loader discards — losing all data. Saves now write a temp file,
  `fsync`, then atomically replace the original.
- Each match (links, transaction flag, payment totals) is one atomic write, and a
  transaction that already has a link is never linked again, so a crash can't
  double-count money.

### Money and API conventions
- `Decimal` throughout; amounts are JSON strings with two decimals, as the provider sends them.
- Summary totals are grouped by currency — never summed across currencies.
- The summary in `GET /payments` covers all data, independent of filters and paging.
- `outstanding_amount` is 0 once a payment is `FULLY_PAID`/`OVERPAID`.

### No AI in the matching path
Deterministic rules are auditable, reproducible, testable, and free to run. An LLM would
hide the algorithm, add cost, latency and an API-key dependency, and give
non-reproducible results on financial data.

---

## What I would improve with more time

**Reliability and scale**
- Replace the JSON file with Postgres: unique constraints for idempotency instead of
  in-process locks, transactions instead of whole-file rewrites (today each write is
  O(file size); webhook latency grew from ~1.4 ms to ~17 ms over 900 events).
- Move reconciliation to a durable queue/worker so work survives crashes and doesn't
  compete with request handling for the CPU.
- Coalesce reconciliation runs during bursts (one rerun flag instead of one full run per
  event), and re-check only transactions related to a new payment via indexes on
  reference and payer, instead of all unmatched × all payments.
- Store totals incrementally in the same DB transaction as each link, with
  recalculation kept as a scheduled audit that reports drift.

**Matching**
- Fuzzy *reference* matching (edit distance) — deferred because no typo'd references
  appeared in the data.
- A manual review endpoint to confirm, reject or re-assign matches, plus an audit trail
  of status transitions.
- LLM-assisted *suggestions* for transactions the rules leave unmatched, for a human to
  approve — never automatic links.
- Resolve the open questions in DESIGN.md §7, chiefly whether the 1.5–4.8% short payments
  are fees or partial payments (it decides the right default tolerance).

**API and operations**
- Rate limiting, cursor-based pagination, and API authentication for the query endpoints.
- Docker / docker-compose.
- Structured logs and metrics (match rate, unmatched amount, webhook latency, signature
  failures).
- Retire the provided single-ID `matched_to_payment_id` field in favour of
  `matched_payment_ids`.

**Known limitations today:** single process only (locks are in-process); a tolerance
change applies on restart and doesn't redo existing links; refunds and multi-payment
splits are tested but haven't yet appeared in provider data.

---

## AI usage declaration

I used **Claude Code** (Anthropic, Claude Opus 5.5) for three things: **writing code**,
**analyzing the raw webhook data**, and **generating simulated test data**. The architecture and the design decisions,
including how to weigh each trade-off, were mine.

**What I did myself: architecture and decisions**
- Set the overall architecture: pure matching and status logic with thin I/O layers,
  links as the single source of truth, and atomic storage writes.
- Made the trade-off decisions, including:
  - recalculating status from links instead of storing it (no drift vs. read cost at scale);
  - running reconciliation as a background task instead of inline or in a separate worker
    (fast acknowledgement vs. durability);
  - preferring a missed match over a wrong one, which led to the ambiguity rules, the
    reference grace period, and requiring an exact amount for fuzzy name matches;
  - keeping AI/LLMs out of the matching path.
- Challenged assumptions instead of accepting them (for example, that transactions can
  arrive before their payments, which the brief doesn't say). That's why every assumption
  is listed and checked against real data in DESIGN.md §6.
- Registered with the mock provider, ran the server and ngrok, subscribed the webhooks,
  and investigated problems seen in the live logs.

**What AI did**
- **Wrote code:** the application code and tests, built from the design I set, one change
  at a time, with each change reviewed by me.
- **Analyzed the raw webhook data:** worked through `data/raw_events.jsonl` to classify
  the reconciliation cases (noisy, missing and placeholder references; partial payments;
  short payments; payer-name variants), compare duplicate deliveries, and measure
  payer-name similarity. I used that analysis to decide the matching tiers and thresholds.
- **Generated simulated data:** built the seeded generator (`scripts/simulate.py`) and the
  fixtures in `tests/fixtures/`: about 150 hand-written edge cases, each with its expected
  outcome, plus 50+ random datasets with known correct answers. These cover cases the live
  provider hadn't sent yet (refunds, multi-reference transactions, other currencies,
  out-of-order delivery).

**How I validated the AI's output**
- **Tests:** 397 automated tests, many using real provider events as fixtures, plus
  replays of seeded simulated data with known correct answers. The matcher must never
  link money to the wrong payment.
- **Against live data:** checked every stored event and match outcome (24 of 27
  transactions linked as expected; the three `UNKNOWN-*` transactions correctly left
  unmatched).
- **Evidence, not assumptions:** logged rejected webhooks to confirm the 401s were
  deliberately bad signatures; compared duplicate payloads to confirm only `timestamp`
  differs; benchmarked webhook latency; derived the fuzzy-name threshold from similarity
  scores across all real payers.
- **Review caught real defects**, each now covered by a test: a crash window that could
  link a transaction twice, the provided storage losing all data on an interrupted
  write, a settled payment still showing a balance due, and reference matching that only
  recognized the one format seen in the sample data.
