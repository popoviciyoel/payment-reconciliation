from decimal import Decimal as D

import pytest

from app.domain.status import PaymentStatus as S, Tolerance, compute_status

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
