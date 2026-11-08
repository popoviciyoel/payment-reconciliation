"""
Simulated provider data: a seeded generator with ground truth, plus a replayer.

Generated events have the same shape as real provider webhooks (DESIGN.md §4)
and carry a `truth` field naming the payment(s) each transaction really belongs
to (`[]` = belongs to nothing and must stay unmatched). Tests use the truth to
check that the matcher never links money to the wrong payment.

Usage:
    # write a dataset (same JSONL shape as data/raw_events.jsonl, plus `truth`)
    python -m scripts.simulate generate --seed 42 --payments 60 --out data/simulated.jsonl

    # replay a dataset (or tests/fixtures/edge_cases.jsonl) against a running server
    WEBHOOK_SECRET=... python -m scripts.simulate replay data/simulated.jsonl --url http://localhost:8000
"""

import argparse
import json
import os
import random
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

CENT = Decimal("0.01")
START = datetime(2026, 11, 8, 9, 0, 0)

# (canonical name, variants a bank might show for the same payer)
PAYERS = [
    ("Horizon Partners", ["Horizon Partners Inc", "HORIZON PARTNERS", "horizon partners"]),
    ("Pinnacle Enterprises", ["Pinnacle Enterprises LLC", "PINNACLE ENTERPRISES"]),
    ("Apex Trading Co", ["apex trading co", "Apex Trading", "APEX TRADING CO."]),
    ("Stellar Group", ["Stellar Group Ltd", "stellar group"]),
    ("Quantum Dynamics", ["Quantum Dynamics Corp", "QUANTUM DYNAMICS"]),
    ("Prime Logistics", ["Prime Logistics, Inc.", "prime logistics"]),
    ("Metro Supply", ["Metro Supply Co", "METRO SUPPLY"]),
    ("Vertex Solutions", ["Vertex Solutions LLC", "vertex solutions"]),
    ("Continental Services", ["Continental Services", "CONTINENTAL SERVICES INC"]),
    ("Pacific Ventures", ["Pacific Ventures LLC", "pacific ventures"]),
    ("Northwind Traders", ["Northwind Traders", "NORTHWIND TRADERS LTD"]),
    ("Blue Harbor Foods", ["Blue Harbor Foods Inc", "blue harbor foods"]),
]

# Fate of a payment -> how its transactions are generated. Weights roughly follow
# the observed mix (mostly exact references) with the rarer cases boosted.
FATES = {
    "exact": 30,          # one transaction, exact reference
    "noisy_ref": 8,       # reference wrapped in noise / different case
    "installments": 10,   # 2-3 partials summing exactly to the amount
    "fee_short": 8,       # short by < tolerance -> FULLY_PAID
    "short": 6,           # short by more than tolerance -> PARTIALLY_PAID
    "overpaid": 4,        # paid more than expected
    "no_ref": 8,          # empty / placeholder reference, payer variant, exact amount
    "refunded": 5,        # paid, then a partial refund by reference
    "unpaid": 8,          # no transactions at all
    "eur": 4,             # non-USD payment paid by reference
}


@dataclass
class Dataset:
    payments: list[dict] = field(default_factory=list)
    transactions: list[dict] = field(default_factory=list)   # each has a `truth` list
    deliveries: list[dict] = field(default_factory=list)     # {"kind", "payload", "truth"?}


def _money(rng: random.Random, lo: float, hi: float) -> Decimal:
    return Decimal(str(rng.uniform(lo, hi))).quantize(CENT, ROUND_HALF_UP)


def _ratio(rng: random.Random, lo: float, hi: float) -> Decimal:
    return Decimal(str(round(rng.uniform(lo, hi), 4)))


def _split(rng: random.Random, total: Decimal, parts: int) -> list[Decimal]:
    cuts = sorted(_ratio(rng, 0.15, 0.85) * total for _ in range(parts - 1))
    cuts = [c.quantize(CENT) for c in cuts]
    edges = [Decimal("0")] + cuts + [total]
    return [b - a for a, b in zip(edges, edges[1:]) if b - a > 0]


def generate(seed: int = 42, n_payments: int = 60, n_noise: int = 8, duplicate_rate: float = 0.2,
             payments_first: bool = False) -> Dataset:
    """
    Build a dataset with ground truth.

    payments_first=True mimics the observed provider (payments batch, then
    transactions); False shuffles everything, so transactions often arrive
    before their payment.
    """
    rng = random.Random(seed)
    ds = Dataset()
    used_refs: set[str] = set()
    clock = [START]
    seq = [0]

    def tick() -> str:
        clock[0] += timedelta(seconds=rng.randint(5, 90))
        return clock[0].isoformat()

    def new_ref() -> str:
        while True:
            ref = f"{rng.choice(['REF', 'ORD', 'PAY', 'INV'])}-2026-{rng.randint(1000, 9999)}"
            if ref not in used_refs:
                used_refs.add(ref)
                return ref

    def txn(ref, amount: Decimal, payer: str, truth: list[str], currency="USD") -> None:
        seq[0] += 1
        settled = tick()
        ds.transactions.append({
            "event_type": "transaction.settled",
            "transaction_id": f"txn_sim{seed}_{seq[0]:04d}",
            "reference": ref,
            "amount": str(amount),
            "currency": currency,
            "payer_name": payer,
            "payer_account_last_four": f"{rng.randint(0, 9999):04d}",
            "settled_at": settled,
            "bank_reference": f"BANK-{rng.getrandbits(48):012X}",
            "timestamp": settled,
            "truth": truth,
        })

    for i in range(n_payments):
        payer, variants = rng.choice(PAYERS)
        fate = rng.choices(list(FATES), weights=list(FATES.values()))[0]
        currency = "EUR" if fate == "eur" else "USD"
        ref = new_ref()
        expected = _money(rng, 25, 5000)
        pid = f"pay_sim{seed}_{i:04d}"
        ds.payments.append({
            "event_type": "payment.created",
            "payment_id": pid,
            "reference": ref,
            "expected_amount": str(expected),
            "currency": currency,
            "payer_name": payer,
            "payer_email": f"billing@{payer.split()[0].lower()}.com",
            "due_date": (START + timedelta(days=rng.randint(5, 30))).date().isoformat(),
            "timestamp": tick(),
            "fate": fate,
        })
        name = rng.choice(variants)

        if fate in ("exact", "eur"):
            txn(ref, expected, name, [pid], currency)
        elif fate == "noisy_ref":
            noisy = rng.choice([f"INV {ref}", f" {ref} ", ref.lower(), f"Payment for {ref}.", f"{ref}/NOV"])
            txn(noisy, expected, name, [pid])
        elif fate == "installments":
            for part in _split(rng, expected, rng.choice([2, 3])):
                txn(ref, part, name, [pid])
        elif fate == "fee_short":
            txn(ref, (expected * (1 - _ratio(rng, 0.001, 0.019))).quantize(CENT), name, [pid])
        elif fate == "short":
            txn(ref, (expected * (1 - _ratio(rng, 0.03, 0.4))).quantize(CENT), name, [pid])
        elif fate == "overpaid":
            txn(ref, expected + _money(rng, 1, 200), name, [pid])
        elif fate == "no_ref":
            placeholder = rng.choice(["", None, f"UNKNOWN-{rng.getrandbits(32):08X}", "N/A"])
            txn(placeholder, expected, name, [pid])
        elif fate == "refunded":
            txn(ref, expected, name, [pid])
            txn(ref, -_money(rng, 1, float(expected) / 2), name, [pid])

    # Noise: money that belongs to nothing we know about.
    payer_amounts: dict[str, list[Decimal]] = {}
    for p in ds.payments:
        payer_amounts.setdefault(p["payer_name"], []).append(Decimal(p["expected_amount"]))
    for _ in range(n_noise):
        payer, variants = rng.choice(PAYERS)
        while True:  # keep clear of any amount the payer could plausibly owe
            amount = _money(rng, 5, 5000)
            if all(abs(amount - a) > a * Decimal("0.05") for a in payer_amounts.get(payer, [])):
                break
        kind = rng.choice(["unknown", "orphan_ref"])
        ref = f"UNKNOWN-{rng.getrandbits(32):08X}" if kind == "unknown" else new_ref()
        txn(ref, amount, rng.choice(variants), [])

    # Delivery order (+ redeliveries that differ only in timestamp, as observed).
    pay_events = [{"kind": "payment", "payload": _strip(p)} for p in ds.payments]
    txn_events = [{"kind": "transaction", "payload": _strip(t), "truth": t["truth"]} for t in ds.transactions]
    if payments_first:
        rng.shuffle(pay_events)
        rng.shuffle(txn_events)
        events = pay_events + txn_events
    else:
        events = pay_events + txn_events
        rng.shuffle(events)
    for e in list(events):
        if rng.random() < duplicate_rate:
            dup = {**e, "payload": {**e["payload"], "timestamp": tick()}}
            events.insert(rng.randint(events.index(e) + 1, len(events)), dup)
    ds.deliveries = events
    return ds


def _strip(record: dict) -> dict:
    """Drop generator-only fields so the payload looks like a real webhook."""
    return {k: v for k, v in record.items() if k not in ("truth", "fate")}


def load_jsonl(path: str | Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(deliveries: list[dict], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for d in deliveries:
            f.write(json.dumps(d) + "\n")


# --------------------------------------------------------------------------- replay


def replay(deliveries: list[dict], base_url: str, secret: str) -> list[int]:
    """POST each delivery to a running server with a valid signature (or as the line says)."""
    from app.core.signature import compute_signature

    codes = []
    for d in deliveries:
        payload = d["payload"]
        headers = {"Content-Type": "application/json"}
        sig = d.get("signature", "valid")
        if sig == "valid":
            headers["X-Webhook-Signature"] = compute_signature(payload, secret)
        elif sig == "invalid":
            headers["X-Webhook-Signature"] = "sha256=" + "0" * 64
        path = "/webhooks/payments" if d["kind"] == "payment" else "/webhooks/transactions"
        req = urllib.request.Request(base_url.rstrip("/") + path, json.dumps(payload).encode(), headers)
        try:
            with urllib.request.urlopen(req) as r:
                codes.append(r.status)
        except urllib.error.HTTPError as e:
            codes.append(e.code)
    return codes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--payments", type=int, default=60)
    g.add_argument("--noise", type=int, default=8)
    g.add_argument("--duplicate-rate", type=float, default=0.2)
    g.add_argument("--payments-first", action="store_true")
    g.add_argument("--out", required=True)
    r = sub.add_parser("replay")
    r.add_argument("file")
    r.add_argument("--url", default="http://localhost:8000")
    args = ap.parse_args(argv)

    if args.cmd == "generate":
        ds = generate(args.seed, args.payments, args.noise, args.duplicate_rate, args.payments_first)
        write_jsonl(ds.deliveries, args.out)
        print(f"wrote {len(ds.deliveries)} deliveries ({len(ds.payments)} payments, "
              f"{len(ds.transactions)} transactions) to {args.out}")
        return 0

    secret = os.environ.get("WEBHOOK_SECRET")
    if not secret:
        print("WEBHOOK_SECRET is not set", file=sys.stderr)
        return 1
    codes = replay(load_jsonl(args.file), args.url, secret)
    print({c: codes.count(c) for c in sorted(set(codes))})
    return 0


if __name__ == "__main__":
    sys.exit(main())
