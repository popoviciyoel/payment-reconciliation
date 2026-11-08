"""
Replays simulated and captured provider data end to end, through signed webhooks.

  - observed_events.jsonl : the real events captured from the mock provider (DESIGN.md §4),
                            including every redelivery
  - edge_cases.jsonl      : hand-written edge cases; each line states its expected outcome
  - generated datasets    : seeded random data with ground truth (scripts/simulate.py),
                            checked for invariants and for wrong matches

Lines marked `xfail` in edge_cases.jsonl document known gaps: the expectation is
what *should* happen, and the test fails (strictly) once the gap is fixed so the
marker gets removed.
"""

import random
from collections import Counter, defaultdict
from decimal import Decimal as D
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_storage
from app.config import settings
from app.core import signature
from app.core.signature import compute_signature
from app.domain.status import Tolerance, compute_status, settle
from app.main import app
from app.reconciliation.engine import reconcile
from app.services import ingest
from app.storage import JsonStorage
from scripts.simulate import generate, load_jsonl, write_jsonl
from tests.conftest import TEST_SECRET

FIXTURES = Path(__file__).parent / "fixtures"
TOL = Tolerance(percent=D("2"))


# --------------------------------------------------------------------------- helpers


@pytest.fixture(scope="module")
def isolated(tmp_path_factory):
    """Module-scoped equivalent of the `store` fixture, so a dataset is replayed once."""
    def make(name: str) -> tuple[JsonStorage, TestClient]:
        tmp = tmp_path_factory.mktemp(name)
        s = JsonStorage(str(tmp / "storage.json"))
        app.dependency_overrides[get_storage] = lambda: s
        return s, TestClient(app)

    with pytest.MonkeyPatch.context() as mp:
        tmp = tmp_path_factory.mktemp("logs")
        mp.setattr(settings, "webhook_secret", TEST_SECRET)
        mp.setattr(settings, "fee_tolerance_percent", D("2"))
        mp.setattr(settings, "fee_tolerance_fixed", D("0"))
        mp.setattr(ingest, "RAW_EVENT_LOG", tmp / "raw_events.jsonl")
        mp.setattr(signature, "REJECTED_EVENT_LOG", tmp / "rejected_events.jsonl")
        yield make
    app.dependency_overrides.clear()


def _headers(line: dict) -> dict:
    payload, mode = line["payload"], line.get("signature", "valid")
    if mode == "valid":
        return {"X-Webhook-Signature": compute_signature(payload, TEST_SECRET)}
    if mode == "valid_utf8":
        return {"X-Webhook-Signature": compute_signature(payload, TEST_SECRET, ensure_ascii=False)}
    if mode == "invalid":
        return {"X-Webhook-Signature": "sha256=" + "0" * 64}
    if mode == "tampered":   # signed over different content than what is delivered
        return {"X-Webhook-Signature": compute_signature({**payload, "amount": "1.00"}, TEST_SECRET)}
    return {}


def deliver(client: TestClient, line: dict):
    path = "/webhooks/payments" if line["kind"] == "payment" else "/webhooks/transactions"
    return client.post(path, json=line["payload"], headers=_headers(line))


def ack(response) -> str | int:
    return response.json()["status"] if response.status_code == 200 else response.status_code


def assert_invariants(store: JsonStorage) -> None:
    """Properties that must hold for any data, whatever the matcher decided."""
    payments = {p["payment_id"]: p for p in store.get_all_payments()}
    txns = {t["transaction_id"]: t for t in store.get_all_transactions()}
    links = store.get_all_reconciliation_links()

    by_txn, by_payment = defaultdict(list), defaultdict(list)
    for l in links:
        assert l["payment_id"] in payments, f"link to unknown payment {l}"
        assert l["transaction_id"] in txns, f"link from unknown transaction {l}"
        by_txn[l["transaction_id"]].append(l)
        by_payment[l["payment_id"]].append(l)

    for tid, t in txns.items():
        tl = by_txn.get(tid, [])
        # matched flag <=> has links; a transaction is fully allocated, never twice
        assert bool(t.get("matched")) == bool(tl), tid
        if tl:
            assert sum(D(l["amount"]) for l in tl) == D(t["amount"]), f"{tid} not fully allocated"
            assert len({l["payment_id"] for l in tl}) == len(tl), f"{tid} linked twice to one payment"
            assert set(t["matched_payment_ids"]) == {l["payment_id"] for l in tl}
            assert all(payments[l["payment_id"]]["currency"] == t["currency"] for l in tl), tid
        else:
            assert t.get("unmatched_reason"), f"{tid} unmatched without a reason"

    for pid, p in payments.items():
        received = sum((D(l["amount"]) for l in by_payment.get(pid, [])), D("0"))
        assert D(p.get("received_amount") or "0") == received, f"{pid} projection drifted"
        if by_payment.get(pid):
            s = settle(D(p["expected_amount"]), [D(l["amount"]) for l in by_payment[pid]])
            assert D(p["amount_due"]) == s.amount_due, f"{pid} amount due drifted"
            assert p["status"] == compute_status(s.amount_due, s.received, TOL).value, pid


# --------------------------------------------------------------------------- observed data


def _observed_unique() -> list[dict]:
    return [l for l in load_jsonl(FIXTURES / "observed_events.jsonl") if l.get("outcome") != "duplicate"]


@pytest.mark.parametrize("with_redeliveries", [False, True])
def test_observed_provider_data_replay(isolated, with_redeliveries):
    """Reproduces DESIGN.md §5 'Result on observed data' from the captured events."""
    store, client = isolated(f"observed_{with_redeliveries}")
    lines = load_jsonl(FIXTURES / "observed_events.jsonl") if with_redeliveries else _observed_unique()
    acks = Counter(ack(deliver(client, l)) for l in lines)
    assert set(acks) <= {"accepted", "duplicate"}
    assert acks["accepted"] == 47

    txns = store.get_all_transactions()
    match_types = Counter(l["match_type"] for l in store.get_all_reconciliation_links())
    assert len(store.get_all_payments()) == 20 and len(txns) == 27
    assert match_types == {"EXACT_REF": 21, "EXTRACTED_REF": 1, "NAME_AMOUNT": 2}
    unmatched = [t for t in txns if not t["matched"]]
    assert sorted(t["reference"] for t in unmatched) == ["UNKNOWN-2B943F38", "UNKNOWN-680664DF", "UNKNOWN-DD9DED6E"]
    assert Counter(p["status"] for p in store.get_all_payments()) == {"FULLY_PAID": 18, "PARTIALLY_PAID": 2}
    assert_invariants(store)


def test_observed_data_is_order_independent(isolated):
    """Reference-driven results must not depend on delivery order (incl. txn before payment)."""
    def final_links(seed):
        store, client = isolated(f"observed_order_{seed}")
        lines = _observed_unique()
        random.Random(seed).shuffle(lines)
        for l in lines:
            deliver(client, l)
        assert_invariants(store)
        return sorted((l["transaction_id"], l["payment_id"], l["amount"]) for l in store.get_all_reconciliation_links())

    baseline = final_links(0)
    assert len(baseline) == 24
    for seed in (1, 2, 3):
        assert final_links(seed) == baseline


# --------------------------------------------------------------------------- edge cases

EDGE_LINES = load_jsonl(FIXTURES / "edge_cases.jsonl")


@pytest.fixture(scope="module")
def edge_replay(isolated):
    store, client = isolated("edge_cases")
    acks = [ack(deliver(client, line)) for line in EDGE_LINES]
    return store, client, acks


def _edge_params():
    for i, line in enumerate(EDGE_LINES):
        if "expect" not in line:
            continue
        p = line["payload"]
        ident = p.get("transaction_id") or p.get("payment_id") or f"line{i}"
        marks = [pytest.mark.xfail(reason=line["xfail"], strict=True)] if "xfail" in line else []
        yield pytest.param(i, id=f"{line['case']}:{ident}:{i}", marks=marks)


@pytest.mark.parametrize("i", list(_edge_params()))
def test_edge_case(edge_replay, i):
    store, client, acks = edge_replay
    line, expect = EDGE_LINES[i], EDGE_LINES[i]["expect"]

    if "ack" in expect:
        assert acks[i] == expect["ack"]
        return
    assert acks[i] == "accepted", f"delivery failed: {acks[i]}"

    if line["kind"] == "payment":
        body = client.get(f"/payments/{line['payload']['payment_id']}").json()
        assert body["status"] == expect["status"]
        assert D(body["received_amount"]) == D(expect["received_amount"])
        assert "E" not in body["received_amount"], "amount shown in exponent form"
        return

    tid = line["payload"]["transaction_id"]
    t = store.get_transaction(tid)
    links = store.get_links_for_transaction(tid)
    if "unmatched" in expect:
        assert links == [] and not t["matched"]
        assert expect["unmatched"] in t["unmatched_reason"]
    else:
        assert {l["payment_id"]: l["amount"] for l in links} == expect["links"], t.get("unmatched_reason")
        assert {l["match_type"] for l in links} == {expect["match_type"]}


def test_edge_case_invariants(edge_replay):
    store, _, _ = edge_replay
    assert_invariants(store)


def test_edge_case_rejected_deliveries_are_not_stored(edge_replay):
    store, _, acks = edge_replay
    stored = {t["transaction_id"] for t in store.get_all_transactions()}
    for line, a in zip(EDGE_LINES, acks):
        tid = line["payload"].get("transaction_id")
        if line["kind"] == "transaction" and a in (401, 422, "ignored") and tid:
            assert tid not in stored, line["case"]


def test_edge_case_summary_is_consistent(edge_replay):
    store, client, _ = edge_replay
    s = client.get("/payments", params={"page_size": 100}).json()["summary"]
    assert s["total_payments"] == len(store.get_all_payments())
    assert s["matched_transactions"] + s["unmatched_transactions"] == s["total_transactions"]
    assert sum(s["payments_by_status"].values()) == s["total_payments"]
    assert set(s["totals_by_currency"]) == {"USD", "EUR", "GBP"}


# --------------------------------------------------------------------------- generated data


def _replay_dataset(store: JsonStorage, deliveries: list[dict]) -> None:
    """Store-level replay (faster than HTTP): ingest each event, reconcile after each."""
    for d in deliveries:
        p = d["payload"]
        if d["kind"] == "payment":
            if store.get_payment(p["payment_id"]) is None:
                store.add_payment({k: v for k, v in p.items() if k not in ("event_type", "timestamp", "sandbox_id")})
        elif store.get_transaction(p["transaction_id"]) is None:
            store.add_transaction({k: v for k, v in p.items() if k not in ("event_type", "timestamp", "sandbox_id")})
        reconcile(store, TOL)


def _truth(deliveries: list[dict]) -> dict[str, list[str]]:
    return {d["payload"]["transaction_id"]: d["truth"] for d in deliveries if d["kind"] == "transaction"}


def _check_against_truth(store: JsonStorage, truth: dict[str, list[str]]) -> Counter:
    """
    No wrong matches allowed. A transaction with a true payment may only go
    unmatched when the matcher saw genuine ambiguity (several open payments
    from that payer within tolerance) - declining is the safe outcome there.
    """
    outcome = Counter()
    for tid, expected in truth.items():
        linked = {l["payment_id"] for l in store.get_links_for_transaction(tid)}
        assert linked <= set(expected), f"{tid} linked to {linked}, truth {expected}"
        if linked:
            outcome["linked"] += 1
        elif not expected:
            outcome["noise_unmatched"] += 1
        else:
            reason = store.get_transaction(tid)["unmatched_reason"]
            assert "ambiguous" in reason, f"{tid} (truth {expected}) missed: {reason}"
            outcome["declined_ambiguous"] += 1
    return outcome


SIM_FILE = FIXTURES / "simulated_seed42.jsonl"


def test_committed_simulated_dataset(store):
    deliveries = load_jsonl(SIM_FILE)
    _replay_dataset(store, deliveries)
    assert_invariants(store)
    outcome = _check_against_truth(store, _truth(deliveries))
    assert outcome["declined_ambiguous"] == 0, outcome


@pytest.mark.parametrize("seed", range(25))
@pytest.mark.parametrize("payments_first", [True, False], ids=["payments_first", "shuffled"])
def test_generated_datasets_never_mismatch(store, seed, payments_first):
    ds = generate(seed=seed, n_payments=40, payments_first=payments_first)
    _replay_dataset(store, ds.deliveries)
    assert_invariants(store)
    outcome = _check_against_truth(store, _truth(ds.deliveries))
    assert outcome["noise_unmatched"] == sum(1 for t in ds.transactions if not t["truth"])


@pytest.mark.parametrize("seed", range(25))
def test_reconcile_reaches_a_fixed_point(store, seed):
    """After the last delivery, another run should find nothing new to match."""
    ds = generate(seed=seed, n_payments=40, payments_first=True)
    _replay_dataset(store, ds.deliveries)
    assert reconcile(store, TOL).matched == 0


@pytest.mark.parametrize("seed", range(5))
def test_generated_final_state_matches_fates(store, seed):
    ds = generate(seed=seed, n_payments=60, payments_first=True)
    _replay_dataset(store, ds.deliveries)
    expected_status = {
        "exact": {"FULLY_PAID"}, "eur": {"FULLY_PAID"}, "noisy_ref": {"FULLY_PAID"},
        "installments": {"FULLY_PAID"}, "fee_short": {"FULLY_PAID"}, "short": {"PARTIALLY_PAID"},
        "overpaid": {"OVERPAID"}, "no_ref": {"FULLY_PAID"}, "refunded": {"PARTIALLY_PAID", "FULLY_PAID"},
        "unpaid": {None, "PENDING"},
    }
    for p in ds.payments:
        status = (store.get_payment(p["payment_id"]) or {}).get("status")
        assert status in expected_status[p["fate"]], (p["payment_id"], p["fate"], status)


def test_generator_cli_roundtrip(tmp_path):
    from scripts.simulate import main

    out = tmp_path / "sim.jsonl"
    assert main(["generate", "--seed", "7", "--payments", "10", "--out", str(out)]) == 0
    lines = load_jsonl(out)
    assert {l["kind"] for l in lines} == {"payment", "transaction"}
    assert all("truth" in l for l in lines if l["kind"] == "transaction")
    # Deterministic for a given seed.
    write_jsonl(generate(seed=7, n_payments=10).deliveries, tmp_path / "again.jsonl")
    assert (tmp_path / "again.jsonl").read_text() == out.read_text()
