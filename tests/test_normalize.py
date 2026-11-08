import pytest

from app.reconciliation.normalize import (
    find_references,
    is_placeholder_reference,
    name_similarity,
    normalize_name,
    normalize_reference,
    numbers_agree,
)


@pytest.mark.parametrize("raw, expected", [
    ("REF-2026-6981", ["REF-2026-6981"]),
    ("INV REF-2026-6981", ["REF-2026-6981"]),        # observed prefix noise
    ("REF-2026-9392 ", ["REF-2026-9392"]),           # observed trailing space
    ("  ord-2026-4149", ["ORD-2026-4149"]),          # case
    ("PAY-2026-1111, INV-2026-2222", ["PAY-2026-1111", "INV-2026-2222"]),
    ("REF-2026-1111 REF-2026-1111", ["REF-2026-1111"]),  # deduped
    ("UNKNOWN-2B943F38", []),                         # observed placeholder
    ("", []),
    (None, []),
    ("REF-26-1", []),                                 # malformed
])
def test_pattern_references_found_without_known_payments(raw, expected):
    assert find_references(normalize_reference(raw) or "", []) == expected


def test_normalize_reference():
    assert normalize_reference("  ref-1 ") == "REF-1"
    assert normalize_reference("   ") is None
    assert normalize_reference(None) is None


@pytest.mark.parametrize("a, b", [
    ("Apex Trading Co", "apex trading co"),           # observed
    ("Horizon Partners", "Horizon Partners Inc"),     # observed
    ("Pacific Ventures", "Pacific Ventures LLC"),     # observed
    ("Acme Corporation", "ACME CORP."),
])
def test_name_variants_normalize_equal(a, b):
    assert normalize_name(a) == normalize_name(b)


def test_suffix_only_name_is_kept():
    assert normalize_name("Co") == "co"


def test_different_companies_stay_different():
    assert normalize_name("Prime Logistics") != normalize_name("Metro Supplies Inc")


def test_name_similarity():
    assert name_similarity("Apex Trading Co", "APEX TRADING") == 1.0
    assert name_similarity("Apex", None) == 0.0
    assert name_similarity("Prime Logistics", "Metro Supplies") < 0.5


@pytest.mark.parametrize("a, b, expected", [
    ("Payer 1", "Payer 2", False),
    ("Store 12", "Store 12 Inc", True),
    ("Acme", "Acmee", True),          # no numbers on either side
    ("Acme 7", "Acme", False),
])
def test_numbers_agree(a, b, expected):
    assert numbers_agree(a, b) is expected


@pytest.mark.parametrize("ref, expected", [
    (None, True), ("", True), ("UNKNOWN-2B943F38", True), ("UNKNOWN", True), ("N/A", True),
    ("ABC123", False), ("INV-2024-001", False), ("UNKNOWNCO-1", False),
])
def test_is_placeholder_reference(ref, expected):
    assert is_placeholder_reference(ref) is expected


def test_find_references_orders_by_position_and_includes_pattern_refs():
    text = "PAY ABC123 AND REF-2026-0001 AND INV-2024-001"
    assert find_references(text, ["INV-2024-001", "ABC123"]) == ["ABC123", "REF-2026-0001", "INV-2024-001"]


def test_find_references_drops_reference_nested_in_a_longer_one():
    # '1234' is a payment reference, but here it is just part of REF-2026-1234.
    assert find_references("INV REF-2026-1234", ["REF-2026-1234", "1234"]) == ["REF-2026-1234"]
    # Nested inside a well-formed reference no payment has yet: still not standalone.
    assert find_references("INV REF-2026-1234", ["1234"]) == ["REF-2026-1234"]


def test_find_references_keeps_short_reference_that_also_stands_alone():
    assert find_references("REF-2026-1234 AND 1234", ["REF-2026-1234", "1234"]) == ["REF-2026-1234", "1234"]
