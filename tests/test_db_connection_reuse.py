"""One SQLite connection per request (database.get_db).

get_db() caches a connection on Flask's ``g`` so a page that calls it eight
times pays for one connection and one set of PRAGMAs, not eight. The tests here
guard the two ways that sharing could break production:

  * a caller doing ``conn.close()`` in a finally must not close the connection
    out from under a later caller in the same request;
  * a caller that is mid-write must not have its half-finished transaction
    committed (or rolled back) by a nested caller.

Off-request behaviour (scripts/, workers/, the MCP server) must be unchanged:
a private connection the caller owns and closes.
"""
import sqlite3

import pytest

from northwind.data import database as db


def _conn_count(monkeypatch):
    """Count real sqlite3.connect() calls, not get_db() calls."""
    calls = []
    real = sqlite3.connect

    def counting(*a, **kw):
        calls.append(a[0] if a else kw.get('database'))
        return real(*a, **kw)

    monkeypatch.setattr(sqlite3, 'connect', counting)
    return calls


def test_off_request_get_db_is_private_and_really_closes(db_copy):
    """Scripts and the MCP server have no app context — they must still own the
    connection outright, including a close() that actually closes."""
    conn = db.get_db()
    assert isinstance(conn, sqlite3.Connection)
    conn.close()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')


def test_two_get_db_calls_in_one_request_share_one_connection(db_copy, monkeypatch):
    import app as a
    calls = _conn_count(monkeypatch)
    with a.app.test_request_context('/'):
        first = db.get_db()
        second = db.get_db()
        assert len(calls) == 1
        # Same underlying connection behind two handles.
        assert first._conn is second._conn


def test_close_by_first_caller_does_not_break_a_later_caller(db_copy):
    """The outage this sharing could cause: `conn = db.get_db()` … `conn.close()`
    in a finally is the dominant call-site shape, and there are several per
    request. If the first finally closed the shared connection, every later
    query in that request would raise on a closed database."""
    import app as a
    with a.app.test_request_context('/'):
        first = db.get_db()
        assert first.execute('SELECT COUNT(*) FROM employees').fetchone()[0] >= 0
        first.close()          # exactly what a route's finally does

        later = db.get_db()
        assert later.execute('SELECT COUNT(*) FROM employees').fetchone()[0] >= 0
        # ...and the connection the first caller "closed" still works, because
        # helpers hold references across their own close().
        assert first.execute('SELECT 1').fetchone()[0] == 1


def test_with_conn_block_still_commits_and_leaves_it_usable(db_copy):
    """`with conn:` is used across the write routes. It must keep committing on
    success without closing the shared connection."""
    import app as a
    with a.app.test_request_context('/'):
        conn = db.get_db()
        conn.execute("CREATE TEMP TABLE t_reuse (v INTEGER)")
        with conn as c:
            c.execute("INSERT INTO t_reuse VALUES (7)")
        assert not conn.in_transaction
        assert conn.execute("SELECT v FROM t_reuse").fetchone()[0] == 7
        assert db.get_db().execute('SELECT 1').fetchone()[0] == 1


def test_nested_caller_during_a_write_gets_its_own_transaction(db_copy, monkeypatch):
    """A caller mid-write must not have its uncommitted work committed by an
    inner caller. While the shared connection is in a transaction, get_db()
    hands out a private connection — the pre-sharing behaviour."""
    import app as a
    with a.app.test_request_context('/'):
        outer = db.get_db()
        outer.execute("CREATE TABLE IF NOT EXISTS t_nested (v INTEGER)")
        outer.commit()
        outer.execute("DELETE FROM t_nested")
        outer.commit()

        calls = _conn_count(monkeypatch)
        outer.execute("INSERT INTO t_nested VALUES (1)")   # uncommitted
        assert outer.in_transaction

        inner = db.get_db()
        assert len(calls) == 1, 'nested caller during a write must not share'
        assert isinstance(inner, sqlite3.Connection)
        # The inner connection cannot see (and so cannot commit) the outer's
        # uncommitted row.
        assert inner.execute("SELECT COUNT(*) FROM t_nested").fetchone()[0] == 0
        inner.close()

        outer.rollback()
        assert outer.execute("SELECT COUNT(*) FROM t_nested").fetchone()[0] == 0
        outer.execute("DROP TABLE t_nested")
        outer.commit()


def test_teardown_closes_the_shared_connection(db_copy):
    import app as a
    holder = {}
    with a.app.test_request_context('/'):
        handle = db.get_db()
        holder['raw'] = handle._conn
    # Context popped -> teardown_appcontext -> close_request_conns.
    with pytest.raises(sqlite3.ProgrammingError):
        holder['raw'].execute('SELECT 1')


def test_a_page_render_opens_exactly_one_connection(client, monkeypatch):
    """The measurable win: /admin used to open 8 connections (5 PRAGMAs each)."""
    client.get('/admin')                       # warm template/stores caches
    calls = _conn_count(monkeypatch)
    resp = client.get('/admin')
    assert resp.status_code == 200
    assert len(calls) == 1, f'expected 1 connection for /admin, got {len(calls)}'


def test_restore_backup_still_works_with_the_request_connection_open(
        client, db_copy, tmp_path, monkeypatch):
    """The one route that rewrites the DB file underneath itself.

    /admin/restore-db copies an uploaded database over DB_PATH with SQLite's
    online backup API and then runs migrations. The auth gate has already opened
    the request's shared connection to that same file by the time it runs, which
    it never used to (every earlier caller closed its own). Guard that the
    backup + migration still succeed with that connection open.
    """
    from northwind.deductions import routes_payroll as rp
    monkeypatch.setattr(rp, 'BACKUP_DIR', str(tmp_path / 'backups'))

    upload = tmp_path / 'snapshot.db'
    src = sqlite3.connect(db_copy)
    dst = sqlite3.connect(str(upload))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()

    before = db.get_db()
    try:
        expected = before.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    finally:
        before.close()

    with open(upload, 'rb') as fh:
        resp = client.post('/admin/restore-db',
                           data={'confirm': 'yes', 'backup_file': (fh, 'snapshot.db')},
                           content_type='multipart/form-data')
    assert resp.status_code in (302, 303)
    # Proof it got past validation and actually ran, rather than flashing an
    # error and redirecting (both look like a 302).
    snapshots = list((tmp_path / 'backups').glob('manual_pre_restore_*.db'))
    assert snapshots, 'restore bailed out before snapshotting the live DB'

    after = db.get_db()
    try:
        assert after.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == expected
    finally:
        after.close()


def test_write_route_still_persists_through_the_shared_connection(client):
    """End-to-end guard that commit semantics survived: a real write route's
    change must be visible on a fresh connection afterwards."""
    conn = db.get_db()
    try:
        row = conn.execute(
            "SELECT id, notes FROM employees WHERE status='active' LIMIT 1").fetchone()
    finally:
        conn.close()
    emp_id, original = row['id'], row['notes']
    try:
        resp = client.post(f'/api/employees/{emp_id}/notes',
                           data={'notes': 'tier3 connection reuse probe'})
        assert resp.status_code == 200
        probe = db.get_db()
        try:
            assert probe.execute(
                "SELECT notes FROM employees WHERE id=?", (emp_id,)
            ).fetchone()['notes'] == 'tier3 connection reuse probe'
        finally:
            probe.close()
    finally:
        restore = db.get_db()
        try:
            restore.execute("UPDATE employees SET notes=? WHERE id=?", (original, emp_id))
            restore.commit()
        finally:
            restore.close()
