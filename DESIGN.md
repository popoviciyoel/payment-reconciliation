# Design Notes

Working design document for the Payment Reconciliation Service. It records what is
built, what is planned, and — explicitly — which parts rest on assumptions rather than
on the assignment README or observed data.

**Status legend:** ✅ built & tested · 🟡 planned · ❓ open question

---

## 1. Goals

Receive two webhook streams from the mock provider — **payments** (expected money) and
**transactions** (money that actually settled) — and reconcile them: link transactions
to payments, track how much of each payment has been received, and expose the result
through a query API.

Guiding principles:

- **Correctness over cleverness.** A missed match is recoverable; a wrong match on money
  is not. When the matcher is unsure, it leaves the transaction unmatched.
- **Deterministic, explainable matching.** Every link records *why* it was made.
- **Pure core, thin edges.** Matching and status logic are pure functions with no I/O,
  so the algorithm can be unit-tested in isolation.
- **Working first, then improve** (per the README's priority guide).

---

## 2. Architecture

```
app/
  main.py                  app wiring, routers, logging              ✅
  config.py                env settings (+ .env loading)             ✅
  storage.py               provided JSON file storage                ✅ (provided)
  api/
    deps.py                injectable dependencies (storage)         ✅
    webhooks.py            POST /webhooks/{payments,transactions}    ✅
    payments.py            GET /payments, GET /payments/{id}         🟡
  core/
    signature.py           canonical JSON + HMAC verification        ✅
  domain/
    models.py              inbound event schemas (Decimal amounts)   ✅
    status.py              pure: amounts + tolerance -> status        🟡
  services/
    ingest.py              idempotent persistence of events          ✅
  reconciliation/
    normalize.py           reference / payer-name normalization      🟡
    matcher.py             pure: transaction + candidates -> links   🟡
    engine.py              orchestration, re-scan, writes links      🟡
tests/                                                               ✅ (signature, webhooks)
```

### Webhook request flow ✅

```
request
  -> verified_payload   parse JSON, verify X-Webhook-Signature   (400 / 401 / 500)
  -> event_type check   wrong type -> 200 "ignored"
  -> schema validation  Pydantic, Decimal amounts                 (422)
  -> ingest             atomic dedupe-then-insert                 (200 "accepted" | "duplicate")
  -> [reconcile]        🟡 triggered after storage
```

| Status | Meaning |
|---|---|
| 200 | stored, duplicate, or unrecognised `event_type` (acknowledged, not processed) |
| 400 | body is not a JSON object |
| 401 | missing or invalid signature |
| 422 | correctly signed payload that fails schema validation |
| 500 | server misconfigured (no `WEBHOOK_SECRET`) |

---

## 3. Decisions made

### Signature verification ✅
- Signature is computed over **canonical JSON** of the *parsed* body (keys sorted,
  `separators=(",", ":")`, `sandbox_id` removed) — never the raw bytes. Tests send
  pretty-printed bodies to prove this.
- Comparison uses `hmac.compare_digest`.
- **Fail closed:** if `WEBHOOK_SECRET` is unset, every webhook is rejected (500) rather
  than accepted unverified.

### Idempotency ✅
- Key = the provider's own ID (`payment_id` / `transaction_id`).
- Check-and-insert runs under a single lock, so concurrent redeliveries can't both insert.
- A duplicate returns **200** so the provider stops retrying.
- A duplicate with **different data** keeps the original and logs a warning — silently
  overwriting a financial record is worse than flagging it.

### Money handling ✅
- Amounts arrive as strings and are parsed to `Decimal`; floats are never used.
- Amounts are persisted as strings to keep the JSON file lossless.

### Raw event log ✅
- Every newly accepted, verified payload is appended to `data/raw_events.jsonl`
  (git-ignored). The README says to derive the matching logic "based on the data you
  receive", so we capture real data before tuning the matcher.

### No AI/LLM in the matching path
Deterministic rules are auditable, reproducible, testable and free to run; an LLM would
hide the algorithm (which is being evaluated), add cost/latency/an API key dependency,
and produce non-reproducible results on financial data. Fuzzy string similarity
(`difflib` / `rapidfuzz`) covers name and reference variants. An LLM-assisted *review
queue* for unmatched transactions is listed under future improvements.

---

## 4. Planned reconciliation design 🟡

### Data model
- **Reconciliation links are the single source of truth** for what matched what.
  Each link carries its own allocated `amount` and a `match_type` explaining why.
- This supports one transaction → many payments (the provided `matched_to_payment_id`
  field on transactions can only hold one ID, so it is not relied on).
- Payment **status and received amount are derived from links**, not maintained as an
  independent copy that could drift. *(Pending confirmation — see Q5.)*

### Matching tiers
Evaluated in order; stop at the first tier that yields an **unambiguous** result.
Currency must always match.

| Tier | Rule | `match_type` |
|---|---|---|
| 1 | Normalized reference equal (uppercase, strip spaces/dashes) | `EXACT_REF` |
| 2 | Reference field contains several references → split amount across them | `MULTI_REF` |
| 3 | Near-miss reference (edit distance ≤ 1–2) confirmed by similar payer name | `FUZZY_REF` |
| 4 | No reference: normalized payer name + amount within tolerance + open balance, **only if exactly one candidate** | `NAME_AMOUNT` |
| 5 | Nothing confident → leave unmatched | — |

Tiers 2–4 are deliberately provisional: their exact rules will be set after inspecting
real events in `raw_events.jsonl`.

### Status
| Status | Rule |
|---|---|
| `PENDING` | no linked amount |
| `PARTIALLY_PAID` | 0 < received < expected − tolerance |
| `FULLY_PAID` | received within tolerance of expected |
| `OVERPAID` | received > expected (+ tolerance) |

Fee tolerance will be configurable (percentage and/or fixed amount) via env settings.

### Refunds
Negative transactions attach to the payment they reverse (same matching tiers) and
reduce its received amount.

### Late arrivals
When a new payment arrives, previously unmatched transactions are re-evaluated against
it. Cheap to do and harmless if it never triggers.

### Execution
Matching runs after the event is stored, serialized behind one lock so two events can't
race on the same payment. *(Inline vs background task — see Q4.)*

---

## 5. Assumptions

Things the implementation relies on that the README does **not** state. Each should be
confirmed or revised against real data.

| # | Assumption | Basis | How to verify |
|---|---|---|---|
| A1 | Provider IDs (`payment_id`, `transaction_id`) are stable across redeliveries and safe as idempotency keys | Standard webhook practice | Look for repeated IDs in raw log |
| A2 | Non-ASCII characters may be signed either escaped (`\uXXXX`) or raw UTF-8; we accept both | README doesn't specify `ensure_ascii` | Check which form validates on a real non-ASCII payload, then narrow |
| A3 | Events with an unexpected `event_type` should be acknowledged (200) and ignored, not rejected | Avoids pointless retries | Watch logs for any such events |
| A4 | Ordering between the two streams is **not** guaranteed (a transaction may arrive before its payment) | Two independent streams; README is silent | Compare arrival order of linked pairs in raw log |
| A5 | Amounts are always sent as JSON strings, never numbers | README examples | Schema accepts both; inspect raw log |
| A6 | A transaction only matches payments in the same currency | Matching across currencies would need FX data we don't have | Check for cross-currency near-matches |
| A7 | Fees only *reduce* the received amount (so tolerance is applied below the expected amount) | README: "fee deductions" | Inspect under-payments clustered just below expected |
| A8 | A wrong match is worse than a missed one | Financial-data norm | Design choice — no verification needed |

---

## 6. Open questions

Questions we would ask the assignment authors; current handling noted for each.

| # | Question | Current handling |
|---|---|---|
| Q1 | The README uses two provider hosts (Lightsail URL vs `mock-api.advancehq.com`). Which is canonical? | Configurable via `MOCK_PROVIDER_URL`; will confirm on first call |
| Q2 | What fee tolerance is expected — percentage, fixed, or both? Typical magnitude? | Will make both configurable with a conservative default |
| Q3 | For a payment that's `OVERPAID` and later receives a refund, should status move back (e.g. to `FULLY_PAID`)? | Yes — status is derived from current net received amount |
| Q4 | Run reconciliation inline in the webhook request, or as a background task after the 200? | Leaning background task (README asks for a fast 200); undecided |
| Q5 | Derive payment status at read time vs store it and recompute on change? | Leaning derive-on-read (single source of truth); undecided |
| Q6 | Should a refund with no identifiable original payment be surfaced separately from other unmatched transactions? | Plan: shown in unmatched list with its negative amount |
| Q7 | Can a single transaction partly match one payment and leave a remainder unallocated? | Plan: yes — links carry their own amounts; remainder reported |
| Q8 | Are the `PENDING / PARTIALLY_PAID / FULLY_PAID / OVERPAID` statuses exhaustive, or is something like `REFUNDED` expected? | Using only the four specified |

---

## 7. Future improvements

- Replace JSON file storage with a real database (Postgres) with unique constraints for
  idempotency instead of an in-process lock.
- Move reconciliation to a durable queue so it survives restarts and scales beyond one process.
- Manual review endpoint to confirm/reject low-confidence matches.
- LLM-assisted *suggestions* (never auto-links) for transactions the rules leave unmatched.
- Pagination, search and rate limiting on the query API (bonus tasks).
- Docker / docker-compose.
