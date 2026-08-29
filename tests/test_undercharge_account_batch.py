"""get_undercharge_accounts() must equal get_undercharge_account() exactly.

The batched read exists only to stop /admin issuing 815 statements; it computes
money (deducted, refunded, remaining, refund_due and the derived status), so
"faster" is worthless unless it is byte-for-byte the same answer. These tests
compare the two paths over EVERY undercharge in the dev database, and again
after each of the settlement shapes that make the maths interesting.
"""
import pytest

from northwind.data import database as db


def _all_ids(conn):
    return [r['id'] for r in conn.execute("SELECT id FROM undercharges_cents ORDER BY id")]


def test_batched_matches_per_row_for_every_undercharge_in_the_db(conn):
    ids = _all_ids(conn)
    assert ids, 'dev DB has no undercharges to compare'
    batched = db.get_undercharge_accounts(ids, conn)
    for uc_id in ids:
        expected = db.get_undercharge_account(uc_id, conn)
        assert batched[uc_id] == expected, f'undercharge {uc_id} diverged'


def test_batched_covers_overcharges_too(conn):
    """Overcharges take the `plan['type'] == 'overcharge'` status branch and have
    no schedule — the batch must still return their row unchanged."""
    overs = [r['id'] for r in conn.execute(
        "SELECT id FROM undercharges_cents WHERE type='overcharge' ORDER BY id")]
    if not overs:
        pytest.skip('no overcharges in the dev DB')
    batched = db.get_undercharge_accounts(overs, conn)
    for uc_id in overs:
        assert batched[uc_id] == db.get_undercharge_account(uc_id, conn)


def test_unknown_and_empty_ids(conn):
    assert db.get_undercharge_accounts([], conn) == {}
    missing = conn.execute("SELECT COALESCE(MAX(id),0)+9999 FROM undercharges_cents").fetchone()[0]
    assert db.get_undercharge_account(missing, conn) is None
    assert missing not in db.get_undercharge_accounts([missing], conn)


def test_duplicate_ids_are_deduped_not_double_counted(conn):
    uc_id = _all_ids(conn)[0]
    batched = db.get_undercharge_accounts([uc_id, uc_id, uc_id], conn)
    assert list(batched) == [uc_id]
    assert batched[uc_id] == db.get_undercharge_account(uc_id, conn)


def _fresh_undercharge(conn, total_cents, months=3):
    emp = conn.execute(
        "SELECT id FROM employees WHERE status='active' LIMIT 1").fetchone()['id']
    cur = conn.execute(
        "INSERT INTO undercharges_cents (employee_id,total_amount_cents,reason,"
        "incident_month,incident_year,start_month,start_year,recovery_method,"
        "split_months,payments_made,status,type) "
        "VALUES (?,?,'batch equivalence probe',1,2026,1,2026,'split',?,0,'pending','undercharge')",
        (emp, total_cents, months))
    uc_id = cur.lastrowid
    db.create_undercharge_schedule(conn, uc_id, 2026, 1, months, total_cents,
                                   reason='probe', actor='pytest')
    return emp, uc_id


@pytest.mark.parametrize('shape', [
    'untouched',
    'part_deducted',
    'customer_paid_in_full_after_deductions',
    'written_off',
    'liability_adjusted',
    'external_refund',
    'refund_waived',
    'voided_transaction',
])
def test_batched_matches_per_row_across_settlement_shapes(conn, shape):
    """Each branch of the derived status / refund maths, checked both ways.

    Everything happens inside one transaction that is rolled back, so the shared
    test database is untouched.
    """
    total = 60000
    emp, uc_id = _fresh_undercharge(conn, total)
    try:
        if shape == 'part_deducted':
            conn.execute(
                "INSERT INTO deduction_transactions_cents "
                "(plan_type,plan_id,employee_id,amount_cents,year,month) "
                "VALUES ('undercharge',?,?,20000,2026,1)", (uc_id, emp))
        elif shape == 'customer_paid_in_full_after_deductions':
            conn.execute(
                "INSERT INTO deduction_transactions_cents "
                "(plan_type,plan_id,employee_id,amount_cents,year,month) "
                "VALUES ('undercharge',?,?,20000,2026,1)", (uc_id, emp))
            db.record_undercharge_event(conn, uc_id, 'customer_payment', total,
                                        actor='pytest')
        elif shape == 'written_off':
            db.record_undercharge_event(conn, uc_id, 'write_off', total, actor='pytest')
        elif shape == 'liability_adjusted':
            db.record_undercharge_event(conn, uc_id, 'liability_adjustment', -15000,
                                        actor='pytest')
        elif shape == 'external_refund':
            conn.execute(
                "INSERT INTO deduction_transactions_cents "
                "(plan_type,plan_id,employee_id,amount_cents,year,month) "
                "VALUES ('undercharge',?,?,?,2026,1)", (uc_id, emp, total))
            db.record_undercharge_event(conn, uc_id, 'customer_payment', total,
                                        actor='pytest')
            db.record_undercharge_event(conn, uc_id, 'external_refund', 25000,
                                        actor='pytest')
        elif shape == 'refund_waived':
            conn.execute(
                "INSERT INTO deduction_transactions_cents "
                "(plan_type,plan_id,employee_id,amount_cents,year,month) "
                "VALUES ('undercharge',?,?,?,2026,1)", (uc_id, emp, total))
            db.record_undercharge_event(conn, uc_id, 'customer_payment', total,
                                        actor='pytest')
            db.record_undercharge_event(conn, uc_id, 'refund_waiver', total,
                                        actor='pytest')
        elif shape == 'voided_transaction':
            conn.execute(
                "INSERT INTO deduction_transactions_cents "
                "(plan_type,plan_id,employee_id,amount_cents,year,month,voided) "
                "VALUES ('undercharge',?,?,20000,2026,1,1)", (uc_id, emp))

        per_row = db.get_undercharge_account(uc_id, conn)
        batched = db.get_undercharge_accounts([uc_id], conn)[uc_id]
        assert batched == per_row, shape
        # Sanity: the probe actually produced a money state, not all zeros.
        assert per_row['original_total_cents'] == total
    finally:
        conn.rollback()


def test_batched_read_writes_nothing_when_schedules_already_exist(conn):
    """ensure_undercharge_schedule is a lazy migration guard that WRITES on a
    read path. Batched, it must reduce to a single existence probe: migration
    0036 backfilled production, so a normal dashboard read must not open a write
    transaction (which, on the request's shared connection, would hold SQLite's
    write lock for the rest of the request)."""
    ids = _all_ids(conn)
    conn.rollback()
    assert not conn.in_transaction
    db.get_undercharge_accounts(ids, conn)
    assert not conn.in_transaction, 'batched read opened a write transaction'


def test_batch_chunking_is_exercised(conn, monkeypatch):
    """Guard the IN(...) chunking (SQLite's 999-variable limit) by forcing a
    tiny batch size over the real data."""
    ids = _all_ids(conn)
    reference = db.get_undercharge_accounts(ids, conn)
    monkeypatch.setattr(db, '_UC_BATCH', 3)
    assert db.get_undercharge_accounts(ids, conn) == reference


def test_dashboard_undercharge_total_matches_the_per_row_sum(conn):
    """The number a human checks: the dashboard's outstanding-undercharge total
    must equal the sum of the per-row remaining balances."""
    rows = conn.execute(
        "SELECT id, employee_id FROM undercharges "
        "WHERE (type IS NULL OR type='undercharge')").fetchall()
    expected = sum(db.get_undercharge_account(r['id'], conn)['remaining_cents']
                   for r in rows)
    totals = db._outstanding_by_employee(conn)
    got = round(sum(v['undercharges'] for v in totals.values()), 2)
    assert got == round(expected / 100.0, 2)
