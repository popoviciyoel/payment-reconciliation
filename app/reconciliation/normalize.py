"""
Normalization of references and payer names (pure functions).

Derived from observed provider data (DESIGN.md §4):
  - real references look like `(REF|ORD|PAY|INV)-YYYY-NNNN`, sometimes wrapped in
    noise (`INV REF-2026-6981`, trailing spaces) or replaced by `UNKNOWN-xxxx`
  - payer names vary by case and legal suffix (`apex trading co`, `Pacific Ventures LLC`)
"""

import re
from difflib import SequenceMatcher

REFERENCE_PATTERN = re.compile(r"\b(?:REF|ORD|PAY|INV)-\d{4}-\d{4}\b")

# Legal-entity suffixes stripped from the end of payer names.
_NAME_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "co", "corp", "corporation", "company", "plc",
})
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_reference(raw: str | None) -> str | None:
    """Trim and uppercase; empty becomes None."""
    if raw is None:
        return None
    cleaned = raw.strip().upper()
    return cleaned or None


def extract_references(raw: str | None) -> list[str]:
    """All well-formed references in a free-text field, in order, without duplicates."""
    normalized = normalize_reference(raw)
    if not normalized:
        return []
    return list(dict.fromkeys(REFERENCE_PATTERN.findall(normalized)))


def normalize_name(raw: str | None) -> str:
    """Casefold, drop punctuation and trailing legal suffixes: 'Apex Trading Co.' -> 'apex trading'."""
    if not raw:
        return ""
    tokens = _NON_ALNUM.sub(" ", raw.casefold()).split()
    while len(tokens) > 1 and tokens[-1] in _NAME_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def name_similarity(a: str | None, b: str | None) -> float:
    """0..1 similarity of normalized names; used as a confidence signal, never as a gate."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    return round(SequenceMatcher(None, na, nb).ratio(), 2)
