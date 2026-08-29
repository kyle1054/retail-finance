"""Ledger-safe undercharge rescheduling and settlement timelines."""

from northwind.data import database as db


YEAR = 2095


def _employee(conn):
    return conn.execute(
        "SELECT id FROM employees WHERE sector='retail' AND status='active' LIMIT 1"
    ).fetchone()['id']


def _plan(conn, employee_id, total_cents=100000, months=4, start_month=1):
    cur = conn.execute(
        "INSERT INTO undercharges_cents "
        "(employee_id,total_amount_cents,reason,incident_month,incident_year,"
        "start_month,start_year,recovery_method,split_months,payments_made,status,type) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,'pending','undercharge')",
        (employee_id, total_cents, 'Timeline test', start_month, YEAR,
         start_month, YEAR, 'split' if months > 1 else 'full', months))
    db.ensure_undercharge_schedule(conn, cur.lastrowid)
    conn.commit()
    return cur.lastrowid


def _cleanup(conn, plan_id):
    conn.execute(
        "DELETE FROM deduction_transactions_cents "
        "WHERE plan_type='undercharge' AND plan_id=?", (plan_id,))
    conn.execute("DELETE FROM undercharges_cents WHERE id=?", (plan_id,))
    conn.commit()


def _tick(conn, employee_id, month):
    with conn:
        assert db.tick_undercharges_due(conn, employee_id, YEAR, month) == 1


def test_reschedule_preserves_irregular_actual_payment(client, conn):
    """R1,347.50 minus the actual R673.25 ledger row becomes exactly
    three R224.75 installments; the historical transaction is untouched."""
    employee_id = _employee(conn)
    plan_id = _plan(conn, employee_id, total_cents=134750, months=2, start_month=7)
    try:
        item = conn.execute(
            "SELECT id FROM undercharge_schedule_items WHERE undercharge_id=? "
            "AND due_month=7", (plan_id,)).fetchone()
        tx = conn.execute(
            "INSERT INTO deduction_transactions_cents "
            "(plan_type,plan_id,employee_id,amount_cents,year,month) "
            "VALUES ('undercharge',?,?,?,?,?)",
            (plan_id, employee_id, 67325, YEAR, 7))
        conn.execute(
            "UPDATE undercharge_schedule_items SET transaction_id=? WHERE id=?",
            (tx.lastrowid, item['id']))
        db.sync_undercharge_state(conn, plan_id)
        conn.commit()

        response = client.post(
            f'/undercharge/{plan_id}/reschedule',
            data={'start_year': YEAR, 'start_month': 8, 'months': 3,
                  'reason': 'Smaller remaining deductions'})
        assert response.status_code in (302, 303)

        actual = conn.execute(
            "SELECT amount_cents FROM deduction_transactions_cents WHERE id=?",
            (tx.lastrowid,)).fetchone()['amount_cents']
        assert actual == 67325
        future = conn.execute(
            "SELECT due_month,amount_cents FROM undercharge_schedule_items "
            "WHERE undercharge_id=? AND state='scheduled' "
            "AND transaction_id IS NULL AND amount_cents>0 "
            "ORDER BY due_year,due_month", (plan_id,)).fetchall()
        assert [(r['due_month'], r['amount_cents']) for r in future] == [
            (8, 22475), (9, 22475), (10, 22475)]
        account = db.get_undercharge_account(plan_id, conn)
        assert account['payroll_deducted_cents'] == 67325
        assert account['remaining_cents'] == 67425
    finally:
        _cleanup(conn, plan_id)


def test_rescheduled_installments_tick_to_exact_total(client, conn):
    employee_id = _employee(conn)
    plan_id = _plan(conn, employee_id, total_cents=100001, months=2, start_month=1)
    try:
        _tick(conn, employee_id, 1)  # 50,000; 50,001 remains
        response = client.post(
            f'/undercharge/{plan_id}/reschedule',
            data={'start_year': YEAR, 'start_month': 2, 'months': 3,
                  'reason': 'Three smaller payments'})
        assert response.status_code in (302, 303)
        future = conn.execute(
            "SELECT amount_cents FROM undercharge_schedule_items "
            "WHERE undercharge_id=? AND state='scheduled' "
            "AND transaction_id IS NULL ORDER BY due_month",
            (plan_id,)).fetchall()
        assert [r['amount_cents'] for r in future] == [16667, 16667, 16667]
        for month in (2, 3, 4):
            _tick(conn, employee_id, month)
        account = db.get_undercharge_account(plan_id, conn)
        assert account['payroll_deducted_cents'] == 100001
        assert account['remaining_cents'] == 0
        assert account['status'] == 'recovered'
    finally:
        _cleanup(conn, plan_id)


def test_customer_pays_full_after_two_deductions_refunds_exact_ledger(client, conn):
    employee_id = _employee(conn)
    plan_id = _plan(conn, employee_id, total_cents=100000, months=4)
    try:
        _tick(conn, employee_id, 1)
        _tick(conn, employee_id, 2)
        response = client.post(
            f'/undercharge/{plan_id}/customer-paid',
            headers={'Accept': 'application/json'},
            data={'customer_amount': '1000.00', 'refund': 'yes',
                  'reimburse_year': YEAR, 'reimburse_month': 6})
        payload = response.get_json()
        assert payload['success']
        refund = conn.execute(
            "SELECT amount_cents FROM undercharge_schedule_items "
            "WHERE undercharge_id=? AND state='scheduled' "
            "AND transaction_id IS NULL AND amount_cents<0",
            (plan_id,)).fetchone()
        assert refund['amount_cents'] == -50000
        assert not conn.execute(
            "SELECT 1 FROM undercharge_schedule_items WHERE undercharge_id=? "
            "AND state='scheduled' AND transaction_id IS NULL AND amount_cents>0",
            (plan_id,)).fetchone()

        monthly = db.get_monthly_data(YEAR, 6)
        row = next(d for d in monthly if d['employee']['id'] == employee_id)
        assert row['undercharge_total'] == -500.0
        _tick(conn, employee_id, 6)
        account = db.get_undercharge_account(plan_id, conn)
        assert account['customer_paid_cents'] == 100000
        assert account['payroll_deducted_cents'] == 50000
        assert account['payroll_refunded_cents'] == 50000
        assert account['net_employee_paid_cents'] == 0
        assert account['remaining_cents'] == 0
        assert account['refund_due_cents'] == 0
        assert account['status'] == 'reimbursed'
    finally:
        _cleanup(conn, plan_id)


def test_customer_pays_only_remaining_balance_no_refund(client, conn):
    employee_id = _employee(conn)
    plan_id = _plan(conn, employee_id, total_cents=100000, months=4)
    try:
        _tick(conn, employee_id, 1)
        _tick(conn, employee_id, 2)
        payload = client.post(
            f'/undercharge/{plan_id}/customer-paid',
            headers={'Accept': 'application/json'},
            data={'customer_amount': '500.00'}).get_json()
        assert payload['success'] and payload['no_reimbursement']
        account = db.get_undercharge_account(plan_id, conn)
        assert account['remaining_cents'] == 0
        assert account['refund_due_cents'] == 0
        assert account['scheduled_refunds_cents'] == 0
    finally:
        _cleanup(conn, plan_id)


def test_partial_customer_payment_rebuilds_only_future_balance(client, conn):
    employee_id = _employee(conn)
    plan_id = _plan(conn, employee_id, total_cents=100000, months=4)
    try:
        _tick(conn, employee_id, 1)
        _tick(conn, employee_id, 2)
        payload = client.post(
            f'/undercharge/{plan_id}/customer-paid',
            headers={'Accept': 'application/json'},
            data={'customer_amount': '300.00'}).get_json()
        assert payload['success']
        account = db.get_undercharge_account(plan_id, conn)
        assert account['customer_paid_cents'] == 30000
        assert account['remaining_cents'] == 20000
        future = conn.execute(
            "SELECT due_month,amount_cents FROM undercharge_schedule_items "
            "WHERE undercharge_id=? AND state='scheduled' "
            "AND transaction_id IS NULL AND amount_cents>0 ORDER BY due_month",
            (plan_id,)).fetchall()
        assert [(r['due_month'], r['amount_cents']) for r in future] == [
            (3, 10000), (4, 10000)]
    finally:
        _cleanup(conn, plan_id)


def test_locked_month_rejects_reschedule(client, conn):
    employee_id = _employee(conn)
    plan_id = _plan(conn, employee_id, total_cents=60000, months=2)
    conn.execute(
        "INSERT OR IGNORE INTO locked_periods(sector,year,month) "
        "VALUES ('retail',?,?)", (YEAR, 3))
    conn.commit()
    try:
        response = client.post(
            f'/undercharge/{plan_id}/reschedule',
            data={'start_year': YEAR, 'start_month': 3, 'months': 2,
                  'reason': 'Should be rejected'})
        assert response.status_code in (302, 303)
        assert conn.execute(
            "SELECT COUNT(*) FROM undercharge_schedule_revisions "
            "WHERE undercharge_id=?", (plan_id,)).fetchone()[0] == 1
    finally:
        conn.execute(
            "DELETE FROM locked_periods WHERE sector='retail' AND year=? AND month=?",
            (YEAR, 3))
        _cleanup(conn, plan_id)
