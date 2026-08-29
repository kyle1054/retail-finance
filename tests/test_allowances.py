"""Staff allowances (HQ/DC): allocation, purchases, remaining math, routes."""


def _hq_employee(conn):
    return conn.execute(
        "SELECT id FROM employees WHERE sector='hq' AND status='active' LIMIT 1"
    ).fetchone()['id']


def test_allowance_math_and_overspend(conn):
    from northwind.data import database as db
    emp_id = _hq_employee(conn)

    db.set_allowance(emp_id, 2099, 5000)
    s = db.get_allowance_summary(emp_id, 2099)
    assert s == {'allocated': 5000.0, 'spent': 0.0, 'remaining': 5000.0}

    db.add_allowance_purchases(emp_id, 2099, '2099-01-13', [
        {'sku': '660200074424', 'desc': 'Yoga Mat', 'price': 695, 'qty': 1},
        {'sku': '', 'desc': 'Protein Shakes', 'price': 1895, 'qty': 2},
    ], location='HQ', sale_number='INV-1')
    s = db.get_allowance_summary(emp_id, 2099)
    assert s['spent'] == 4485.0          # 695 + 1895*2
    assert s['remaining'] == 515.0

    # Overspend is allowed: remaining goes negative, nothing blocks.
    db.add_allowance_purchases(emp_id, 2099, '2099-02-04', [
        {'sku': '', 'desc': 'NORTHWIND 100 Off-White', 'price': 2495, 'qty': 1},
    ])
    s = db.get_allowance_summary(emp_id, 2099)
    assert s['remaining'] == -1980.0

    # Years are independent: next year starts clean.
    assert db.get_allowance_summary(emp_id, 2100) is None

    # Deleting a line restores the budget.
    purchases = db.get_allowance_purchases(emp_id, 2099)
    assert len(purchases) == 3
    db.delete_allowance_purchase(purchases[-1]['id'])
    assert db.get_allowance_summary(emp_id, 2099)['remaining'] == 515.0


def test_allowance_overview_includes_unallocated(conn):
    from northwind.data import database as db
    emp_id = _hq_employee(conn)
    db.set_allowance(emp_id, 2098, 10000)
    rows = db.get_allowances_overview(2098, 'hq')
    by_id = {r['id']: r for r in rows}
    assert by_id[emp_id]['allocated'] == 10000.0
    # Employees with no allocation still appear (allocated 0), never drop off.
    hq_count = conn.execute(
        "SELECT COUNT(*) FROM employees WHERE sector='hq' AND status='active'"
    ).fetchone()[0]
    assert len(rows) == hq_count


def test_allowance_routes(client, conn):
    emp_id = _hq_employee(conn)

    r = client.get('/hq/allowances')
    assert r.status_code == 200

    r = client.post('/hq/allowances/set', data={
        'employee_id': emp_id, 'year': '2097', 'allocated': '5000'})
    assert r.status_code == 302

    r = client.post('/hq/allowances/add-purchase', data={
        'employee_id': emp_id, 'purchase_date': '2097-03-01',
        'location': 'DC', 'sale_number': 'INV-77',
        'item_sku_0': '660200000001', 'item_desc_0': 'Cap',
        'item_price_0': '495', 'item_qty_0': '1',
        'item_sku_1': '', 'item_desc_1': 'Tee',
        'item_price_1': '350', 'item_qty_1': '2',
    })
    assert r.status_code == 302

    from northwind.data import database as db
    s = db.get_allowance_summary(emp_id, 2097)
    assert s['spent'] == 1195.0
    assert s['remaining'] == 3805.0

    # Profile page renders the allowance card with the right remaining figure.
    r = client.get(f'/hq/employees/{emp_id}?allowance_year=2097')
    assert r.status_code == 200
    assert b'Staff Allowance' in r.data
    assert b'3805.00' in r.data

    # Overview shows the employee for that year.
    r = client.get('/hq/allowances?year=2097')
    assert r.status_code == 200
    assert b'1195.00' in r.data


def test_allowance_resave_keeps_notes(conn):
    """Editing only the amount must not wipe an existing allocation note."""
    from northwind.data import database as db
    emp_id = _hq_employee(conn)
    db.set_allowance(emp_id, 2094, 5115, 'Includes +115 credit')
    db.set_allowance(emp_id, 2094, 6000)  # amount-only re-save (no note sent)
    note = conn.execute(
        "SELECT notes FROM allowances WHERE employee_id=? AND year=2094",
        (emp_id,)).fetchone()['notes']
    assert note == 'Includes +115 credit'
    assert db.get_allowance_summary(emp_id, 2094)['allocated'] == 6000.0


def test_allowance_location_split(client, conn):
    """The overview can be filtered to one HQ location (HQ vs DC)."""
    hq_emp = conn.execute(
        "SELECT full_name FROM employees WHERE sector='hq' AND status='active' "
        "AND current_store='HQ' LIMIT 1").fetchone()
    dc_emp = conn.execute(
        "SELECT full_name FROM employees WHERE sector='hq' AND status='active' "
        "AND current_store='DC' LIMIT 1").fetchone()

    r = client.get('/hq/allowances?location=HQ')
    assert r.status_code == 200
    if hq_emp:
        assert hq_emp['full_name'].encode() in r.data
    if dc_emp:
        assert dc_emp['full_name'].encode() not in r.data

    if dc_emp:
        r = client.get('/hq/allowances?location=DC')
        assert dc_emp['full_name'].encode() in r.data

    # Unknown locations fall back to the unfiltered view, not an error.
    r = client.get('/hq/allowances?location=Narnia')
    assert r.status_code == 200


def test_allowance_add_purchase_rejects_empty(client, conn):
    emp_id = _hq_employee(conn)
    r = client.post('/hq/allowances/add-purchase', data={
        'employee_id': emp_id, 'purchase_date': '2096-01-01'})
    assert r.status_code == 302
    from northwind.data import database as db
    assert db.get_allowance_summary(emp_id, 2096) is None
