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
_DIGITS = re.compile(r"\d+")


def normalize_reference(raw: str | None) -> str | None:
    """Trim and uppercase; empty becomes None."""
    if raw is None:
        return None
    cleaned = raw.strip().upper()
    return cleaned or None


# References that mean "no reference" (observed: `UNKNOWN-2B943F38`).
_PLACEHOLDER_REFERENCES = frozenset({"UNKNOWN", "N/A", "NA", "NONE", "NULL", "-"})
_PLACEHOLDER_PREFIXES = ("UNKNOWN-", "UNKNOWN ")

# Shortest known reference we will search for *inside* longer text; shorter
# ones (e.g. "12") would match incidental tokens. Exact equality has no minimum.
MIN_EMBEDDED_REFERENCE_LENGTH = 4


def is_placeholder_reference(normalized: str | None) -> bool:
    """True for empty / placeholder references that carry no matching signal."""
    if not normalized:
        return True
    return normalized in _PLACEHOLDER_REFERENCES or normalized.startswith(_PLACEHOLDER_PREFIXES)


def find_references(normalized_text: str, known_references) -> list[str]:
    """
    References found inside free text, ordered by position, without duplicates:
      - any known payment reference appearing as a whole token (any format), and
      - any well-formed `(REF|ORD|PAY|INV)-YYYY-NNNN` reference (even if no payment
        has it yet, so the caller can wait for that payment).
    `normalized_text` and `known_references` must already be normalized.

    A reference found only *inside* a longer found reference is dropped: `1234`
    inside `REF-2026-1234` is part of that reference, not a second one.
    """
    spans: dict[str, list[tuple[int, int]]] = {}
    for ref in known_references:
        if len(ref) < MIN_EMBEDDED_REFERENCE_LENGTH:
            continue
        found = [
            m.span()
            for m in re.finditer(rf"(?<![0-9A-Z]){re.escape(ref)}(?![0-9A-Z])", normalized_text)
        ]
        if found:
            spans[ref] = found
    for m in REFERENCE_PATTERN.finditer(normalized_text):
        spans.setdefault(m.group(), []).append(m.span())

    def nested(ref: str, span: tuple[int, int]) -> bool:
        start, end = span
        return any(
            other != ref and len(other) > len(ref) and s <= start and end <= e
            for other, other_spans in spans.items()
            for s, e in other_spans
        )

    positions = {
        ref: min(start for start, end in standalone)
        for ref, ref_spans in spans.items()
        if (standalone := [sp for sp in ref_spans if not nested(ref, sp)])
    }
    return sorted(positions, key=positions.__getitem__)


def normalize_name(raw: str | None) -> str:
    """Casefold, drop punctuation and trailing legal suffixes: 'Apex Trading Co.' -> 'apex trading'."""
    if not raw:
        return ""
    tokens = _NON_ALNUM.sub(" ", raw.casefold()).split()
    while len(tokens) > 1 and tokens[-1] in _NAME_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def name_similarity(a: str | None, b: str | None) -> float:
    """
    0..1 similarity of normalized names. A confidence note on reference matches;
    a gate only in the fuzzy tier, where it is paired with an exact amount.
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    return round(SequenceMatcher(None, na, nb).ratio(), 2)


def numbers_agree(a: str | None, b: str | None) -> bool:
    """
    True if both names contain the same numbers (or none). 'Payer 1' vs 'Payer 2'
    scores 0.86 on similarity but names two different entities (branches, stores).
    """
    return _DIGITS.findall(normalize_name(a)) == _DIGITS.findall(normalize_name(b))
