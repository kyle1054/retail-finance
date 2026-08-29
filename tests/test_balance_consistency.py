"""Every read path that reports an employee's outstanding balance must agree.

These tests pin the phase-2 refactor: get_outstanding_summary, get_top_debtors,
get_all_outstanding_totals and get_category_totals all derive their figures from
database._outstanding_by_employee / money.py, so the employee page, dashboard
and top-debtor lists can never show different numbers for the same person.
"""
from northwind.data import database as db
from northwind.services import money


def _all_employees(conn):
    return [(r['id'], r['sector'] or 'retail')
            for r in conn.execute("SELECT id, sector FROM employees")]


def test_summary_matches_all_outstanding_totals(conn):
    totals = {}
    totals.update(db.get_all_outstanding_totals('retail'))
    totals.update(db.get_all_outstanding_totals('hq'))
    for emp_id, _ in _all_employees(conn):
        summary = db.get_outstanding_summary(emp_id)
        assert totals[emp_id] == summary['total'], emp_id


def test_top_debtors_match_summary(conn):
    for sector in ('retail', 'hq'):
        for d in db.get_top_debtors(limit=1000, sector=sector):
            s = db.get_outstanding_summary(d['id'])
            assert d['total'] == s['total'], d['id']
            assert d['uniform_out'] == s['uniform'], d['id']
            assert d['layby_out'] == s['layby'], d['id']
            assert d['uc_out'] == s['undercharges'], d['id']


def test_category_totals_remaining_matches_summary(conn):
    for emp_id, _ in _all_employees(conn):
        cat = db.get_category_totals(emp_id)
        s = db.get_outstanding_summary(emp_id)
        assert cat['uniform']['remaining'] == s['uniform'], emp_id
        assert cat['layby']['remaining'] == s['layby'], emp_id
        assert cat['undercharges']['remaining'] == s['undercharges'], emp_id


def test_top_debtors_sorted_and_positive(conn):
    debtors = db.get_top_debtors(limit=1000, sector='retail')
    totals = [d['total'] for d in debtors]
    assert totals == sorted(totals, reverse=True)
    assert all(t > 0 for t in totals)


# --- money.py helpers added in phase 2 --------------------------------------

def test_layby_balance_honours_stored_balance():
    assert money.layby_balance_cents(500.0, 100.0, 5, 1, balance_remaining=250.0) == 25000


def test_layby_balance_fallback_unpaid_terms():
    assert money.layby_balance_cents(500.0, 100.0, 5, 2, balance_remaining=None) == 30000


def test_layby_balance_overpaid_clamps_to_zero():
    assert money.layby_balance_cents(500.0, 100.0, 5, 7, balance_remaining=None) == 0


def test_undercharge_outstanding_full():
    assert money.undercharge_outstanding_cents(1000.0, 'full', 1, 0) == 100000


def test_undercharge_outstanding_split():
    # 1395 over 3 months, one paid -> two thirds remain (930.00).
    assert money.undercharge_outstanding_cents(1395.0, 'split', 3, 1) == 93000
