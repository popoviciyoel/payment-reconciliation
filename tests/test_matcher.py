"""Matcher tests. Cases marked 'observed' come from real provider data (DESIGN.md §4)."""

from decimal import Decimal as D

import pytest

from app.domain.status import Tolerance
from app.reconciliation.matcher import (
    MAX_OPEN_PAYMENTS_FOR_COMBINATIONS,
    MatchType as M,
    PaymentView,
    TransactionView,
    match,
)

TOL = Tolerance(percent=D("2"))


def pay(pid, ref, expected, payer, received="0", linked=(), currency="USD"):
    return PaymentView(pid, ref, D(expected), currency, payer, D(received), tuple(D(x) for x in linked))


def txn(ref, amount, payer, currency="USD"):
    return TransactionView("txn_1", ref, D(amount), currency, payer)


PAYMENTS = [
    pay("p_2811", "ORD-2026-2811", "4153.15", "Apex Trading Co"),
    pay("p_5038", "PAY-2026-5038", "3282.29", "Apex Trading Co"),
    pay("p_6981", "REF-2026-6981", "4670.75", "Quantum Dynamics"),
    pay("p_9392", "REF-2026-9392", "4865.40", "Pinnacle Enterprises"),
    pay("p_9137", "ORD-2026-9137", "2139.69", "Pinnacle Enterprises", received="2139.69", linked=["2139.69"]),
    pay("p_4149", "ORD-2026-4149", "356.13", "Stellar Group"),
    pay("p_2486", "REF-2026-2486", "243.98", "Prime Logistics", received="53.80", linked=["53.80"]),
]


def only(result):
    assert result.matched, result.reason
    assert len(result.allocations) == 1
    return result.allocations[0]


# ----------------------------------------------------------------- tier 1: reference

def test_exact_reference():                                   # observed
    a = only(match(txn("ORD-2026-9137", "2139.69", "Pinnacle Enterprises"), PAYMENTS, TOL))
    assert (a.payment_id, a.amount, a.match_type) == ("p_9137", D("2139.69"), M.EXACT_REF)


def test_reference_wins_despite_name_variant():               # observed: 'apex trading co'
    a = only(match(txn("PAY-2026-5038", "3282.29", "apex trading co"), PAYMENTS, TOL))
    assert a.payment_id == "p_5038"
    assert "similarity 1.0" in a.notes


def test_prefixed_reference_is_extracted():                   # observed: 'INV REF-2026-6981'
    a = only(match(txn("INV REF-2026-6981", "4670.75", "Quantum Dynamics"), PAYMENTS, TOL))
    assert (a.payment_id, a.match_type) == ("p_6981", M.EXTRACTED_REF)


def test_trailing_space_counts_as_exact():                    # observed: 'REF-2026-9392 '
    a = only(match(txn("REF-2026-9392 ", "4865.40", "Pinnacle Enterprises"), PAYMENTS, TOL))
    assert (a.payment_id, a.match_type) == ("p_9392", M.EXACT_REF)


def test_spaces_for_dashes_counts_as_exact():                 # observed: 'PAY 2026 9788'
    # Matches right away by reference, with no grace-period wait.
    a = only(match(txn("PAY 2026 9788", "1092.33", "Global Tech Solutions"),
                   PAYMENTS + [pay("p_9788", "PAY-2026-9788", "1092.33", "Global Tech Solutions")], TOL))
    assert (a.payment_id, a.match_type) == ("p_9788", M.EXACT_REF)


def test_partial_payment_links_full_transaction_amount():     # observed: REF-2026-2486
    a = only(match(txn("REF-2026-2486", "75.87", "Prime Logistics"), PAYMENTS, TOL))
    assert (a.payment_id, a.amount) == ("p_2486", D("75.87"))


def test_overpayment_by_reference_still_links():
    a = only(match(txn("ORD-2026-9137", "100.00", "Pinnacle Enterprises"), PAYMENTS, TOL))
    assert a.payment_id == "p_9137"   # status will become OVERPAID


def test_reference_matching_no_payment_falls_back_to_name_and_amount():
    # References aren't always informative. Known risk (DESIGN.md Q9/Q10): if the
    # referenced payment arrives later, this link points at the wrong invoice.
    a = only(match(txn("INV-2026-8490", "4153.15", "Apex Trading Co"), PAYMENTS, TOL))
    assert (a.payment_id, a.match_type) == ("p_2811", M.NAME_AMOUNT)
    assert "'INV-2026-8490' matched no payment" in a.notes


def test_reference_matching_no_payment_and_no_fallback_explains_both():
    r = match(txn("INV-2026-8490", "1.00", "Apex Trading Co"), PAYMENTS, TOL)
    assert not r.matched
    assert "'INV-2026-8490' matched no payment" in r.reason and "fits amount" in r.reason


def test_refund_by_reference():
    a = only(match(txn("REF-2026-2486", "-53.80", "Prime Logistics"), PAYMENTS, TOL))
    assert (a.payment_id, a.amount) == ("p_2486", D("-53.80"))


def test_currency_mismatch_is_unmatched():
    r = match(txn("ORD-2026-4149", "356.13", "Stellar Group", currency="EUR"), PAYMENTS, TOL)
    assert not r.matched and "currency" in r.reason


def test_duplicate_payment_reference_is_ambiguous():
    payments = PAYMENTS + [pay("p_dup", "ORD-2026-4149", "10.00", "Someone")]
    r = match(txn("ORD-2026-4149", "356.13", "Stellar Group"), payments, TOL)
    assert not r.matched and "shared" in r.reason


# ----------------------------------------------------------------- tier 1b: multi-reference

def test_multi_reference_fills_balances_in_order():
    r = match(txn("ORD-2026-4149 / REF-2026-2486", "546.31", "Stellar Group"), PAYMENTS, TOL)
    assert [(a.payment_id, a.amount, a.match_type) for a in r.allocations] == [
        ("p_4149", D("356.13"), M.MULTI_REF),
        ("p_2486", D("190.18"), M.MULTI_REF),   # 243.98 - 53.80 remaining
    ]


def test_multi_reference_remainder_goes_to_last():
    r = match(txn("ORD-2026-4149 REF-2026-2486", "600.00", "x"), PAYMENTS, TOL)
    assert [a.amount for a in r.allocations] == [D("356.13"), D("243.87")]
    assert sum(a.amount for a in r.allocations) == D("600.00")


def test_multi_reference_skips_fully_paid_payment():
    r = match(txn("ORD-2026-9137 ORD-2026-4149", "356.13", "x"), PAYMENTS, TOL)
    assert [(a.payment_id, a.amount) for a in r.allocations] == [("p_4149", D("356.13"))]


def test_multi_reference_refund_is_unmatched():
    r = match(txn("ORD-2026-4149 REF-2026-2486", "-10.00", "x"), PAYMENTS, TOL)
    assert not r.matched


# ----------------------------------------------------------------- tier 2: name + amount

def test_empty_reference_matches_on_name_and_exact_amount():  # observed: '' 4153.15 Apex
    a = only(match(txn("", "4153.15", "Apex Trading Co"), PAYMENTS, TOL))
    assert (a.payment_id, a.match_type) == ("p_2811", M.NAME_AMOUNT)


def test_null_reference_matches_on_name_and_amount():         # observed: '' 356.13 Stellar
    a = only(match(txn(None, "356.13", "STELLAR GROUP"), PAYMENTS, TOL))
    assert a.payment_id == "p_4149"


def test_name_amount_matches_remaining_balance():
    a = only(match(txn(None, "190.18", "Prime Logistics"), PAYMENTS, TOL))
    assert a.payment_id == "p_2486"


def test_name_amount_within_fee_tolerance():
    # 2% of 356.13 -> lowest accepted amount is 349.0074
    a = only(match(txn(None, "349.01", "Stellar Group"), PAYMENTS, TOL))
    assert a.payment_id == "p_4149"
    assert not match(txn(None, "349.00", "Stellar Group"), PAYMENTS, TOL).matched


def test_unknown_placeholder_with_no_fitting_amount_is_unmatched():  # observed: UNKNOWN-DD9DED6E
    r = match(txn("UNKNOWN-DD9DED6E", "96.94", "Pinnacle Enterprises"), PAYMENTS, TOL)
    assert not r.matched
    assert "fits amount" in r.reason


def test_name_alone_never_matches():
    r = match(txn(None, "1.00", "Apex Trading Co"), PAYMENTS, TOL)
    assert not r.matched


def test_ambiguous_name_amount_is_unmatched():
    payments = [pay("a", "R-1", "100.00", "Acme"), pay("b", "R-2", "100.00", "Acme Inc")]
    r = match(txn(None, "100.00", "ACME"), payments, TOL)
    assert not r.matched and "ambiguous" in r.reason


def test_unknown_payer_is_unmatched():
    r = match(txn(None, "4153.15", "Somebody Else"), PAYMENTS, TOL)
    assert not r.matched


def test_refund_without_reference_reverses_equal_prior_payment():
    a = only(match(txn(None, "-53.80", "Prime Logistics"), PAYMENTS, TOL))
    assert (a.payment_id, a.match_type) == ("p_2486", M.REFUND_NAME_AMOUNT)


def test_refund_without_reference_and_no_equal_prior_payment():
    r = match(txn(None, "-20.00", "Prime Logistics"), PAYMENTS, TOL)
    assert not r.matched


def test_zero_amount_is_unmatched():
    assert not match(txn("ORD-2026-4149", "0", "Stellar Group"), PAYMENTS, TOL).matched


# ----------------------------------------------------------------- tier 2b: fuzzy name + exact amount

def test_misspelled_payer_with_exact_amount_matches_fuzzily():
    a = only(match(txn(None, "356.13", "Stelar Group"), PAYMENTS, TOL))
    assert (a.payment_id, a.match_type) == ("p_4149", M.FUZZY_NAME_AMOUNT)
    assert "similarity 0.96" in a.notes


def test_fuzzy_matches_exact_remaining_balance_of_partial_payment():
    a = only(match(txn(None, "190.18", "Prime Logistic"), PAYMENTS, TOL))   # 243.98 - 53.80
    assert (a.payment_id, a.match_type) == ("p_2486", M.FUZZY_NAME_AMOUNT)


def test_fuzzy_requires_exact_amount_no_fee_tolerance():
    # 349.01 would match Stellar via tolerance with the exact name, not with a misspelled one.
    assert only(match(txn(None, "349.01", "Stellar Group"), PAYMENTS, TOL)).match_type is M.NAME_AMOUNT
    r = match(txn(None, "349.01", "Stelar Group"), PAYMENTS, TOL)
    assert not r.matched and "exactly" in r.reason


def test_fuzzy_below_threshold_does_not_match():
    r = match(txn(None, "356.13", "Stellar Holdings"), PAYMENTS, TOL)
    assert not r.matched


def test_threshold_is_configurable():
    assert not match(txn(None, "356.13", "Stelar Group"), PAYMENTS, TOL, name_threshold=0.99).matched


def test_names_differing_only_by_number_never_fuzzy_match():
    payments = [pay("s1", "R-1", "100.00", "Store 1")]
    r = match(txn(None, "100.00", "Store 2"), payments, TOL)
    assert not r.matched


def test_fuzzy_ambiguous_is_unmatched():
    payments = [pay("a", "R-1", "100.00", "Acme Trading"), pay("b", "R-2", "100.00", "Acme Tradings")]
    r = match(txn(None, "100.00", "Acme Tradng"), payments, TOL)
    assert not r.matched and "ambiguous" in r.reason


def test_fuzzy_not_used_when_exact_payer_exists_but_amount_does_not_fit():
    # Exact-name candidates take precedence; fuzzy must not reach a different payer.
    payments = PAYMENTS + [pay("p_other", "R-9", "96.94", "Pinnacle Enterprise Group")]
    r = match(txn("UNKNOWN-DD9DED6E", "96.94", "Pinnacle Enterprises"), payments, TOL)
    assert not r.matched


def test_fuzzy_refund_reverses_exact_earlier_amount():
    a = only(match(txn(None, "-53.80", "Prime Logistic"), PAYMENTS, TOL))
    assert (a.payment_id, a.match_type) == ("p_2486", M.FUZZY_NAME_AMOUNT)
    assert "refund" in a.notes


def test_fuzzy_respects_currency():
    payments = [pay("e", "R-1", "100.00", "Stellar Group", currency="EUR")]
    assert not match(txn(None, "100.00", "Stelar Group"), payments, TOL).matched


# ----------------------------------------------------------------- references in any format
# Regression: references used to count only if they matched REF|ORD|PAY|INV-YYYY-NNNN.

OTHER_FORMATS = [
    pay("p_doc", "INV-2024-001", "1000.00", "Acme Corporation"),   # format from storage.py docstring
    pay("p_abc", "ABC123", "250.00", "Globex Inc"),
    pay("p_abc2", "ABC1234", "250.00", "Globex Inc"),
    pay("p_short", "A1", "75.00", "Initech"),
]


def test_reference_from_provided_docstring_format_matches_exactly():
    a = only(match(txn("INV-2024-001", "1000.00", "ACME CORP"), OTHER_FORMATS, TOL))
    assert (a.payment_id, a.match_type) == ("p_doc", M.EXACT_REF)


def test_arbitrary_reference_matches_exactly_not_ambiguous():
    a = only(match(txn("ABC123", "250.00", "Globex Inc"), OTHER_FORMATS, TOL))
    assert (a.payment_id, a.match_type) == ("p_abc", M.EXACT_REF)


def test_exact_reference_is_case_and_whitespace_insensitive():
    a = only(match(txn("  abc123 ", "250.00", "Globex Inc"), OTHER_FORMATS, TOL))
    assert (a.payment_id, a.match_type) == ("p_abc", M.EXACT_REF)


def test_any_format_reference_found_inside_text():
    a = only(match(txn("Payment for ABC123 thanks", "250.00", "Globex"), OTHER_FORMATS, TOL))
    assert (a.payment_id, a.match_type) == ("p_abc", M.EXTRACTED_REF)


def test_embedded_reference_needs_whole_token():
    # 'ABC1234' contains 'ABC123' but must resolve to the longer reference only.
    a = only(match(txn("Payment ABC1234", "250.00", "Globex"), OTHER_FORMATS, TOL))
    assert a.payment_id == "p_abc2"


def test_short_reference_matches_exactly_but_not_inside_text():
    assert only(match(txn("A1", "75.00", "Initech"), OTHER_FORMATS, TOL)).match_type is M.EXACT_REF
    # Not found as a reference inside text; matched only via the payer + amount fallback.
    a = only(match(txn("ROUTE A1 TRANSFER", "75.00", "Initech"), OTHER_FORMATS, TOL))
    assert (a.payment_id, a.match_type) == ("p_short", M.NAME_AMOUNT)


def test_multiple_any_format_references_split():
    r = match(txn("ABC123, INV-2024-001", "1250.00", "x"), OTHER_FORMATS, TOL)
    assert [(a.payment_id, a.amount, a.match_type) for a in r.allocations] == [
        ("p_abc", D("250.00"), M.MULTI_REF),
        ("p_doc", D("1000.00"), M.MULTI_REF),
    ]


def test_free_text_reference_falls_back_to_name_and_amount():
    a = only(match(txn("November invoice", "1000.00", "Acme Corporation"), OTHER_FORMATS, TOL))
    assert (a.payment_id, a.match_type) == ("p_doc", M.NAME_AMOUNT)
    assert "matched no payment" in a.notes


@pytest.mark.parametrize("placeholder", ["UNKNOWN-2B943F38", "unknown", "N/A", "NONE", "null", "-", "   "])
def test_placeholder_references_fall_through_to_name_and_amount(placeholder):
    a = only(match(txn(placeholder, "1000.00", "Acme Corporation"), OTHER_FORMATS, TOL))
    assert (a.payment_id, a.match_type) == ("p_doc", M.NAME_AMOUNT)


# ----------------------------------------------------------------- nested references

NESTED = [
    pay("p_long", "REF-2026-1234", "100.00", "Acme"),
    pay("p_short", "1234", "50.00", "Other Co"),
]


def test_reference_nested_in_another_is_not_a_second_hit():
    a = only(match(txn("INV REF-2026-1234", "100.00", "Acme"), NESTED, TOL))
    assert (a.payment_id, a.amount, a.match_type) == ("p_long", D("100.00"), M.EXTRACTED_REF)


def test_nested_reference_does_not_take_money_from_partly_paid_payment():
    payments = [pay("p_long", "REF-2026-1234", "100.00", "Acme", received="40.00", linked=("40.00",)), NESTED[1]]
    a = only(match(txn("INV REF-2026-1234", "100.00", "Acme"), payments, TOL))
    assert (a.payment_id, a.amount) == ("p_long", D("100.00"))


def test_multi_reference_never_writes_zero_link_to_last_payment():
    payments = [pay("p_a", "REF-2026-0001", "100.00", "x"), pay("p_b", "REF-2026-0002", "50.00", "x")]
    r = match(txn("REF-2026-0001 REF-2026-0002", "100.00", "x"), payments, TOL)
    assert [(a.payment_id, a.amount) for a in r.allocations] == [("p_a", D("100.00"))]


# ----------------------------------------------------------------- multi-reference with a missing payment

def waiting(t):
    return TransactionView(t.transaction_id, t.reference, t.amount, t.currency, t.payer_name,
                           reference_fallback_allowed=False)


def test_multi_reference_waits_while_a_referenced_payment_is_missing():
    r = match(waiting(txn("ORD-2026-4149 ORD-2026-0001", "400.00", "Stellar Group")), PAYMENTS, TOL)
    assert not r.matched and "ORD-2026-0001" in r.reason and "waiting" in r.reason


def test_single_known_plus_missing_reference_also_waits():
    # Previously the known payment took the whole amount and became OVERPAID.
    r = match(waiting(txn("REF-2026-6981 REF-2026-0002", "5000.00", "Quantum Dynamics")), PAYMENTS, TOL)
    assert not r.matched


def test_after_grace_amount_beyond_known_payments_stays_unmatched():
    # Previously p_4149 (356.13) took all 400.00 and the missing payment's share was lost.
    r = match(txn("ORD-2026-4149 ORD-2026-0001", "400.00", "Stellar Group"), PAYMENTS, TOL)
    assert not r.matched
    assert "ORD-2026-0001" in r.reason and "needs review" in r.reason


def test_after_grace_one_known_of_two_references_does_not_take_everything():
    payments = [pay("p1", "REF-2026-0001", "1000.00", "Acme")]
    r = match(txn("REF-2026-0001 REF-2026-0002", "1500.00", "Acme"), payments, TOL)
    assert not r.matched


def test_after_grace_amount_that_fits_known_payment_is_allocated_with_note():
    r = match(txn("ORD-2026-4149 ORD-2026-0001", "356.13", "Stellar Group"), PAYMENTS, TOL)
    [a] = r.allocations
    assert (a.payment_id, a.amount, a.match_type) == ("p_4149", D("356.13"), M.EXTRACTED_REF)
    assert "ORD-2026-0001 matched no payment after the grace period" in a.notes


def test_after_grace_refund_naming_a_missing_reference_stays_unmatched():
    payments = [pay("p1", "REF-2026-0001", "100.00", "Acme", received="100.00", linked=["100.00"])]
    r = match(txn("REF-2026-0001 REF-2026-0002", "-100.00", "Acme"), payments, TOL)
    assert not r.matched


# ----------------------------------------------------------------- generic references inside text

def test_generic_word_reference_does_not_steal_part_of_a_payment():
    payments = [pay("p_word", "PAYMENT", "50.00", "Other Co"), pay("p_real", "REF-2026-0001", "100.00", "Acme")]
    a = only(match(txn("PAYMENT REF-2026-0001", "100.00", "Acme"), payments, TOL))
    assert (a.payment_id, a.amount, a.match_type) == ("p_real", D("100.00"), M.EXTRACTED_REF)


def test_generic_word_reference_still_matches_exactly():
    payments = [pay("p_word", "PAYMENT", "50.00", "Other Co")]
    a = only(match(txn("payment", "50.00", "Other Co"), payments, TOL))
    assert (a.payment_id, a.match_type) == ("p_word", M.EXACT_REF)


# ----------------------------------------------------------------- refunds reverse each payment once

def test_refund_cannot_take_received_below_zero():
    # 100.00 still listed as unreversed (e.g. an earlier partial refund by reference), but only 70.00 received.
    payments = [pay("p1", "R-1", "100.00", "Acme", received="70.00", linked=["100.00"])]
    r = match(txn(None, "-100.00", "Acme"), payments, TOL)
    assert not r.matched


def test_fuzzy_refund_requires_unreversed_amount():
    payments = [pay("p1", "R-1", "100.00", "Stellar Group", received="0.00", linked=())]
    r = match(txn(None, "-100.00", "Stelar Group"), payments, TOL)
    assert not r.matched


# ----------------------------------------------------------------- tier 2c: one transaction, several invoices

COMBO = [
    pay("c1", "R-1", "100.00", "Acme"),
    pay("c2", "R-2", "250.00", "Acme"),
    pay("c3", "R-3", "75.00", "Acme", received="25.00", linked=["25.00"]),   # 50.00 remaining
    pay("c4", "R-4", "999.00", "Other Co"),
]


def test_amount_equal_to_two_open_balances_splits_across_them():
    r = match(txn(None, "350.00", "ACME"), COMBO, TOL)
    assert [(a.payment_id, a.amount, a.match_type) for a in r.allocations] == [
        ("c1", D("100.00"), M.MULTI_NAME_AMOUNT),
        ("c2", D("250.00"), M.MULTI_NAME_AMOUNT),
    ]


def test_combination_uses_remaining_balance_and_up_to_three_payments():
    r = match(txn(None, "400.00", "Acme"), COMBO, TOL)    # 100 + 250 + 50 remaining
    assert {a.payment_id: a.amount for a in r.allocations} == {"c1": D("100.00"), "c2": D("250.00"), "c3": D("50.00")}


def test_combination_requires_exact_sum_no_fee_tolerance():
    assert not match(txn(None, "349.00", "Acme"), COMBO, TOL).matched


def test_ambiguous_combinations_are_unmatched():
    payments = [pay("a", "R-1", "100.00", "Acme"), pay("b", "R-2", "50.00", "Acme"),
                pay("c", "R-3", "60.00", "Acme"), pay("d", "R-4", "90.00", "Acme")]
    r = match(txn(None, "150.00", "Acme"), payments, TOL)   # a+b or c+d
    assert not r.matched and "several combinations" in r.reason


def test_single_payment_fit_wins_over_combination():
    payments = COMBO + [pay("c5", "R-5", "350.00", "Acme")]
    a = only(match(txn(None, "350.00", "Acme"), payments, TOL))
    assert (a.payment_id, a.match_type) == ("c5", M.NAME_AMOUNT)


def test_combinations_never_cross_payers_or_use_fuzzy_names():
    assert not match(txn(None, "1099.00", "Acme"), COMBO, TOL).matched          # c1 + c4 (Other Co)
    assert not match(txn(None, "350.00", "Acmee Ltd Group"), COMBO, TOL).matched


def test_too_many_open_payments_skips_combination_search():
    payments = [pay(f"p{i}", f"R-{i}", f"{i + 1}.00", "Acme") for i in range(MAX_OPEN_PAYMENTS_FOR_COMBINATIONS + 1)]
    r = match(txn(None, "1000.00", "Acme"), payments, TOL)   # every balance is below the amount
    assert not r.matched and "too many" in r.reason


def test_refund_never_uses_combinations():
    assert not match(txn(None, "-350.00", "Acme"), COMBO, TOL).matched


def test_combination_ignores_payment_settled_within_fee_tolerance():
    # A received 98 of 100: FULLY_PAID within 2%, so its 2.00 shortfall is fees, not a balance.
    payments = [pay("a", "R-1", "100.00", "Acme", received="98.00", linked=["98.00"]),
                pay("b", "R-2", "50.00", "Acme")]
    assert not match(txn(None, "52.00", "Acme"), payments, TOL).matched


def test_balances_larger_than_the_amount_are_pruned_before_searching():
    # 31 open payments, but only two are smaller than the amount: no "too many" refusal.
    payments = [pay(f"big{i}", f"R-{i}", "500.00", "Acme") for i in range(MAX_OPEN_PAYMENTS_FOR_COMBINATIONS)]
    payments += [pay("s1", "S-1", "40.00", "Acme"), pay("s2", "S-2", "60.00", "Acme")]
    r = match(txn(None, "100.00", "Acme"), payments, TOL)
    assert {a.payment_id for a in r.allocations} == {"s1", "s2"}


# ----------------------------------------------------------------- tier 1c: reference typos

def test_typo_in_reference_matches_when_payer_and_amount_agree():
    # REF-2026-6981 with the last two digits swapped.
    a = only(match(txn("REF-2026-6918", "4670.75", "Quantum Dynamics"), PAYMENTS, TOL))
    assert (a.payment_id, a.match_type) == ("p_6981", M.FUZZY_REF)
    assert "1 typo(s)" in a.notes


def test_typo_inside_noisy_text_matches():
    a = only(match(txn("INV REF-2026-6918", "2000.00", "Quantum Dynamics"), PAYMENTS, TOL))
    assert (a.payment_id, a.match_type, a.amount) == ("p_6981", M.FUZZY_REF, D("2000.00"))


def test_typo_allows_partial_payment_but_not_overpayment():
    assert only(match(txn("REF-2026-6918", "100.00", "Quantum Dynamics"), PAYMENTS, TOL)).payment_id == "p_6981"
    r = match(txn("REF-2026-6918", "9000.00", "Quantum Dynamics"), PAYMENTS, TOL)
    assert not r.matched


def test_near_reference_of_another_payer_never_matches():
    # One digit from Quantum's REF-2026-6981, but paid by someone else: likely a different invoice.
    r = match(txn("REF-2026-6982", "4670.75", "Stellar Group"), PAYMENTS, TOL)
    assert not r.matched


def test_typo_waits_out_the_grace_period():
    # The payment REF-2026-6918 may simply not have arrived yet.
    t = TransactionView("txn_1", "REF-2026-6918", D("4670.75"), "USD", "Quantum Dynamics",
                        reference_fallback_allowed=False)
    r = match(t, PAYMENTS, TOL)
    assert not r.matched and "waiting" in r.reason


def test_typo_ambiguous_between_two_payments_is_not_fuzzy_matched():
    payments = [
        pay("p_a", "REF-2026-1000", "500.00", "Acme"),
        pay("p_b", "REF-2026-1002", "500.00", "Acme"),
    ]
    # One edit from both, same payer, amount fits both.
    r = match(txn("REF-2026-1001", "500.00", "Acme"), payments, TOL)
    assert not r.matched


def test_typo_matching_respects_max_edits_setting():
    # Disabled: falls back to payer + amount instead.
    t = txn("REF-2026-6918", "4670.75", "Quantum Dynamics")
    assert only(match(t, PAYMENTS, TOL, reference_max_edits=0)).match_type == M.NAME_AMOUNT
    two_edits = txn("REF-2026-6900", "4670.75", "Quantum Dynamics")
    assert all(a.match_type != M.FUZZY_REF for a in match(two_edits, PAYMENTS, TOL).allocations)
    assert only(match(two_edits, PAYMENTS, TOL, reference_max_edits=2)).match_type == M.FUZZY_REF


def test_typo_needs_currency_match():
    r = match(txn("REF-2026-6918", "4670.75", "Quantum Dynamics", currency="EUR"), PAYMENTS, TOL)
    assert not r.matched


def test_refund_with_typo_reverses_earlier_payment():
    a = only(match(txn("REF-2026-2468", "-53.80", "Prime Logistics"), PAYMENTS, TOL))
    assert (a.payment_id, a.match_type) == ("p_2486", M.FUZZY_REF)
