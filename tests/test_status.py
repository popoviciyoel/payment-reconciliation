from decimal import Decimal as D

import pytest

from app.domain.status import PaymentStatus as S, Tolerance, compute_status, settle

TOL_2PCT = Tolerance(percent=D("2"))


@pytest.mark.parametrize("received, expected_status", [
    ("0", S.PENDING),
    ("-10.00", S.PENDING),            # refunded below zero
    ("500.00", S.PARTIALLY_PAID),
    ("979.99", S.PARTIALLY_PAID),     # just outside 2%
    ("980.00", S.FULLY_PAID),         # exactly at 2% allowance
    ("1000.00", S.FULLY_PAID),
    ("1000.01", S.OVERPAID),
])
def test_status_with_2pct_tolerance(received, expected_status):
    assert compute_status(D("1000.00"), D(received), TOL_2PCT) is expected_status


def test_zero_tolerance_requires_full_amount():
    assert compute_status(D("100"), D("99.99"), Tolerance()) is S.PARTIALLY_PAID


def test_fixed_allowance_wins_when_larger():
    tol = Tolerance(percent=D("1"), fixed=D("5"))  # 1% of 100 = 1 < 5
    assert compute_status(D("100"), D("95"), tol) is S.FULLY_PAID
    assert compute_status(D("100"), D("94.99"), tol) is S.PARTIALLY_PAID


def test_observed_short_payments():
    """DESIGN.md §4: which 'slightly short' payments count as paid at 2%."""
    assert compute_status(D("2583.76"), D("2544.75"), TOL_2PCT) is S.FULLY_PAID       # -1.5%
    assert compute_status(D("1410.45"), D("1348.80"), TOL_2PCT) is S.PARTIALLY_PAID   # -4.4%
    assert compute_status(D("2874.77"), D("2735.86"), TOL_2PCT) is S.PARTIALLY_PAID   # -4.8%


def test_tolerance_covers():
    assert TOL_2PCT.covers(D("98"), D("100"))
    assert TOL_2PCT.covers(D("100"), D("100"))
    assert not TOL_2PCT.covers(D("100.01"), D("100"))
    assert not TOL_2PCT.covers(D("97.99"), D("100"))


# A refund first gives back any overpayment; the rest reduces the amount due (DESIGN Q1).
@pytest.mark.parametrize("amounts, due, received, status", [
    ([], "100.00", "0", S.PENDING),
    (["100.00", "-30.00"], "70.00", "70.00", S.FULLY_PAID),       # partial refund: settled, not reopened
    (["120.00", "-20.00"], "100.00", "100.00", S.FULLY_PAID),     # refund of the overpayment (Q3)
    (["150.00", "-60.00"], "90.00", "90.00", S.FULLY_PAID),       # 50 overpayment back, 10 off the due
    (["50.00", "-10.00"], "90.00", "40.00", S.PARTIALLY_PAID),    # still owes 50, as before the refund
    (["100.00", "-100.00"], "0.00", "0.00", S.FULLY_PAID),        # fully refunded: nothing owed
    (["-30.00"], "70.00", "-30.00", S.PENDING),                   # refund linked before its payment
    (["-30.00", "100.00"], "70.00", "70.00", S.FULLY_PAID),       # ...then the payment arrives
    (["100.00", "-30.00", "40.00"], "70.00", "110.00", S.OVERPAID),  # paid again after a credit
])
def test_settle_applies_refunds(amounts, due, received, status):
    s = settle(D("100.00"), [D(a) for a in amounts])
    assert (s.amount_due, s.received) == (D(due), D(received))
    assert compute_status(s.amount_due, s.received, Tolerance()) is status


def test_nothing_received_is_pending_even_within_the_fee_allowance():
    # Review regression: a fixed allowance larger than the amount due.
    assert compute_status(D("3.00"), D("0"), Tolerance(fixed=D("5"))) is S.PENDING
    assert compute_status(D("3.00"), D("-1.00"), Tolerance(fixed=D("5"))) is S.PENDING
