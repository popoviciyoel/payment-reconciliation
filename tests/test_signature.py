import hashlib
import hmac

from app.core.signature import canonical_json, compute_signature, verify_signature

SECRET = "whsec_unit"


def test_canonical_json_sorts_keys_compacts_and_drops_sandbox_id():
    payload = {"b": "2", "a": "1", "sandbox_id": "sb_1"}
    assert canonical_json(payload) == b'{"a":"1","b":"2"}'


def test_compute_signature_matches_manual_hmac():
    payload = {"amount": "10.00", "id": "x"}
    expected = hmac.new(SECRET.encode(), b'{"amount":"10.00","id":"x"}', hashlib.sha256).hexdigest()
    assert compute_signature(payload, SECRET) == f"sha256={expected}"


def test_valid_signature_verifies():
    payload = {"id": "x", "sandbox_id": "anything"}
    assert verify_signature(payload, compute_signature(payload, SECRET), SECRET)


def test_sandbox_id_change_does_not_break_signature():
    sig = compute_signature({"id": "x", "sandbox_id": "one"}, SECRET)
    assert verify_signature({"id": "x", "sandbox_id": "two"}, sig, SECRET)


def test_tampered_payload_fails():
    sig = compute_signature({"amount": "10.00"}, SECRET)
    assert not verify_signature({"amount": "1000.00"}, sig, SECRET)


def test_wrong_secret_fails():
    payload = {"id": "x"}
    assert not verify_signature(payload, compute_signature(payload, "other"), SECRET)


def test_missing_or_malformed_header_fails():
    payload = {"id": "x"}
    digest = compute_signature(payload, SECRET).removeprefix("sha256=")
    assert not verify_signature(payload, None, SECRET)
    assert not verify_signature(payload, "", SECRET)
    assert not verify_signature(payload, digest, SECRET)  # no prefix


def test_non_ascii_accepted_in_either_canonical_form():
    payload = {"payer_name": "Société Générale"}
    escaped = compute_signature(payload, SECRET, ensure_ascii=True)
    raw = compute_signature(payload, SECRET, ensure_ascii=False)
    assert escaped != raw
    assert verify_signature(payload, escaped, SECRET)
    assert verify_signature(payload, raw, SECRET)
