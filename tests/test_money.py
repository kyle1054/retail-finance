"""Unit tests for money.py — the integer-cents arithmetic that guarantees a
plan's installments always sum back to the entered total with no drift.

These run pure-function math; no database is touched.
"""
from northwind.services import money


# --- to_cents / to_rands round-trip ---------------------------------------

def test_to_cents_basic():
    assert money.to_cents(0) == 0
    assert money.to_cents(1) == 100
    assert money.to_cents(12.34) == 1234
    assert money.to_cents(0.1) == 10


def test_to_cents_rounds_half_cent():
    # Float representation of money should round to the nearest cent.
    assert money.to_cents(0.005) in (0, 1)  # banker's/standard rounding either way
    assert money.to_cents(2.675) == 268 or money.to_cents(2.675) == 267


def test_to_rands_basic():
    assert money.to_rands(0) == 0
    assert money.to_rands(100) == 1
    assert money.to_rands(1234) == 12.34


def test_round_trip_preserves_value():
    for rands in (0, 1, 12.34, 99.99, 1611.0, 810.48, 1000000.0):
        assert money.to_rands(money.to_cents(rands)) == round(rands, 2)


def test_none_handling():
    assert money.to_cents(None) is None
    assert money.to_rands(None) is None


# --- total_cents -----------------------------------------------------------

def test_total_cents_uses_entered_total():
    assert money.total_cents(1200.0, 200.0, 6) == 120000


def test_total_cents_falls_back_to_term_times_monthly():
    assert money.total_cents(None, 200.0, 6) == 120000


def test_total_cents_zero_term_fallback():
    assert money.total_cents(None, 200.0, 0) == 0


# --- installment_cents: last installment absorbs the remainder -------------

def test_installments_sum_to_total_clean():
    total, monthly, term = 1200.0, 200.0, 6
    sched = money.schedule_cents(total, monthly, term)
    assert len(sched) == term
    assert sum(sched) == money.to_cents(total)
    assert all(c == 20000 for c in sched)


def test_installments_sum_to_total_with_remainder():
    # 1100 over 3 months => 366.67/month doesn't divide evenly.
    total, monthly, term = 1100.0, 366.67, 3
    sched = money.schedule_cents(total, monthly, term)
    assert sum(sched) == money.to_cents(total)
    # First (term-1) equal the entered monthly; last carries the odd cents.
    assert sched[0] == money.to_cents(monthly)
    assert sched[1] == money.to_cents(monthly)
    assert sched[-1] != sched[0]


def test_installment_index_out_of_normal_range_returns_monthly():
    # Indices that are not the final month return the plain monthly amount.
    assert money.installment_cents(1200.0, 200.0, 6, 0) == 20000
    assert money.installment_cents(1200.0, 200.0, 6, 4) == 20000


def test_single_month_plan_is_the_whole_total():
    sched = money.schedule_cents(999.99, 999.99, 1)
    assert sched == [money.to_cents(999.99)]
    assert sum(sched) == 99999


def test_zero_term_schedule_is_empty():
    assert money.schedule_cents(0, 0, 0) == []
    assert money.schedule_cents(None, 100.0, 0) == []


def test_large_amount_reconciles():
    total, monthly, term = 1_000_000.00, 83_333.33, 12
    sched = money.schedule_cents(total, monthly, term)
    assert sum(sched) == money.to_cents(total)


# --- uniform_balance_cents -------------------------------------------------

def test_uniform_balance_honours_stored_balance():
    # When balance_remaining is set, it is the source of truth.
    assert money.uniform_balance_cents(1200.0, 200.0, 6, 2, balance_remaining=500.0) == 50000


def test_uniform_balance_computed_when_no_stored_balance():
    # 1200 total, 200/mo, 2 paid => 800 remaining.
    assert money.uniform_balance_cents(1200.0, 200.0, 6, 2) == 80000


def test_uniform_balance_zero_when_fully_paid():
    assert money.uniform_balance_cents(1200.0, 200.0, 6, 6) == 0
    assert money.uniform_balance_cents(1200.0, 200.0, 6, 7) == 0
