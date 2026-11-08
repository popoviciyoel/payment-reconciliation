# Design Notes

What was built, why, and which parts rest on assumptions rather than on the brief
([`ASSIGNMENT.md`](ASSIGNMENT.md)) or observed data. Setup, the API and the project layout
are in the [README](README.md).

**Start here:** §3 (what the provider actually sent) and §4 (how matching works, and why).
§5–6 list assumptions and open questions.

---

## 1. Principles

- **A wrong match is worse than a missed one.** A missed match waits for review; money on
  the wrong invoice is hard to undo. When unsure, the matcher leaves the transaction unmatched.
- **Deterministic and explainable.** Every link records its match type and why it was made.
- **Pure core, thin edges.** Matching and status are pure functions with no I/O, so the
  algorithm is tested in isolation. The engine and API are thin layers around them.
- **Built from real data.** Every event the provider sent was logged before the matcher
  was written (§3).

---

## 2. Ingestion and storage

```
request
  -> verify signature   canonical JSON + HMAC                     (400 / 401 / 429 / 500)
  -> event_type check   wrong type -> 200 "ignored"
  -> validate           Pydantic, Decimal amounts                 (422)
  -> ingest             atomic dedupe-then-insert                 (200 "accepted" | "duplicate")
  -> reconcile          background task after the response
```

- **Signatures** are computed over canonical JSON of the *parsed* body (sorted keys, compact
  separators, `sandbox_id` removed), never the raw bytes, and compared in constant time.
  With no `WEBHOOK_SECRET`, every webhook is rejected. *Observed:* the provider sometimes
  sends a payload with an invalid signature and never redelivers it (e.g.
  `txn_6b40e8cbf37606b8`), a deliberate forgery test; `401` is correct.
- **Idempotency** is keyed on the provider's IDs. *Observed:* duplicates differ only in
  `timestamp` (e.g. `pay_a7c17401fb7c6557`), so a hash of the body or signature would store
  them twice. A duplicate returns `200`; one with different data keeps the original and
  logs a warning.
- **Crash-safe storage.** The provided `_save()` truncated the file before writing, so a
  crash mid-write lost all data. Saves now write a temp file, `fsync`, and `os.replace()`
  it. Each match (links, transaction flag, payment totals) is one atomic write, and a
  transaction that already has a link is never linked again, so a crash can't count money
  twice.
- **Money** is `Decimal` throughout and stored as strings; floats are never used.
- **Raw event log.** Every verified delivery, duplicates included, goes to
  `data/raw_events.jsonl` with its outcome and receipt time. The brief says to derive
  matching "based on the data you receive", so real data came first.
- **Rate limiting** (`app/core/rate_limit.py`): an in-memory token bucket per client on the
  query endpoints (60/min, bursts of 20). Correctly signed webhooks are never limited, since
  a `429` would make the provider retry or drop events; only failed signatures draw from a
  separate bucket. The trap: behind ngrok, every request comes from the proxy, but
  `X-Forwarded-For` can be faked. It is trusted only from addresses in `TRUSTED_PROXIES`,
  and then only its right-most entry. Written in-house (not `slowapi`) to make "limit only
  failed signatures" simple and the clock injectable for tests.

---

## 3. Observed provider data

Snapshot from the live mock provider on 2026-11-08: 20 payments and 27 transactions, all
USD, committed as `tests/fixtures/observed_events.jsonl`.

| Case | Examples | Count |
|---|---|---|
| **Exact reference** | `ORD-2026-9137` → payment `ORD-2026-9137` | 20 of 27 |
| **Noisy reference** | `INV REF-2026-6981`, `REF-2026-9392 ` (trailing space); later `PAY 2026 9788` | 2 (+1) |
| **Missing reference** | `''` 4153.15 Apex = ORD-2026-2811 exactly; `''` 356.13 Stellar = ORD-2026-4149 exactly | 2 |
| **Placeholder reference** | `UNKNOWN-DD9DED6E`, `UNKNOWN-2B943F38`, `UNKNOWN-680664DF` | 3 |
| **Partial payments** | ORD-2026-5866: 428.68 + 730.88 = 1159.56; REF-2026-2486: three parts = 243.98 | 3 payments |
| **Slightly short** (fee or partial?) | REF-2026-1047 −1.5%; REF-2026-8710 −4.4%; REF-2026-9857 −4.8% | 3 |
| **Payer name variants** | `Horizon Partners Inc`, `apex trading co`, `Pacific Ventures LLC` | 3 |
| **Same payer, many payments** | Pinnacle ×3; most others ×2 | most payers |
| Multi-reference / other currency | — | 0 so far |

**Refunds arrived after the snapshot.** The live store later held two, both with exact
references: `PAY-2026-3316` −522.01 and `ORD-2026-7252` −479.73. Both linked as
`EXACT_REF` and left their payments `FULLY_PAID` with a lower amount due (Q1). They are not
in the fixture yet; tests cover the same shape with hand-written events.

What this implies:
- **The reference is the main signal, but sometimes has to be found inside noisy text.**
  Every observed reference looked like `(REF|ORD|PAY|INV)-YYYY-NNNN`, but the matcher
  doesn't depend on that shape: other providers will differ. Separator variants
  (`PAY 2026 9788`, `INV_2026_1234`) are normalized to dashes. No typos were observed; the
  brief asks for fuzzy references anyway, so tier 1c handles them under strict conditions.
- **The payer name alone must never decide.** Most payers have several open payments, and
  the `UNKNOWN-*` transactions fit no open balance.
- **Name + exact amount is reliable without a reference:** both empty-reference
  transactions match a single payment to the cent.
- **`payer_account_last_four` carries no signal here:** 27 transactions, 27 different
  values, and the same payer uses a new one each time. Even if stable, four digits collide
  easily (~39% chance of a shared value among 100 accounts), so it could only support a
  match (Q11).

---

## 4. Reconciliation

### Data model
**Links are the single source of truth.** Each link stores the amount it allocates and its
match type, so one transaction can cover several payments and several transactions one
payment. A payment's `received_amount`, `amount_due` and `status` are recalculated from its
links on every change and at startup, then cached on the record for cheap reads. They are
never incremented, so they can't drift, and a new fee tolerance applies everywhere on
restart. At larger scale, storing totals incrementally is the path (§7).

### Matching tiers
Tried in order; the first unambiguous answer wins. Currency must always match. Listed in
the order they run (2c is tried before 2b).

| Tier | When | Rule | `match_type` |
|---|---|---|---|
| 1 | Reference equals a payment's, or a known one is found in noisy text | Link to that payment | `EXACT_REF` / `EXTRACTED_REF` |
| 1b | Several references found | Split across them, filling each balance in order | `MULTI_REF` |
| 1c | Reference matches no payment, after the grace period | Within N typos of exactly one open payment's reference; payer agrees; amount fits | `FUZZY_REF` |
| 2 | No usable reference | Same payer + amount within fee tolerance; exactly one candidate | `NAME_AMOUNT` / `REFUND_NAME_AMOUNT` |
| 2c | Same payer, but no single payment fits | Amount **exactly** equals 2–3 open balances; exactly one combination | `MULTI_NAME_AMOUNT` |
| 2b | No same-payer candidate at all | Similar name + **exact** amount; exactly one candidate | `FUZZY_NAME_AMOUNT` |
| 3 | Nothing confident | Leave unmatched, with the reason | — |

**1. Reference, any format.** Exact equality after trimming and uppercasing comes first.
Otherwise the text is searched for known payment references as whole tokens (4+ characters
with a digit, so `PAYMENT` never matches ordinary words) and for well-formed
`(REF|ORD|PAY|INV)-YYYY-NNNN`. The reference wins even if the payer name differs; name
similarity is recorded on the link as confidence. An earlier version only recognised the
observed shape, so `INV-2024-001` (the format in the provided `storage.py`) silently failed
to match; a fresh-context review caught it.

**1b. Several references** (not yet observed). The amount fills each remaining balance in
order; the remainder goes to the last. If one referenced payment hasn't arrived, the
transaction waits out the grace period, then allocates across the existing payments **only
if the amount fits their balances**, so the missing payment's share isn't lost to a
permanent link.

**1c. Reference typo.** Within `FUZZY_REFERENCE_MAX_EDITS` (default 1) typos, where an
adjacent swap (`6981` → `6918`) counts as one (optimal string alignment, `rapidfuzz`); only
references of 8+ characters with a digit. Edit distance, not a similarity ratio, because one
digit also separates *different* invoices (`...-6981` vs `...-6982` is 1 edit), and a
ratio scores both cases the same. So the distance never decides alone:
- it runs only after the grace period, since the exact payment may still arrive;
- same currency, payer name equal or similar (as in 2b), payment still open;
- a well-formed reference must settle the balance within tolerance, since it may be a
  different invoice whose payment is missing; a malformed one may be partial; a refund must
  reverse an earlier linked amount;
- exactly one candidate, and if payer + amount (tier 2) points at a different payment, the
  transaction is left for review.

**2. Payer + amount.** For empty or placeholder references (`UNKNOWN-*`, `N/A`, `NONE`,
`NULL`, `-`), or a reference matching no payment after the grace period. Normalized payer
name (casefold, legal suffixes like `inc`/`llc`/`co` stripped) + amount within tolerance of
the remaining balance, exactly one candidate. A refund must reverse an amount linked to that
payment and not already refunded.

**2c. Combined balances.** The riskiest tier, so it demands the most: exact payer, positive
amount, **exactly** equal to the combined balances of 2–3 open payments, and exactly one
such combination. A single payment that fits always wins. The search is bounded (≤ 30
candidates, stops at the second combination).

**2b. Similar payer name.** Only when no payer has the exact name, so it can't redirect
money to a similar-but-different payer. A weaker name signal needs a stronger amount
signal, so the amount must be exact. The 0.85 threshold is measured: the most similar
*different* real payers score 0.56, misspellings 0.85–0.97. Names differing only by a
number (`Payer 1` / `Payer 2`, 0.86) are excluded.

### The reference grace period
A reference that matches no payment **waits** (`REFERENCE_GRACE_MINUTES`, default 15)
before falling back to tiers 1c and 2, because its payment may not have arrived. Falling
back immediately put money on the wrong invoice in 5 shuffled simulations (e.g.
`txn_sim5_0015` linked to another Prime Logistics payment while its own arrived 65 events
later). The wait counts from when *we* received the transaction, not `settled_at` (which
can be old), and not while the service was down (the payment webhook is then stuck in the
sender's retries). Empty and placeholder references fall back at once. A timer
(`RECONCILE_INTERVAL_SECONDS`, default 60) re-checks waiting transactions when the stream
is quiet. A payment arriving *after* the grace period still loses its transaction (Q9).

### Status and refunds

| Status | Rule |
|---|---|
| `PENDING` | Nothing received (net ≤ 0) while something is due |
| `PARTIALLY_PAID` | Below the amount due minus the fee allowance |
| `FULLY_PAID` | Within the fee allowance of the amount due, or nothing due (fully refunded) |
| `OVERPAID` | More than the amount due |

- **Fee tolerance:** the larger of `FEE_TOLERANCE_PERCENT` (default 2%) and
  `FEE_TOLERANCE_FIXED`, applied below the amount due only, since fees reduce what arrives.
  2% because under-calling "fully paid" is the safer error. The three short payments in §3
  are ambiguous (Q2).
- **Refunds** are negative links. In link order, a refund first gives back any overpayment,
  and the rest lowers `amount_due` (the payer owes less, Q1). So a refund never reopens a
  balance. A refund larger than what was paid leaves a negative amount due, for review.
- `outstanding_amount` is 0 once `FULLY_PAID`/`OVERPAID`: a shortfall within tolerance is fees.

### Execution
Matching runs as a background task after the `200`, behind one lock so two events can't race
on a payment. Each run re-checks **all** unmatched transactions in settlement order, which
is what matches a transaction that arrived before its payment. A run repeats its pass until
nothing new links, because a later match can resolve an earlier transaction's ambiguity; the
result then doesn't depend on how events were grouped into runs. A startup run catches
anything stored but never reconciled.

| | Inline (in the request) | **Background task (current)** | Queue worker |
|---|---|---|---|
| Status freshness | Before the 200 | Milliseconds later | Seconds–minutes later |
| Webhook latency | Includes matching | Fast | Fast |
| Survives a crash | Yes (5xx, provider retries) | Covered by startup catch-up + re-checking all unmatched | Yes |
| Extra infrastructure | None | None | Queue, workers, monitoring |

The current choice gives fast acknowledgements and near-real-time status with no extra
infrastructure; its weakness (losing queued work on a crash) is covered by the catch-up.
Measured locally: median 3–7 ms per webhook.

### Result on observed data
24 of 27 transactions linked (21 `EXACT_REF`, 1 `EXTRACTED_REF`, 2 `NAME_AMOUNT`); the three
`UNKNOWN-*` left unmatched with a reason. 18 payments `FULLY_PAID` (including REF-2026-1047
at −1.5%), 2 `PARTIALLY_PAID` (−4.4% and −4.8%, outside the 2% tolerance).

---

## 5. Assumptions

Things the implementation relies on that the brief doesn't state.

| # | Assumption | Basis |
|---|---|---|
| A1 | Provider IDs are stable across redeliveries and safe as idempotency keys | ✅ **Confirmed:** duplicates reuse the ID; only `timestamp` changes |
| A2 | Non-ASCII may be signed escaped (`\uXXXX`) or raw UTF-8; both are accepted | Not specified; none observed |
| A3 | An unexpected `event_type` should be acknowledged (`200`) and ignored | Avoids pointless retries; none observed |
| A4 | A transaction may arrive before its payment | Not observed live (payments came first); kept as a cheap safeguard |
| A5 | Amounts are always JSON strings | ✅ **Confirmed** across all observed events |
| A6 | A transaction only matches payments in its currency | Cross-currency would need FX data |
| A7 | Fees only *reduce* what arrives, so tolerance applies below the amount due | Brief: "fee deductions" |

---

## 6. Open questions

Questions for the assignment authors, with how each is handled now.

| # | Question | Current handling |
|---|---|---|
| Q1 | After a refund, does the payer owe that amount again, or does the amount due go down? | Amount due goes down (a refund never reopens a balance). PAY-2026-3316 (paid 2327.15, refunded 522.01) is `FULLY_PAID` with 1805.14 due. |
| Q2 | Are 1.5–5% short payments fees (settled) or partials (still due)? | Fees up to 2%. A later transaction for exactly 39.01, 61.65 or 138.91 would show they were partials. |
| Q3 | Should an `OVERPAID` payment move back after a refund? | Yes: status follows the net received amount. |
| Q4 | How soon after a webhook must status be correct? | One background run later (milliseconds). |
| Q5 | Derive status at read time, or store it? | Recalculated from links on every change and cached (§4 "Data model"). |
| Q6 | Should refunds with no original payment be listed separately? | Shown in the unmatched list with their negative amount. |
| Q7 | Can a transaction be partly allocated, leaving a remainder? | No: a multi-reference remainder goes to the last payment (it may become `OVERPAID`). |
| Q8 | Are the four statuses exhaustive (no `REFUNDED`)? | Using only the four specified. |
| Q9 | If a payer + amount link proves wrong because the referenced payment arrives after the grace period, should the link move? | Open; links are immutable. Option: mark fallback links provisional and move them atomically when an exact reference match arrives. |
| Q10 | What grace period suits the provider? | 15 minutes, configurable. Live payments came first; shuffled simulations show the wait prevents wrong matches. |
| Q11 | Is `payer_account_last_four` stable per payer? | Not in observed data, so unused. If stable: learn accounts from reference matches and use them as corroboration only. |
| Q12 | How do we tell a refund from a chargeback (where the payer still owes)? | Not distinguished: every negative amount is a refund (Q1), so a chargeback would wrongly show the payment as settled. |

---

## 7. Future improvements

- **Storage and scale.** Postgres with unique constraints for idempotency instead of a
  lock and whole-file rewrites (webhook latency grew from ~1.4 ms to ~17 ms over 900
  events). Update totals in the same DB transaction as each link, and record status
  transitions for audit. Today each run checks every unmatched transaction against every
  payment; index payments by reference and payer, and re-check only related transactions.
  Keep full recalculation as a scheduled drift audit.
- **Durability.** A queue and worker for reconciliation, so work survives crashes and
  scales beyond one process. Move rate-limit buckets to Redis or a gateway.
- **Review.** An endpoint to confirm, reject or reassign matches, with an audit trail.
- **Payer identity beyond the name.** Only payments carry `payer_email`, so it can't link
  a transaction, but payments with different names and the same email could count as one
  payer in tiers 2 and 2c (no effect on current data: 12 emails, 12 names). Learn
  `payer_account_last_four` → payer from reference matches as a supporting signal (Q11).
- **The analysis agent** (§8), and cursor-based pagination.

---

## 8. AI analysis agent (proposed, not built)

**Why AI here and not in the matcher.** Linking money must be reproducible, auditable,
testable, and free and fast on every webhook, so it stays deterministic. AI is strong at
what rules are weak at: reading **unstructured data** (free-text references, drifting payer
names), **reasoning** over several weak signals, and **detecting patterns** across many
events. That is how this project's rules were made: AI analysis of the raw webhook log
classified the cases in §3. The agent makes that permanent: it analyses and proposes, the
engine decides, and a person approves anything in between.

**Inputs.** Both the **raw events** as the provider sent them (including fields the matcher
ignores, and rejected deliveries when enabled) and the **reconciliation results** (links,
match types, unmatched reasons). Raw events are where new data shapes appear before any
rule exists for them; reading only results would limit the agent to cases the rules
already know.

**What it does**
- **Suggests matches for exceptions.** A short payment from a slightly misspelled company
  fails tier 2b, which requires an exact amount. The agent proposes the likely invoice with
  a confidence score and reasoning, and a person approves it.
- **Investigates breaks.** It gathers the context an analyst would dig for, e.g. "this $40
  gap matches the processor's chargeback fee on the 12th" (this needs extra inputs, such
  as processor fee schedules).
- **Proposes new rules.** When it keeps solving the same kind of exception (a new reference
  format, a payer alias, a consistent fee percentage for Q2, stable accounts for Q11), that
  becomes a deterministic rule with a test. The AI does discovery; the rules engine does
  enforcement.
- **Reports** match rate, unmatched amounts, anomalies and delivery behaviour.

```
webhooks ─▶ ingest ─┬─▶ deterministic engine ─▶ links (source of truth) ─▶ query API / UI
                    │            ▲                         │
                    ▼   approved rules/settings            │ read-only
             raw event log       │                         ▼
                    │       human review ◀── suggestions ◀── analysis agent
                    │                    (separate store)        ▲
                    └─────────────────── read-only ──────────────┘
```

**Safeguards.** Read-only access; output goes to a separate suggestions store, never to
links. An approved suggestion writes a link through the normal atomic path with its own
match type, so it stays auditable; rejections are kept to measure precision. Each
suggestion stores its inputs, model version, reasoning and confidence. It runs on demand or
on a schedule, never per webhook, and is off without an API key.

**Open points.** Calibrate confidence against approved and rejected suggestions and the
simulated datasets with known answers before trusting it. Payer names and emails would go
to the model provider, which needs the same review as any data processor.
