"""Customer-paid / reimbursement lifecycle for undercharges.

Pins the rules added after the undercharges deep-dive:
- customer paid BEFORE any deduction -> resolved with zero payroll impact
  (no reimbursement month, never appears in any monthly sheet, and can
  never be deducted afterwards);
- customer paid AFTER a deduction -> a reimbursement month is mandatory,
  must not be locked, and must not clash with the month of the deduction;
- reverting a reimbursed undercharge voids the payback transaction so the
  ledger and the plan status always agree.
"""
from northwind.data import database as db


def _mk_uc(conn, emp_id, status='pending', payments_made=0, total=100.0,
           method='full', split=1, month=5, year=2026, **extra):
    cols = dict(employee_id=emp_id, total_amount_cents=db.to_cents(total),
                recovery_method=method, split_months=split, payments_made=payments_made,
                status=status, incident_month=month, incident_year=year,
                start_month=month, start_year=year, type='undercharge', **extra)
    cur = conn.execute(
        f"INSERT INTO undercharges_cents ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})", list(cols.values()))
    conn.commit()
    return cur.lastrowid


def _emp(conn):
    return conn.execute("SELECT id FROM employees WHERE sector='retail' AND status='active' LIMIT 1").fetchone()['id']


def _get(conn, uc_id):
    return conn.execute("SELECT * FROM undercharges_cents WHERE id=?", (uc_id,)).fetchone()


def test_customer_paid_before_deduction_has_no_payroll_impact(client, conn):
    emp = _emp(conn)
    uc_id = _mk_uc(conn, emp, status='pending', payments_made=0)

    r = client.post(f'/undercharge/{uc_id}/customer-paid',
                    headers={'Accept': 'application/json'},
                    data={'reimburse_month': '6', 'reimburse_year': '2026'})
    data = r.get_json()
    assert data['success'] and data['no_reimbursement']

    row = _get(conn, uc_id)
    assert row['status'] == 'paid_by_customer'
    assert row['reimburse_month'] is None and row['reimburse_year'] is None

    # It must never be deducted nor reimbursed in any month.
    for y, m in [(2026, 5), (2026, 6), (2026, 7)]:
        for d in db.get_monthly_data(y, m):
            assert not any(u['id'] == uc_id for u in d['undercharge_rows'])
    assert db.get_outstanding_summary(emp)['undercharges'] >= 0  # and excludes this row
    conn.execute("DELETE FROM undercharges_cents WHERE id=?", (uc_id,)); conn.commit()


def test_customer_paid_after_deduction_requires_month(client, conn):
    emp = _emp(conn)
    uc_id = _mk_uc(conn, emp, status='recovered', payments_made=1)

    r = client.post(f'/undercharge/{uc_id}/customer-paid',
                    headers={'Accept': 'application/json'}, data={})
    assert r.get_json()['success'] is False
    assert _get(conn, uc_id)['status'] == 'recovered'  # unchanged
    conn.execute("DELETE FROM undercharges_cents WHERE id=?", (uc_id,)); conn.commit()


def test_customer_paid_explicit_no_refund_resolves_without_payback(client, conn):
    """Explicit 'No refund' is allowed even when money was deducted: the amount
    already deducted stays, no reimbursement month, no negative transaction."""
    emp = _emp(conn)
    uc_id = _mk_uc(conn, emp, status='recovered', payments_made=1)
    try:
        r = client.post(f'/undercharge/{uc_id}/customer-paid',
                        headers={'Accept': 'application/json'},
                        data={'refund': 'no'})
        data = r.get_json()
        assert data['success'] and data['no_reimbursement']
        row = _get(conn, uc_id)
        assert row['status'] == 'paid_by_customer'
        assert row['reimburse_month'] is None and row['reimburse_year'] is None
        # No payback transaction should exist.
        neg = conn.execute(
            "SELECT COUNT(*) c FROM deduction_transactions_cents WHERE plan_type='undercharge' "
            "AND plan_id=? AND amount_cents < 0 AND COALESCE(voided,0)=0", (uc_id,)).fetchone()['c']
        assert neg == 0
    finally:
        conn.execute("DELETE FROM undercharges_cents WHERE id=?", (uc_id,)); conn.commit()


def test_customer_paid_rejects_locked_reimburse_month(client, conn):
    emp = _emp(conn)
    uc_id = _mk_uc(conn, emp, status='recovered', payments_made=1)
    conn.execute("INSERT OR IGNORE INTO locked_periods (sector, year, month) VALUES ('retail', 2026, 8)")
    conn.commit()
    try:
        r = client.post(f'/undercharge/{uc_id}/customer-paid',
                        headers={'Accept': 'application/json'},
                        data={'reimburse_month': '8', 'reimburse_year': '2026'})
        assert r.get_json()['success'] is False
        assert _get(conn, uc_id)['status'] == 'recovered'
    finally:
        conn.execute("DELETE FROM locked_periods WHERE sector='retail' AND year=2026 AND month=8")
        conn.execute("DELETE FROM undercharges_cents WHERE id=?", (uc_id,))
        conn.commit()


def test_customer_paid_rejects_month_clashing_with_deduction(client, conn):
    emp = _emp(conn)
    uc_id = _mk_uc(conn, emp, status='recovered', payments_made=1)
    conn.execute(
        "INSERT INTO deduction_transactions_cents (plan_type, plan_id, employee_id, amount_cents, year, month) "
        "VALUES ('undercharge', ?, ?, 10000, 2026, 5)", (uc_id, emp))
    conn.commit()
    try:
        r = client.post(f'/undercharge/{uc_id}/customer-paid',
                        headers={'Accept': 'application/json'},
                        data={'reimburse_month': '5', 'reimburse_year': '2026'})
        data = r.get_json()
        assert data['success'] is False and 'already' in data['message']
        # A later month is fine.
        r = client.post(f'/undercharge/{uc_id}/customer-paid',
                        headers={'Accept': 'application/json'},
                        data={'reimburse_month': '6', 'reimburse_year': '2026'})
        assert r.get_json()['success'] is True
        row = _get(conn, uc_id)
        assert (row['reimburse_year'], row['reimburse_month']) == (2026, 6)
    finally:
        conn.execute("DELETE FROM deduction_transactions_cents WHERE plan_type='undercharge' AND plan_id=?", (uc_id,))
        conn.execute("DELETE FROM undercharges_cents WHERE id=?", (uc_id,))
        conn.commit()


def test_revert_reimbursed_voids_payback_transaction(client, conn):
    emp = _emp(conn)
    uc_id = _mk_uc(conn, emp, status='reimbursed', payments_made=1,
                   reimburse_month=6, reimburse_year=2026)
    conn.execute(
        "INSERT INTO deduction_transactions_cents (plan_type, plan_id, employee_id, amount_cents, year, month) "
        "VALUES ('undercharge', ?, ?, -10000, 2026, 6)", (uc_id, emp))
    conn.commit()
    try:
        r = client.post(f'/undercharge/{uc_id}/revert')
        assert r.status_code in (302, 303)
        row = _get(conn, uc_id)
        assert row['status'] == 'recovered'  # payments_made == split_months
        voided = conn.execute(
            "SELECT voided FROM deduction_transactions_cents WHERE plan_type='undercharge' "
            "AND plan_id=? AND amount_cents < 0", (uc_id,)).fetchone()['voided']
        assert voided == 1
    finally:
        conn.execute("DELETE FROM deduction_transactions_cents WHERE plan_type='undercharge' AND plan_id=?", (uc_id,))
        conn.execute("DELETE FROM undercharges_cents WHERE id=?", (uc_id,))
        conn.commit()


def test_edit_requires_reimburse_month_when_money_was_deducted(client, conn):
    emp = _emp(conn)
    uc_id = _mk_uc(conn, emp, status='recovered', payments_made=1)
    try:
        r = client.post(f'/undercharge/{uc_id}/edit', data={
            'type': 'undercharge', 'reason': 'test', 'total_amount': '100',
            'incident_month': '5', 'incident_year': '2026',
            'start_month': '5', 'start_year': '2026',
            'recovery_method': 'full', 'payments_made': '1',
            'status': 'paid_by_customer',  # no reimburse fields
        })
        row = _get(conn, uc_id)
        assert row['status'] == 'recovered'  # edit rejected, nothing changed
    finally:
        conn.execute("DELETE FROM undercharges_cents WHERE id=?", (uc_id,))
        conn.commit()


def test_edit_explicit_no_refund_allowed_when_money_deducted(client, conn):
    """With an explicit 'No refund' choice the edit IS accepted even though money
    was deducted — the deducted amount stays, no reimbursement is scheduled."""
    emp = _emp(conn)
    uc_id = _mk_uc(conn, emp, status='recovered', payments_made=1)
    try:
        client.post(f'/undercharge/{uc_id}/edit', data={
            'type': 'undercharge', 'reason': 'test', 'total_amount': '100',
            'incident_month': '5', 'incident_year': '2026',
            'start_month': '5', 'start_year': '2026',
            'recovery_method': 'full', 'payments_made': '1',
            'status': 'paid_by_customer', 'refund': 'no',
        })
        row = _get(conn, uc_id)
        assert row['status'] == 'paid_by_customer'
        assert row['reimburse_month'] is None and row['reimburse_year'] is None
    finally:
        conn.execute("DELETE FROM undercharges_cents WHERE id=?", (uc_id,))
        conn.commit()


def test_edit_refund_yes_requires_month(client, conn):
    """Choosing 'Yes, refund' without a month is rejected — nothing changes."""
    emp = _emp(conn)
    uc_id = _mk_uc(conn, emp, status='recovered', payments_made=1)
    try:
        client.post(f'/undercharge/{uc_id}/edit', data={
            'type': 'undercharge', 'reason': 'test', 'total_amount': '100',
            'incident_month': '5', 'incident_year': '2026',
            'start_month': '5', 'start_year': '2026',
            'recovery_method': 'full', 'payments_made': '1',
            'status': 'paid_by_customer', 'refund': 'yes',  # no month
        })
        assert _get(conn, uc_id)['status'] == 'recovered'  # rejected, unchanged
    finally:
        conn.execute("DELETE FROM undercharges_cents WHERE id=?", (uc_id,))
        conn.commit()
