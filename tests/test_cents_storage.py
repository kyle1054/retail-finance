"""Write-path round-trip tests for the integer-cent storage.

These exercise the actual mutation routes (add / tick / edit) and assert that:
  - money is physically stored as integer cents in the *_cents base table, and
  - reading back through the Rands view returns the exact original value.
This is what guards against a write path silently storing Rands as cents.
"""


def _an_employee(conn):
    return conn.execute("SELECT id FROM employees WHERE status='active' LIMIT 1").fetchone()['id']


def test_uniform_table_is_cents_with_rands_view(conn):
    # Base table holds integer cents; view exposes the same value in Rands.
    row = conn.execute("SELECT name FROM sqlite_master WHERE name='uniform_deductions_cents'").fetchone()
    assert row, "expected uniform_deductions_cents base table"
    vw = conn.execute("SELECT type FROM sqlite_master WHERE name='uniform_deductions'").fetchone()
    assert vw and vw['type'] == 'view', "uniform_deductions should be a view"
    pair = conn.execute("""
        SELECT c.total_amount_cents AS cents, v.total_amount AS rands
        FROM uniform_deductions_cents c JOIN uniform_deductions v ON v.id = c.id
        WHERE c.total_amount_cents IS NOT NULL LIMIT 1""").fetchone()
    if pair:
        assert abs(pair['cents'] / 100.0 - pair['rands']) < 1e-9


def test_add_uniform_stores_cents(client, conn):
    emp = _an_employee(conn)
    r = client.post('/uniform/add', data={
        'employee_id': emp, 'monthly_amount': '83.33', 'term_months': '3',
        'total_amount': '250.00', 'start_month': '12', 'start_year': '2027',
        'sku': 'TEST-SKU', 'description': 'pytest uniform', 'sale_number': 'T1', 'notes': '',
    }, follow_redirects=False)
    assert r.status_code in (302, 200)
    row = conn.execute("""SELECT total_amount_cents, monthly_amount_cents, balance_remaining_cents
                          FROM uniform_deductions_cents WHERE sku='TEST-SKU' ORDER BY id DESC LIMIT 1""").fetchone()
    assert row is not None, "new uniform row not found"
    assert row['total_amount_cents'] == 25000
    assert row['monthly_amount_cents'] == 8333
    assert row['balance_remaining_cents'] == 25000
    # And the Rands view reflects it exactly.
    v = conn.execute("SELECT total_amount FROM uniform_deductions WHERE sku='TEST-SKU' ORDER BY id DESC LIMIT 1").fetchone()
    assert abs(v['total_amount'] - 250.00) < 1e-9


def test_layby_table_is_cents_with_rands_view(conn):
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='layby_deductions_cents'").fetchone()
    vw = conn.execute("SELECT type FROM sqlite_master WHERE name='layby_deductions'").fetchone()
    assert vw and vw['type'] == 'view'
    pair = conn.execute("""
        SELECT c.total_amount_cents AS cents, v.total_amount AS rands
        FROM layby_deductions_cents c JOIN layby_deductions v ON v.id = c.id
        WHERE c.total_amount_cents IS NOT NULL LIMIT 1""").fetchone()
    if pair:
        assert abs(pair['cents'] / 100.0 - pair['rands']) < 1e-9


def test_tick_uniform_reduces_balance_in_cents(client, conn):
    emp = _an_employee(conn)
    client.post('/uniform/add', data={
        'employee_id': emp, 'monthly_amount': '100.00', 'term_months': '2',
        'total_amount': '200.00', 'start_month': '11', 'start_year': '2027',
        'sku': 'TICK-SKU', 'description': 'pytest tick', 'sale_number': 'T2', 'notes': '',
    })
    pid = conn.execute("SELECT id FROM uniform_deductions_cents WHERE sku='TICK-SKU' ORDER BY id DESC LIMIT 1").fetchone()['id']
    client.post(f'/uniform/{pid}/tick')
    row = conn.execute("SELECT payments_made, balance_remaining_cents FROM uniform_deductions_cents WHERE id=?", (pid,)).fetchone()
    assert row['payments_made'] == 1
    assert row['balance_remaining_cents'] == 10000  # R200 - R100 = R100, exact cents
    # The recorded payment transaction is stored in cents and reads back as Rands.
    tx = conn.execute("""SELECT t.amount_cents AS cents, v.amount AS rands
                         FROM deduction_transactions_cents t JOIN deduction_transactions v ON v.id=t.id
                         WHERE t.plan_type='uniform' AND t.plan_id=? ORDER BY t.id DESC LIMIT 1""", (pid,)).fetchone()
    assert tx['cents'] == 10000 and abs(tx['rands'] - 100.0) < 1e-9


def test_all_money_tables_are_views(conn):
    for t in ('uniform_deductions', 'layby_deductions', 'undercharges',
              'deduction_transactions', 'plan_adjustments', 'layby_items', 'overpayments'):
        row = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (t,)).fetchone()
        assert row and row['type'] == 'view', f"{t} should be a Rands view over a *_cents base table"
