"""Matcher tests. Cases marked 'observed' come from real provider data (DESIGN.md §4)."""

from decimal import Decimal as D

from app.domain.status import Tolerance
from app.reconciliation.matcher import MatchType as M, PaymentView, TransactionView, match

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


def test_partial_payment_links_full_transaction_amount():     # observed: REF-2026-2486
    a = only(match(txn("REF-2026-2486", "75.87", "Prime Logistics"), PAYMENTS, TOL))
    assert (a.payment_id, a.amount) == ("p_2486", D("75.87"))


def test_overpayment_by_reference_still_links():
    a = only(match(txn("ORD-2026-9137", "100.00", "Pinnacle Enterprises"), PAYMENTS, TOL))
    assert a.payment_id == "p_9137"   # status will become OVERPAID


def test_unknown_reference_waits_instead_of_name_matching():
    # Well-formed ref with no payment yet: must NOT fall back to name+amount,
    # even though Apex has an open payment of exactly this amount.
    r = match(txn("INV-2026-8490", "4153.15", "Apex Trading Co"), PAYMENTS, TOL)
    assert not r.matched
    assert "INV-2026-8490" in r.reason


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
