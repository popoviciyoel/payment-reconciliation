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
    status.py              pure: amounts + tolerance -> status        ✅
  services/
    ingest.py              idempotent persistence of events          ✅
  reconciliation/
    normalize.py           reference / payer-name normalization      ✅
    matcher.py             pure: transaction + candidates -> links   ✅
    engine.py              orchestration, re-scan, writes links      ✅
tests/                                                               ✅ (signature, webhooks, normalize, status, matcher, engine)
```

### Webhook request flow ✅

```
request
  -> verified_payload   parse JSON, verify X-Webhook-Signature   (400 / 401 / 500)
  -> event_type check   wrong type -> 200 "ignored"
  -> schema validation  Pydantic, Decimal amounts                 (422)
  -> ingest             atomic dedupe-then-insert                 (200 "accepted" | "duplicate")
  -> reconcile          background task, newly stored events only
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

### Atomic persistence ✅
- **Crash-safe saves.** The provided `_save()` opened the file with `"w"` (truncating it)
  before writing; a crash mid-write left invalid JSON, which `_load()` discards — losing
  all data. Saves now write a temp file, `fsync`, then `os.replace()` it over the original,
  so the file is always either the old or the new complete version. A failed save reloads
  memory from disk so memory never runs ahead of what was persisted.
- **One atomic write per match.** `storage.record_match()` applies a match's links, the
  transaction's matched flag and the affected payments' projections under one lock and one
  save, after validating inputs. Previously these were separate saves, and a crash between
  "link written" and "transaction flagged" would make the next run link it again
  (double-counting money).
- **Defence in depth.** The engine treats any transaction that already has a link as matched
  regardless of its flag, repairs the flag, and never links it twice.

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

## 4. Observed provider data

Snapshot from the live mock provider on 2026-11-08 (20 unique payments, 27 unique
transactions, all USD), captured in `data/raw_events.jsonl`. The matching design in §5 is
derived from this, not from guesses.

### Reconciliation cases

| Case | Examples | Count |
|---|---|---|
| **Exact reference** | `ORD-2026-9137` → payment `ORD-2026-9137` | 20 of 27 |
| **Noisy reference** | `INV REF-2026-6981` (prefix), `REF-2026-9392 ` (trailing space) | 2 |
| **Missing reference** | `''` 4153.15 Apex = ORD-2026-2811 exactly; `''` 356.13 Stellar = ORD-2026-4149 exactly | 2 |
| **Placeholder reference** | `UNKNOWN-DD9DED6E` (96.94 Pinnacle), `UNKNOWN-2B943F38` (381.86 Apex), `UNKNOWN-680664DF` (207.00 Vertex) | 3 |
| **Partial payments summing exactly** | ORD-2026-5866: 428.68 + 730.88 = 1159.56; ORD-2026-3701: 2804.18 + 1427.64 = 4231.82; REF-2026-2486: 53.80 + 75.87 + 114.31 = 243.98 | 3 payments |
| **Slightly short** (fee or partial?) | REF-2026-1047 2544.75 / 2583.76 (−1.5%); REF-2026-8710 1348.80 / 1410.45 (−4.4%); REF-2026-9857 2735.86 / 2874.77 (−4.8%) | 3 |
| **Payer name variants** | `Horizon Partners Inc`, `apex trading co`, `Pacific Ventures LLC` | 3 |
| **Same payer, many payments** | Pinnacle ×3; Vertex, Horizon, Prime, Metro, Quantum, Apex ×2 | most payers |
| Refunds / multi-reference / other currency | — | 0 so far |

### What this implies

- **Reference is the primary signal but needs extraction.** Every real reference has the
  shape `(REF|ORD|PAY|INV)-YYYY-NNNN`; extracting that pattern fixes both noisy cases. No
  typo'd references were observed, so edit-distance matching is **not** needed yet.
- **Payer name alone must never decide a match.** Most payers have several open payments.
  The `UNKNOWN-*` transactions match no outstanding balance; forcing a name match would
  misallocate money.
- **Name + exact amount is reliable when the reference is missing** — both empty-reference
  transactions match a single payment to the cent.

### Delivery behaviour

| Observation | Evidence |
|---|---|
| Payments arrive as a batch first (20), then transactions; later payment deliveries were all duplicates | Raw log ordering |
| Duplicates are identical **except `timestamp`** (so raw bytes and signature differ) | `pay_a7c17401fb7c6557`, `pay_53b6eaa7807eb6ee` |
| Payments were redelivered ~10 minutes after the original | 2 samples (9m54s, 9m51s) |
| Occasional webhooks carry an invalid signature on an otherwise normal ASCII payload; never redelivered validly | `txn_6b40e8cbf37606b8` — treated as a deliberate forgery test |
| Real payment payloads omit `description` | All 20 payments |

---

## 5. Reconciliation design ✅

### Data model
- **Reconciliation links are the single source of truth** for what matched what.
  Each link carries its own allocated `amount` and a `match_type` explaining why.
- This supports one transaction → many payments (the provided `matched_to_payment_id`
  field on transactions can only hold one ID, so it is not relied on).
- Payment **status and received amount are a projection of the links**: always recomputed
  from scratch (never incremented) and cached on the payment record. All projections are
  recomputed at startup, so a tolerance change applies on restart (Q5).

### Matching tiers
Derived from §4. Evaluated in order; stop at the first tier that yields an
**unambiguous** result. Currency must always match.

| Tier | Rule | `match_type` |
|---|---|---|
| 1 | Uppercase/trim, regex-extract `(REF\|ORD\|PAY\|INV)-\d{4}-\d{4}`; one extracted ref matching a payment → link full amount | `EXACT_REF` / `EXTRACTED_REF` (if cleanup was needed) |
| 1b | Several extracted refs → allocate across them, filling each remaining balance in order; remainder to the last | `MULTI_REF` (not yet observed) |
| 2 | No usable ref (empty or `UNKNOWN-*`): same currency + normalized payer name (casefold, strip `inc`/`llc`/`co`/`corp`) + amount equals remaining balance or expected amount within tolerance; **only if exactly one candidate** | `NAME_AMOUNT` |
| 3 | Nothing confident → leave unmatched, surfaced in the unmatched list | — |

- In tier 1 the reference wins even if the payer name differs; name similarity is
  recorded on the link as a confidence signal, not used as a gate.
- Fuzzy (edit-distance) reference matching is deferred until the data shows typos.
- Links are immutable once made, so results are reproducible and explainable.

### Status
| Status | Rule |
|---|---|
| `PENDING` | no linked amount |
| `PARTIALLY_PAID` | 0 < received < expected − tolerance |
| `FULLY_PAID` | received within tolerance of expected |
| `OVERPAID` | received > expected (+ tolerance) |

Fee tolerance is configurable via `FEE_TOLERANCE_PERCENT` / `FEE_TOLERANCE_FIXED`
(allowance = the larger of the two). Default: **2%** — under-calling "fully paid" is the safer error. The three
"slightly short" payments in §4 (−1.5% to −4.8%) are ambiguous: if follow-up transactions
for exactly 39.01, 61.65 or 138.91 arrive, they were partials, not fees (see Q2).

### Status: recalculate vs store

**Decision (current scale): recalculate from links.** A payment's `received_amount` and
`status` are rebuilt from scratch from its links whenever a link is added and at startup.
The result is cached on the payment record only so reads and status filters are cheap;
it is never incremented or edited independently.

Why recalculate:
- **Cannot drift.** Links are the single source of truth; no bug, partial write or manual
  data fix can leave a stale total that disagrees with them — the key property for money.
- **Rule changes apply everywhere.** A new fee tolerance is reflected on the next
  recalculation (restart), with no separate backfill or mixed old/new statuses.
- **Simpler, safer writes.** Creating a link is a single write; no "also update the
  running total" step that must be transactional with it.
- **Self-correcting.** Removing a bad link fixes the status automatically.
- **Pure and testable.** Status is `f(expected, links, tolerance)` with no
  order-dependent hidden state.

What storing would buy at scale (and why it's deferred):
- O(1) reads and indexed filtering/sorting/counting by status without recomputing.
- Status *transitions* (old → new) for notifications and audit history.
- Read cost independent of how many links a payment has.

Cost of recalculating here is negligible (tens of payments, a handful of links each).
The scale-out path is in §8.

### Refunds
Negative transactions attach to the payment they reverse (same matching tiers) and
reduce its received amount.

### Late arrivals
When a new payment arrives, previously unmatched transactions are re-evaluated against
it. Cheap to do and harmless if it never triggers.

### Execution
Matching runs as a FastAPI background task after the 200, serialized behind one lock so
two events can't race on the same payment (Q4). Each run re-evaluates **all** unmatched
transactions in settlement order, which is what picks up late arrivals. A startup run
catches anything stored but never reconciled (e.g. a crash after the 200).

#### Real-time vs background: trade-off

| | **Inline** (in the webhook request) | **Current:** in-process background task after the 200 | **Separate job** (queue worker / scheduled batch) |
|---|---|---|---|
| Status freshness | Up to date before the 200 | Milliseconds later | Seconds–minutes later |
| Webhook latency | Includes matching time | Fast | Fast |
| Survives a crash | Yes — fail with 5xx, provider retries | No — mitigated by startup catch-up + re-checking all unmatched each run | Yes — queue holds the work |
| Extra infrastructure | None | None | Queue, workers, monitoring |
| Scales independently | No | No (same process) | Yes |

- **Inline wins on:** read-your-writes consistency, retries for free (a failure becomes a
  5xx the provider resends), and simplicity.
- **A separate job wins on:** fast, reliable acknowledgement isolated from matcher bugs or
  slowness; absorbing bursts and scaling workers independently; better batch decisions
  (seeing more data before calling something ambiguous); proper retries, dead-letter
  queues and replay after rule changes. Cost: eventual consistency visible to API users
  and more infrastructure.
- **Why the current choice:** fast acks and near-real-time status with no extra
  infrastructure. Its weakness — losing queued work on a crash — is covered because every
  run re-checks all unmatched transactions and a catch-up runs at startup. Duplicate
  links after a crash are prevented by atomic writes (§3).

### Result on observed data
All 27 transactions in §4: 24 linked (21 `EXACT_REF`, 1 `EXTRACTED_REF`, 2 `NAME_AMOUNT`),
3 `UNKNOWN-*` left unmatched with a reason. Payments: 18 `FULLY_PAID` (incl. REF-2026-1047
at −1.5%), 2 `PARTIALLY_PAID` (REF-2026-8710, REF-2026-9857, outside the 2% tolerance).

---

## 6. Assumptions

Things the implementation relies on that the README does **not** state. Each should be
confirmed or revised against real data.

| # | Assumption | Basis | How to verify |
|---|---|---|---|
| A1 | Provider IDs (`payment_id`, `transaction_id`) are stable across redeliveries and safe as idempotency keys | ✅ **Confirmed:** redeliveries reuse the ID with identical business fields; only `timestamp` changes, so content/signature-based dedupe would fail | — |
| A2 | Non-ASCII characters may be signed either escaped (`\uXXXX`) or raw UTF-8; we accept both | README doesn't specify `ensure_ascii`; no non-ASCII payloads observed yet | Check which form validates on a real non-ASCII payload, then narrow |
| A3 | Events with an unexpected `event_type` should be acknowledged (200) and ignored, not rejected | Avoids pointless retries; none observed yet | Watch logs for any such events |
| A4 | Ordering between the two streams is **not** guaranteed (a transaction may arrive before its payment) | Observed so far: payments batch first, so not yet seen; kept as a cheap safeguard | Compare arrival order of linked pairs in raw log |
| A5 | Amounts are always sent as JSON strings, never numbers | ✅ **Confirmed** across all observed events | — |
| A6 | A transaction only matches payments in the same currency | Matching across currencies would need FX data we don't have | Check for cross-currency near-matches |
| A7 | Fees only *reduce* the received amount (so tolerance is applied below the expected amount) | README: "fee deductions" | Inspect under-payments clustered just below expected |
| A8 | A wrong match is worse than a missed one | Financial-data norm | Design choice — no verification needed |

---

## 7. Open questions

Questions we would ask the assignment authors; current handling noted for each.

| # | Question | Current handling |
|---|---|---|
| Q1 | The README uses two provider hosts (Lightsail URL vs `mock-api.advancehq.com`). Which is canonical? | Registration and webhook subscription both worked on the Lightsail host; using that via `MOCK_PROVIDER_URL` |
| Q2 | What fee tolerance is expected — percentage, fixed, or both? Typical magnitude? Are the −1.5% to −4.8% short payments fees or partials? | Both configurable, default 2%; watching for follow-up transactions (§4) |
| Q3 | For a payment that's `OVERPAID` and later receives a refund, should status move back (e.g. to `FULLY_PAID`)? | Yes — status is derived from current net received amount |
| Q4 | Run reconciliation inline in the webhook request, or as a background task after the 200? | **Decided:** background task, serialized by a lock |
| Q5 | Derive payment status at read time vs store it and recompute on change? | **Decided:** recomputed from links on every change and at startup; cached on the record |
| Q6 | Should a refund with no identifiable original payment be surfaced separately from other unmatched transactions? | Plan: shown in unmatched list with its negative amount |
| Q7 | Can a single transaction partly match one payment and leave a remainder unallocated? | No — every matched transaction is fully allocated; a multi-reference remainder goes to the last referenced payment (may make it `OVERPAID`) |
| Q8 | Are the `PENDING / PARTIALLY_PAID / FULLY_PAID / OVERPAID` statuses exhaustive, or is something like `REFUNDED` expected? | Using only the four specified |

---

## 8. Future improvements

- Replace JSON file storage with a real database (Postgres) with unique constraints for
  idempotency instead of an in-process lock.
- Move reconciliation to a durable queue so it survives restarts and scales beyond one process.
- **Scaling status and matching** (see §5 "Status: recalculate vs store"). Today each run
  rebuilds all payment totals from all links, O(payments + links), and re-checks every
  unmatched transaction against every payment, O(unmatched × payments). At scale:
  - store `received_amount`/`status` and update them incrementally, in the same DB
    transaction as the link insert;
  - record status transitions for notifications and audit history;
  - index payments by normalized reference and by (normalized payer, currency); when a
    payment arrives, re-check only unmatched transactions sharing its reference or payer;
  - keep from-scratch recalculation as a scheduled audit job that reports drift, and
    as an explicit backfill when the fee tolerance changes.
- Manual review endpoint to confirm/reject low-confidence matches.
- LLM-assisted *suggestions* (never auto-links) for transactions the rules leave unmatched.
- Pagination, search and rate limiting on the query API (bonus tasks).
- Docker / docker-compose.
