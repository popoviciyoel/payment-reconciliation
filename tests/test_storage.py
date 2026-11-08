"""JsonStorage: atomic saves and atomic multi-record writes."""

import json

import pytest

from app import storage as storage_module
from app.storage import JsonStorage


def seed(store):
    store.add_payment({"payment_id": "p1", "reference": "INV-2026-0001", "expected_amount": "100.00",
                       "currency": "USD", "payer_name": "Acme"})
    store.add_transaction({"transaction_id": "t1", "reference": "INV-2026-0001", "amount": "100.00",
                           "currency": "USD", "payer_name": "Acme", "settled_at": "2026-11-08T10:00:00"})


def test_crash_during_save_keeps_previous_file_and_memory(tmp_path, monkeypatch):
    path = tmp_path / "storage.json"
    store = JsonStorage(str(path))
    seed(store)
    before = path.read_text()

    def exploding_dump(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(storage_module.json, "dump", exploding_dump)

    with pytest.raises(OSError):
        store.add_payment({"payment_id": "p2", "expected_amount": "1.00"})

    assert path.read_text() == before                 # file untouched, still valid JSON
    assert json.loads(before)["payments"].keys() == {"p1"}
    assert store.get_payment("p2") is None            # memory reverted to disk
    assert not (tmp_path / "storage.json.tmp").exists()


def test_record_match_applies_everything_in_one_save(tmp_path, monkeypatch):
    store = JsonStorage(str(tmp_path / "storage.json"))
    seed(store)
    saves = []
    original_save = store._save
    monkeypatch.setattr(store, "_save", lambda: (saves.append(1), original_save()))

    store.record_match(
        "t1",
        links=[{"payment_id": "p1", "match_type": "EXACT_REF", "amount": "100.00", "notes": ""}],
        transaction_updates={"matched": True},
        payment_updates={"p1": {"status": "FULLY_PAID", "received_amount": "100.00"}},
    )

    assert len(saves) == 1
    reloaded = JsonStorage(str(tmp_path / "storage.json"))
    assert reloaded.get_transaction("t1")["matched"] is True
    assert reloaded.get_payment("p1")["status"] == "FULLY_PAID"
    assert reloaded.get_links_for_transaction("t1")[0]["payment_id"] == "p1"


def test_record_match_is_all_or_nothing_on_bad_input(tmp_path):
    store = JsonStorage(str(tmp_path / "storage.json"))
    seed(store)
    with pytest.raises(KeyError):
        store.record_match(
            "t1",
            links=[{"payment_id": "p1", "amount": "50.00"}, {"payment_id": "nope", "amount": "50.00"}],
            transaction_updates={"matched": True},
            payment_updates={},
        )
    assert store.get_all_reconciliation_links() == []
    assert store.get_transaction("t1")["matched"] is False
