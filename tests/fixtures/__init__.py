"""Synthetic data for a Northwind Deductions database.

    from northwind.data import database as db
    import fixtures

    conn = db.get_db()
    fixtures.seed(conn)                  # the test dataset
    fixtures.seed(conn, scale='demo')    # a bigger one, same shapes

Why this exists
---------------
The test suite needs a populated database — plans at every stage, a payroll
ledger that reconciles, cash floats, card statements — and it must not be a copy
of anyone's real one. This package builds that database from nothing, so a fresh
clone can run the whole suite with no data handed to it.

Design rules
------------
* **Deterministic.** No ``random``. Every value comes from an index, so two runs
  on the same day produce byte-identical data and a failure is reproducible.
* **Anchored, not frozen.** Months are offsets from the first of the current
  month (see ``calendar_math``), because several tests ask the app what comes off
  *next* pay and what is *already* overdue. Offsets are coarse enough that no
  assertion sits on a boundary a single day could cross.
* **Cents in, Rands out.** Writes go to the ``*_cents`` base tables; the Rands
  views are read-only.
* **Borrow the app's writers.** Where a fact spans tables — an undercharge's
  schedule and its ledger row, a write-off's event, a plan adjustment's audit
  row — the seed calls the same function the app calls. That is what makes
  ``tools/db_check.py`` clean by construction instead of by inspection.
* **Scalable.** ``scale`` selects a profile; the generators are the same code, so
  a demo dataset cannot drift in shape from the tested one.

Every name, merchant, SKU and email is invented (see ``names.py``).
"""
from northwind.data import database as db

from . import cards, cash, deductions, people
from .calendar_math import ANCHOR

__all__ = ['seed', 'SCALES', 'summary_of']


SCALES = {
    # Sized for the suite: comfortably more than one pagination window on each
    # windowed list (50 rows), enough spare staff for the request tests to each
    # take a fresh employee, and at least two of everything that gets compared
    # against a sibling row.
    'test': {
        'retail_employees': 72,
        'hq_employees': 12,
        'terminated_employees': 8,
        'arrears_employees': 4,
        'uniform_plans': 86,
        'layby_plans': 64,
        'undercharges': 66,
        'overcharges': 5,
        'overpayments': 6,
        'requests': 6,
        'recon_stores': 8,
        'recon_months': 3,
        'cards': 4,
        'statements_per_card': 2,
        'lines_per_statement': 9,
        'allowance_years': 2,
        'merchant_memory': False,
    },
    # A populated-looking demo: same generators, bigger numbers. Kept here (not
    # in a separate script) so the demo can never exercise untested shapes.
    'demo': {
        'retail_employees': 240,
        'hq_employees': 30,
        'terminated_employees': 40,
        'arrears_employees': 12,
        'uniform_plans': 420,
        'layby_plans': 300,
        'undercharges': 260,
        'overcharges': 18,
        'overpayments': 24,
        'requests': 20,
        'recon_stores': 20,
        'recon_months': 6,
        'cards': 4,
        'statements_per_card': 4,
        'lines_per_statement': 18,
        'allowance_years': 3,
        'merchant_memory': True,
    },
}


def summary_of(result):
    """One-line description of what a seed run produced (for -s / scripts)."""
    return ('%(employees)d employees, %(uniform)d uniform, %(layby)d lay-by, '
            '%(undercharge)d undercharge, %(cash_entries)d cash entries, '
            '%(cc_lines)d card lines' % result)


def seed(conn, scale='test'):
    """Populate an empty (migrated) database. Returns a counts dict.

    `conn` must be a live connection on the database ``database.DB_PATH`` points
    at, because some of the app writers this calls open their own connection to
    that same path. The seed commits between phases for that reason.
    """
    if scale not in SCALES:
        raise ValueError('unknown seed scale %r (have %s)'
                         % (scale, ', '.join(sorted(SCALES))))
    profile = SCALES[scale]

    # ── People and places ────────────────────────────────────────────────────
    stores = people.seed_stores(conn)
    people.seed_store_logins(conn, stores)
    roster = people.seed_employees(conn, profile, stores)
    people.seed_employee_logins(conn, roster)
    people.seed_transfer_history(conn, roster)
    people.seed_regional_managers(conn, stores)
    people.seed_admins(conn)
    deductions.seed_locked_periods(conn)
    conn.commit()
    db.invalidate_stores_cache()

    # ── Plans ────────────────────────────────────────────────────────────────
    arrears = roster['retail'][:profile['arrears_employees']]
    uniform_plans = deductions.seed_uniform_plans(conn, profile, roster)
    uniform_plans += deductions.seed_arrears_uniform_plans(conn, arrears)
    layby_plans = deductions.seed_layby_plans(conn, profile, roster)
    undercharges, overcharges = deductions.seed_undercharges(conn, profile, roster)
    deductions.seed_overpayments(conn, profile, roster)
    conn.commit()

    # ── Payroll history ──────────────────────────────────────────────────────
    deductions.run_payroll(conn, roster, {e['id'] for e in arrears})
    deductions.void_a_transaction(conn)
    conn.commit()

    deductions.write_off_some(conn, uniform_plans, layby_plans, undercharges)
    deductions.adjust_some(conn, uniform_plans, layby_plans)
    conn.commit()

    # ── HQ allowances and the request queue ──────────────────────────────────
    years = [ANCHOR.year - offset
             for offset in range(profile['allowance_years'])]
    deductions.seed_allowances(conn, roster, years)
    conn.commit()
    deductions.seed_requests(conn, roster, profile['requests'])
    conn.commit()

    # ── Cash reconciliation ──────────────────────────────────────────────────
    cash_result = cash.seed(conn, profile, stores)
    cash.seed_settings(conn)
    cash.seed_shopify_comparison(conn, stores)
    conn.commit()

    # ── Credit cards (its writers own their own connections) ─────────────────
    card_result = cards.seed(conn, profile)
    conn.commit()

    def _count(table):
        return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]

    return {
        'scale': scale,
        'stores': len(stores),
        'employees': _count('employees'),
        'uniform': _count('uniform_deductions_cents'),
        'layby': _count('layby_deductions_cents'),
        'undercharge': _count('undercharges_cents'),
        'transactions': _count('deduction_transactions_cents'),
        'requests': _count('staff_requests'),
        'cash_entries': cash_result['entries'],
        'cc_cards': card_result['cards'],
        'cc_lines': _count('cc_lines'),
        'cc_receipts': _count('cc_receipts'),
    }
