import pytest

from app.reconciliation.normalize import (
    extract_references,
    name_similarity,
    normalize_name,
    normalize_reference,
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
def test_extract_references(raw, expected):
    assert extract_references(raw) == expected


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
