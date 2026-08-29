"""Input-preservation test — the "don't change any entered data" guarantee.

tests/golden_entered_amounts.json captures every amount/term/payment entered for
every plan, taken before the money refactors. This test asserts the live data
still matches it exactly. Run it after each migration; if it passes, no entered
value was altered.

NOTE: when the money-as-cents migration lands, update `read_total()` etc. to read
the *_cents columns (value / 100) — that single change keeps this test as the
round-trip proof that cents convert back to the identical Rand value.
"""
import json
import os
from northwind.data import database as db

GOLDEN = os.path.join(os.path.dirname(__file__), 'golden_entered_amounts.json')


def _money_equal(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(round(a, 2) - round(b, 2)) <= 0.005


def test_uniform_entered_amounts_unchanged(conn):
    golden = json.load(open(GOLDEN))['uniform']
    drift = []
    for pid, g in golden.items():
        r = conn.execute("SELECT total_amount, monthly_amount, term_months, payments_made "
                         "FROM uniform_deductions WHERE id=?", (pid,)).fetchone()
        assert r is not None, f"uniform {pid} disappeared"
        if not (_money_equal(r['total_amount'], g['total'])
                and _money_equal(r['monthly_amount'], g['monthly'])
                and r['term_months'] == g['term'] and r['payments_made'] == g['paid']):
            drift.append((pid, dict(r), g))
    assert not drift, f"uniform entered amounts changed: {drift[:5]}"


def test_layby_entered_amounts_unchanged(conn):
    golden = json.load(open(GOLDEN))['layby']
    drift = []
    for pid, g in golden.items():
        r = conn.execute("SELECT total_amount, monthly_amount, term_months, payments_made, "
                         "basket_total, discount_pct FROM layby_deductions WHERE id=?",
                         (pid,)).fetchone()
        assert r is not None, f"layby {pid} disappeared"
        if not (_money_equal(r['total_amount'], g['total'])
                and _money_equal(r['monthly_amount'], g['monthly'])
                and r['term_months'] == g['term'] and r['payments_made'] == g['paid']):
            drift.append((pid, dict(r), g))
    assert not drift, f"layby entered amounts changed: {drift[:5]}"


def test_undercharge_entered_amounts_unchanged(conn):
    golden = json.load(open(GOLDEN))['undercharge']
    drift = []
    for pid, g in golden.items():
        r = conn.execute("SELECT total_amount, split_months, payments_made, recovery_method "
                         "FROM undercharges WHERE id=?", (pid,)).fetchone()
        assert r is not None, f"undercharge {pid} disappeared"
        if not (_money_equal(r['total_amount'], g['total'])
                and r['split_months'] == g['split'] and r['payments_made'] == g['paid']
                and r['recovery_method'] == g['method']):
            drift.append((pid, dict(r), g))
    assert not drift, f"undercharge entered amounts changed: {drift[:5]}"
