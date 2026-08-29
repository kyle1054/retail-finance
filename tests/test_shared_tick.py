"""Regression tests for the shared uniform/lay-by tick helpers.

db.tick_uniform_due / db.tick_layby_due are the single source of truth for
ticking those plan types; both the monthly per-employee tick (routes_monthly)
and the reconcile 'tick all' (routes_payroll) call them, so they can never
drift. These tests pin the money invariant that matters most for payroll:
installments always sum EXACTLY to the entered total, with no final-month
balloon and no over-deduction — even when the total is not a clean multiple of
the monthly amount.

Everything runs on the connection's transaction and is rolled back, so the
shared test-DB copy is never mutated.
"""
from northwind.data import database as db


def _mk_employee(conn, emp_id='TEST_TICK_EMP'):
    conn.execute(
        "INSERT INTO employees (id, full_name, current_store, job_title, status, sector) "
        "VALUES (?, 'Tick Test', 'TESTSTORE', 'Staff', 'active', 'retail')",
        (emp_id,))
    return emp_id


def _months(start_year, start_month, n):
    """Yield n consecutive (year, month) pairs starting at start_year/start_month."""
    idx = start_year * 12 + (start_month - 1)
    for _ in range(n):
        yield idx // 12, idx % 12 + 1
        idx += 1


def _sum_txns(conn, plan_type, plan_id):
    rows = conn.execute(
        "SELECT amount FROM deduction_transactions "
        "WHERE plan_type=? AND plan_id=? AND COALESCE(voided,0)=0 ORDER BY year, month",
        (plan_type, plan_id)).fetchall()
    return [r['amount'] for r in rows]


def test_uniform_installments_sum_to_total_no_overshoot(conn):
    emp = _mk_employee(conn)
    try:
        # total 250, monthly 100, term 3 -> 100, 100, 50 (last absorbs remainder)
        cur = conn.execute(
            "INSERT INTO uniform_deductions_cents "
            "(employee_id, description, total_amount_cents, monthly_amount_cents, term_months, "
            " start_month, start_year, payments_made, status, balance_remaining_cents) "
            "VALUES (?, 'u', 25000, 10000, 3, 1, 2025, 0, 'active', 25000)", (emp,))
        plan_id = cur.lastrowid

        for y, m in _months(2025, 1, 4):  # tick one extra month to prove idempotency/stop
            db.tick_uniform_due(conn, emp, y, m)

        amounts = _sum_txns(conn, 'uniform', plan_id)
        assert amounts == [100.0, 100.0, 50.0], amounts
        assert round(sum(amounts), 2) == 250.00
        plan = conn.execute("SELECT status, balance_remaining FROM uniform_deductions WHERE id=?", (plan_id,)).fetchone()
        assert plan['status'] == 'complete'
        assert round(plan['balance_remaining'], 2) == 0.00
    finally:
        conn.rollback()


def test_layby_final_month_capped_no_balloon(conn):
    emp = _mk_employee(conn)
    try:
        # total 250, monthly 100, term 3 -> 100, 100, 50 (capped at balance, no balloon/overshoot)
        cur = conn.execute(
            "INSERT INTO layby_deductions_cents "
            "(employee_id, description, total_amount_cents, monthly_amount_cents, term_months, "
            " start_month, start_year, payments_made, status, balance_remaining_cents) "
            "VALUES (?, 'l', 25000, 10000, 3, 1, 2025, 0, 'active', 25000)", (emp,))
        plan_id = cur.lastrowid

        for y, m in _months(2025, 1, 4):
            db.tick_layby_due(conn, emp, y, m)

        amounts = _sum_txns(conn, 'layby', plan_id)
        # No single installment exceeds the regular monthly, and they sum to the total.
        assert all(a <= 100.0 + 1e-9 for a in amounts), amounts
        assert round(sum(amounts), 2) == 250.00, amounts
        plan = conn.execute("SELECT status, balance_remaining FROM layby_deductions WHERE id=?", (plan_id,)).fetchone()
        assert plan['status'] == 'complete'
        assert round(plan['balance_remaining'], 2) == 0.00
    finally:
        conn.rollback()


def test_clean_multiple_sums_exactly(conn):
    emp = _mk_employee(conn)
    try:
        # total 1200, monthly 100, term 12 -> twelve equal 100s
        cur = conn.execute(
            "INSERT INTO uniform_deductions_cents "
            "(employee_id, description, total_amount_cents, monthly_amount_cents, term_months, "
            " start_month, start_year, payments_made, status, balance_remaining_cents) "
            "VALUES (?, 'u', 120000, 10000, 12, 1, 2025, 0, 'active', 120000)", (emp,))
        plan_id = cur.lastrowid
        for y, m in _months(2025, 1, 12):
            db.tick_uniform_due(conn, emp, y, m)
        amounts = _sum_txns(conn, 'uniform', plan_id)
        assert len(amounts) == 12
        assert round(sum(amounts), 2) == 1200.00
    finally:
        conn.rollback()


def test_idempotent_within_month(conn):
    emp = _mk_employee(conn)
    try:
        cur = conn.execute(
            "INSERT INTO layby_deductions_cents "
            "(employee_id, description, total_amount_cents, monthly_amount_cents, term_months, "
            " start_month, start_year, payments_made, status, balance_remaining_cents) "
            "VALUES (?, 'l', 30000, 10000, 3, 1, 2025, 0, 'active', 30000)", (emp,))
        plan_id = cur.lastrowid
        n1 = db.tick_layby_due(conn, emp, 2025, 1)
        n2 = db.tick_layby_due(conn, emp, 2025, 1)  # same month again
        assert n1 == 1 and n2 == 0
        amounts = _sum_txns(conn, 'layby', plan_id)
        assert amounts == [100.0], amounts
    finally:
        conn.rollback()
