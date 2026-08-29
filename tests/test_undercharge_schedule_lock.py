"""Regression: the lazy schedule repair must not deadlock a request, and must
not commit a write route's half-finished transaction.

The bug: `ensure_undercharge_schedule` is a WRITE reached from read paths (the
dashboard prices every undercharge). It wrote on a connection it did not own and
never committed. That was survivable while each caller had a private connection —
the real `close()` rolled the write back and released the lock immediately.

Once a request shares ONE connection whose `close()` is a no-op until teardown,
the uncommitted INSERT holds SQLite's RESERVED lock for the rest of the request.
The next `get_db()` sees `in_transaction` and correctly hands out a private
connection, which then blocks on the lock its own request is holding, waits out
`busy_timeout` and fails:

    GET /admin -> 500 in 5351 ms   sqlite3.OperationalError: database is locked

And because the repair was rolled back rather than committed, the row stayed
unrepaired — so /admin was permanently down, not intermittently.

Trigger: any `undercharges_cents` row with no `undercharge_schedule_revisions`
row. In-app creation always builds a schedule, so this needs a row from an
import, a fixture, an old operational script, or a restored older backup — which
is exactly the case the guard exists to handle.
"""
import time

import pytest

from northwind.data import database as db


@pytest.fixture
def unscheduled_undercharge(conn):
    """An undercharge stripped of its schedule — the state the guard repairs."""
    row = conn.execute(
        "SELECT id FROM undercharges_cents WHERE type='undercharge' "
        "AND COALESCE(start_year, incident_year) IS NOT NULL LIMIT 1").fetchone()
    assert row, 'dev DB has no undercharge to strip'
    uid = row['id']
    conn.execute('DELETE FROM undercharge_schedule_items WHERE undercharge_id=?', (uid,))
    conn.execute('DELETE FROM undercharge_schedule_revisions WHERE undercharge_id=?', (uid,))
    conn.commit()
    return uid


def _revisions(uid):
    c = db.get_db()
    try:
        return c.execute(
            'SELECT COUNT(*) FROM undercharge_schedule_revisions WHERE undercharge_id=?',
            (uid,)).fetchone()[0]
    finally:
        c.close()


def test_dashboard_survives_an_unscheduled_undercharge(client, unscheduled_undercharge):
    """The original 500. Timed, because the failure mode is a busy_timeout stall."""
    started = time.perf_counter()
    r = client.get('/admin')
    elapsed = time.perf_counter() - started
    assert r.status_code == 200, (
        f'/admin returned {r.status_code} with an unscheduled undercharge — '
        f'the lazy repair is holding the write lock against its own request')
    assert elapsed < 2.0, (
        f'/admin took {elapsed:.1f}s — that is a lock wait, not work '
        f'(busy_timeout is 5s)')


def test_repair_persists_so_it_does_not_recur(client, unscheduled_undercharge):
    """Rolled back at teardown, the repair re-ran — and re-deadlocked — forever."""
    assert _revisions(unscheduled_undercharge) == 0
    assert client.get('/admin').status_code == 200
    assert _revisions(unscheduled_undercharge) == 1, (
        'repair did not persist; the next request will redo it')
    assert client.get('/admin').status_code == 200      # second pass, now a no-op


def test_repair_leaves_the_connection_clean(unscheduled_undercharge):
    """An open transaction here is what forces later callers onto private
    connections and into the lock wait, so assert both consequences."""
    import app as a
    with a.app.test_request_context('/'):
        conn = db.get_db()
        assert conn.in_transaction is False
        db.get_undercharge_account(unscheduled_undercharge, conn)
        assert conn.in_transaction is False, (
            'guard left a write transaction open on a shared connection')
        # Still sharing: a dirty connection would have pushed this onto a
        # private one, which is the connection that then blocks on the lock.
        assert hasattr(db.get_db(), '_conn'), (
            'next caller was forced onto a private connection')


def test_does_not_commit_a_callers_open_transaction(conn, unscheduled_undercharge):
    """The money-safety half: a write route mid-change must keep ownership.

    If the guard committed unconditionally, a pricing call nested inside a
    payroll tick or an undercharge reschedule would make that route's
    half-finished change durable — and a later rollback could not take it back.
    """
    original = conn.execute(
        'SELECT reason FROM undercharges_cents WHERE id=?',
        (unscheduled_undercharge,)).fetchone()['reason']

    conn.execute("UPDATE undercharges_cents SET reason='PARTIAL-UNCOMMITTED' WHERE id=?",
                 (unscheduled_undercharge,))
    assert conn.in_transaction is True

    db.ensure_undercharge_schedule(conn, unscheduled_undercharge)
    assert conn.in_transaction is True, (
        "guard committed the caller's transaction out from under them")

    conn.rollback()

    assert _revisions(unscheduled_undercharge) == 0, (
        'repair survived the caller rolling back — it was committed separately')
    after = conn.execute('SELECT reason FROM undercharges_cents WHERE id=?',
                         (unscheduled_undercharge,)).fetchone()['reason']
    assert after == original, "caller's abandoned write was made durable"


def test_batched_read_of_many_unscheduled_rows_is_safe(client, conn):
    """The dashboard path repairs a whole batch, not one row."""
    ids = [r['id'] for r in conn.execute(
        "SELECT id FROM undercharges_cents WHERE type='undercharge' "
        "AND COALESCE(start_year, incident_year) IS NOT NULL LIMIT 5")]
    assert ids, 'dev DB has no undercharges to strip'
    for uid in ids:
        conn.execute('DELETE FROM undercharge_schedule_items WHERE undercharge_id=?', (uid,))
        conn.execute('DELETE FROM undercharge_schedule_revisions WHERE undercharge_id=?', (uid,))
    conn.commit()

    started = time.perf_counter()
    assert client.get('/admin').status_code == 200
    assert time.perf_counter() - started < 2.0
    for uid in ids:
        assert _revisions(uid) == 1, f'undercharge {uid} not repaired'
