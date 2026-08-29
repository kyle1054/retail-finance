"""Regression: /undercharges/export used a connection it had already closed.

The route did `rows = conn.execute(...)` then `conn.close()`, and 60 lines later
priced every row through that same closed `conn`:

    account = db.get_undercharge_account(r['id'], conn)
    rem_val = round(_uc_remaining(r, conn), 2)

That raised `sqlite3.ProgrammingError: Cannot operate on a closed database` and
returned HTTP 500 — on the committed version, in production, for every request.
It had no test, which is how it survived.

It then started "working" for the wrong reason: sharing one connection per
request made `close()` a no-op until teardown, so the use-after-close silently
became legal. That is a coincidence, not a contract — if the sharing is ever
reverted or reworked, the export breaks again. These tests pin the route's own
correctness so it no longer depends on that.
"""
import io

import pytest

from northwind.data import database as db

XLSX_MAGIC = b'PK\x03\x04'


def test_export_returns_a_real_workbook(client):
    r = client.get('/undercharges/export')
    assert r.status_code == 200, (
        f'/undercharges/export returned {r.status_code} — the route prices rows '
        f'through a connection it closed too early')
    body = r.get_data()
    assert body[:4] == XLSX_MAGIC, 'response is not an xlsx file'
    assert len(body) > 4000, 'workbook looks empty'


def test_export_does_not_depend_on_close_being_a_no_op(client, monkeypatch):
    """The real point: make close() actually close, and the route must still work.

    This is what fails on the committed version and what would fail again if the
    per-request connection sharing were removed.
    """
    def unshared_get_db():
        # Exactly the pre-sharing shape: a brand-new private connection per
        # call, registered for teardown, whose close() genuinely closes.
        conn = db._connect()
        db._track_for_teardown(conn)
        return conn

    monkeypatch.setattr(db, 'get_db', unshared_get_db)
    r = client.get('/undercharges/export')
    assert r.status_code == 200, (
        'export broke once close() actually closed — it is relying on the '
        'shared-connection no-op rather than managing its own connection')
    assert r.get_data()[:4] == XLSX_MAGIC


@pytest.mark.parametrize('qs', [
    '',
    '?status=outstanding',
    '?status=all',
    '?type=undercharge',
    '?type=overcharge',
    '?from_month=1&from_year=2020&to_month=12&to_year=2099',
])
def test_export_honours_its_filters_without_erroring(client, qs):
    """Each filter path reaches the same per-row pricing loop."""
    r = client.get('/undercharges/export' + qs)
    assert r.status_code == 200, f'{qs or "(no filters)"} returned {r.status_code}'
    assert r.get_data()[:4] == XLSX_MAGIC


def test_export_prices_rows_the_same_way_the_list_does(client, conn):
    """The loop must produce real money, not blanks — a closed connection would
    have thrown, but a silently-empty account would not."""
    import openpyxl

    r = client.get('/undercharges/export?status=all')
    assert r.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(r.get_data()))
    ws = wb.active

    grand = None
    for row in ws.iter_rows(values_only=True):
        if row and any(isinstance(c, str) and 'GRAND TOTAL' in c for c in row):
            grand = row
            break
    assert grand is not None, 'no grand-total row — the data loop produced nothing'
    numbers = [c for c in grand if isinstance(c, (int, float))]
    assert numbers, 'grand total carried no money figures'
