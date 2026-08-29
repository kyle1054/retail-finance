"""Route-level tests for the store-expenses MJ preview + CSV export.

The shared DB copy is session-scoped, so each test seeds into its own month
(2099-01, -02, …) to stay isolated from the others' rows.
"""
import csv
import io

from northwind.cash import mj as cash_mj


def _seed_expenses(conn, month):
    """A store with two expense entries (one standard, one no-VAT) + a Banked
    transfer that must NOT appear on the MJ. Returns (store, year, month)."""
    store = conn.execute("SELECT name FROM stores LIMIT 1").fetchone()['name']
    conn.execute("UPDATE stores SET xero_tracking_name='Test Store - XX' WHERE name=?", (store,))
    printing = conn.execute(
        "SELECT id FROM recon_categories WHERE lower(name) LIKE '%printing%' LIMIT 1").fetchone()['id']
    conn.execute("UPDATE recon_categories SET xero_code='6240', vat_type='standard' WHERE id=?", (printing,))
    milk = conn.execute(
        "SELECT id FROM recon_categories WHERE lower(name) LIKE 'milk%' LIMIT 1").fetchone()['id']
    conn.execute("UPDATE recon_categories SET xero_code='6330', vat_type='novat' WHERE id=?", (milk,))
    banked = conn.execute("SELECT id FROM recon_categories WHERE kind='transfer' LIMIT 1").fetchone()['id']

    day = f"2099-{month:02d}-10"

    def add(cat, direction, cents, note):
        conn.execute(
            "INSERT INTO cash_recon_entries (store, entry_date, category_id, description, "
            "direction, amount_cents, note, created_by) "
            "SELECT ?, ?, ?, name, ?, ?, ?, 'pytest' FROM recon_categories WHERE id=?",
            (store, day, cat, direction, cents, note, cat))
    add(printing, 'out', 8290, 'waybills')   # 82.90 gross standard
    add(milk, 'out', 9790, 'staff milk')     # 97.90 gross no-VAT
    add(banked, 'out', 500000, 'to THE BANK')     # must be excluded
    conn.commit()
    return store, 2099, month


def _expense_rows(conn, store, month):
    ym = f"2099-{month:02d}"
    return conn.execute(
        "SELECT e.id, e.note FROM cash_recon_entries e JOIN recon_categories c ON c.id=e.category_id "
        "WHERE e.store=? AND substr(e.entry_date,1,7)=? AND c.kind='expense' ORDER BY e.id",
        (store, ym)).fetchall()


def test_mj_preview_lists_only_expenses(client, conn):
    store, y, m = _seed_expenses(conn, 1)
    r = client.get(f'/cash/{store}/{y}/{m}/mj')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'waybills' in body and 'staff milk' in body
    assert 'to THE BANK' not in body            # Banked transfer excluded
    assert 'Test Store - XX' in body       # tracking name shown


def test_mj_export_csv_shape_and_values(client, conn):
    store, y, m = _seed_expenses(conn, 2)
    rows = _expense_rows(conn, store, m)
    ids = [r['id'] for r in rows]
    form = {'row': [str(i) for i in ids]}
    for i, row in zip(ids, rows):
        form[f'include_{i}'] = '1'
        form[f'desc_{i}'] = row['note']
        form[f'vat_{i}'] = 'standard' if row['note'] == 'waybills' else 'novat'
        form[f'code_{i}'] = '6240' if row['note'] == 'waybills' else '6330'
        form[f'gross_{i}'] = '82.90' if row['note'] == 'waybills' else '97.90'

    r = client.post(f'/cash/{store}/{y}/{m}/mj/export', data=form)
    assert r.status_code == 200
    assert 'text/csv' in r.content_type
    assert '.csv' in r.headers['Content-Disposition']

    parsed = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    assert list(parsed[0].keys()) == cash_mj.CSV_HEADER
    # 2 expense lines + the POS contra (this combo needs no rounding line)
    assert len(parsed) == 3
    printing = next(p for p in parsed if p['*AccountCode'] == '6240')
    assert printing['*Amount'] == '72.09' and printing['Gross (incl VAT) check'] == '82.90'
    assert printing['*TaxRate'] == 'Standard Rate Purchases'
    assert printing['TrackingOption1'] == 'Test Store - XX'
    milk = next(p for p in parsed if p['*AccountCode'] == '6330')
    assert milk['*Amount'] == '97.90'  # no VAT -> net == gross
    # Contra credit = total gross (82.90 + 97.90), negative = credit.
    contra = next(p for p in parsed if p['Description'].startswith('POS expenses'))
    assert contra['*Amount'] == '-180.80'
    assert contra['*TaxRate'] == 'No VAT (0%)'


def test_mj_export_dates_are_ddmmyyyy(client, conn):
    """The finance Xero org imports DD/MM/YYYY (proven by the cash-split journal
    workbook). The expense MJ once emitted YYYY/MM/DD, which a SA-locale Xero
    reads as an invalid day — every row's *Date must be DD/MM/YYYY, month-end."""
    store, y, m = _seed_expenses(conn, 7)
    rows = _expense_rows(conn, store, m)
    ids = [r['id'] for r in rows]
    form = {'row': [str(i) for i in ids]}
    for i, row in zip(ids, rows):
        form[f'include_{i}'] = '1'
        form[f'desc_{i}'] = row['note']
        form[f'vat_{i}'] = 'novat'
        form[f'code_{i}'] = '6330'
        form[f'gross_{i}'] = '10.00'
    r = client.post(f'/cash/{store}/{y}/{m}/mj/export', data=form)
    parsed = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    # 2099-07 -> month-end 31/07/2099, on the store lines AND the POS contra.
    assert {p['*Date'] for p in parsed} == {'31/07/2099'}


def test_mj_export_excludes_unticked_lines(client, conn):
    store, y, m = _seed_expenses(conn, 3)
    ids = [r['id'] for r in _expense_rows(conn, store, m)]
    # Only include the first line; leave the second unticked.
    form = {'row': [str(i) for i in ids]}
    keep = ids[0]
    form[f'include_{keep}'] = '1'
    form[f'desc_{keep}'] = 'kept'
    form[f'vat_{keep}'] = 'novat'
    form[f'code_{keep}'] = '6330'
    form[f'gross_{keep}'] = '10.00'
    r = client.post(f'/cash/{store}/{y}/{m}/mj/export', data=form)
    parsed = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    # 1 kept expense line + the POS contra (no rounding needed at R10.00 no-VAT)
    expense_lines = [p for p in parsed if not p['Description'].startswith('POS expenses')
                     and p['Description'] != 'rounding']
    assert len(expense_lines) == 1 and expense_lines[0]['Description'] == 'kept'


def test_mj_export_refuses_unmapped_account(client, conn):
    store, y, m = _seed_expenses(conn, 4)
    row = _expense_rows(conn, store, m)[0]
    form = {
        'row': str(row['id']), f'include_{row["id"]}': '1',
        f'desc_{row["id"]}': 'missing account', f'vat_{row["id"]}': 'novat',
        f'code_{row["id"]}': '', f'gross_{row["id"]}': '10.00',
    }
    r = client.post(f'/cash/{store}/{y}/{m}/mj/export', data=form,
                    follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers['Location'].endswith(f'/cash/{store}/{y}/{m}/mj')


def _one_line_form(row, **overrides):
    """A minimally valid single-line export post, before an override breaks it."""
    i = row['id']
    form = {'row': str(i), f'include_{i}': '1', f'desc_{i}': 'a line',
            f'vat_{i}': 'novat', f'code_{i}': '6330', f'gross_{i}': '10.00'}
    form.update({f'{k}_{i}': v for k, v in overrides.items()})
    return form


# Every one of these used to produce a file rather than a refusal: a blank
# amount silently became 0.00, an unknown VAT key raised a KeyError deeper in
# cash_mj, and '1e400' reached money.to_cents as an OverflowError — a 500 on a
# download button. A Xero journal must be refused, never guessed at.
BAD_LINE_POSTS = [
    ('invalid VAT rate', {'vat': 'twenty-percent'}),
    ('zero gross', {'gross': '0'}),
    ('negative gross', {'gross': '-10.00'}),
    ('non-finite gross', {'gross': '1e400'}),
    ('unreadable gross', {'gross': 'R ten'}),
]


def test_mj_export_refuses_every_unusable_amount_and_vat(client, conn):
    store, y, m = _seed_expenses(conn, 5)
    row = _expense_rows(conn, store, m)[0]
    for label, override in BAD_LINE_POSTS:
        r = client.post(f'/cash/{store}/{y}/{m}/mj/export',
                        data=_one_line_form(row, **override), follow_redirects=False)
        assert r.status_code in (302, 303), f'{label} should bounce, got {r.status_code}'
        assert 'text/csv' not in r.content_type, label
        assert r.headers['Location'].endswith(f'/cash/{store}/{y}/{m}/mj'), label
    # Control: the same post without the override still downloads.
    ok = client.post(f'/cash/{store}/{y}/{m}/mj/export', data=_one_line_form(row))
    assert ok.status_code == 200 and 'text/csv' in ok.content_type


def test_mj_export_refuses_a_store_missing_its_xero_setup(client, conn):
    """The preview renders #### so finance can see what to fix; the download
    must not — the tracking option and the POS account are both required."""
    store, y, m = _seed_expenses(conn, 6)
    row = _expense_rows(conn, store, m)[0]
    before = conn.execute(
        "SELECT xero_tracking_name, store_code FROM stores WHERE name=?", (store,)).fetchone()
    assert (before['store_code'] or '').strip(), 'fixture store needs a POS code'
    try:
        for column in ('xero_tracking_name', 'store_code'):
            conn.execute(f"UPDATE stores SET {column}=NULL WHERE name=?", (store,))
            conn.commit()
            r = client.post(f'/cash/{store}/{y}/{m}/mj/export',
                            data=_one_line_form(row), follow_redirects=False)
            assert r.status_code in (302, 303), column
            assert 'text/csv' not in r.content_type, column
            conn.execute(f"UPDATE stores SET {column}=? WHERE name=?",
                         (before[column], store))
            conn.commit()
    finally:
        conn.execute("UPDATE stores SET xero_tracking_name=?, store_code=? WHERE name=?",
                     (before['xero_tracking_name'], before['store_code'], store))
        conn.commit()


def test_mj_pages_deny_store_session(staff_client):
    c, emp = staff_client
    store = emp['current_store']
    assert c.get(f'/cash/{store}/2026/6/mj', follow_redirects=False).status_code in (302, 303)
    assert c.get('/cash/xero-setup', follow_redirects=False).status_code in (302, 303)


def test_add_category_appears_and_dupe_rejected(client, conn):
    before = conn.execute("SELECT COUNT(*) FROM recon_categories").fetchone()[0]
    r = client.post('/cash/xero-setup/categories/add',
                    data={'name': 'Waste Removal', 'code': '6180', 'vat': 'standard'},
                    follow_redirects=True)
    assert r.status_code == 200
    row = conn.execute(
        "SELECT kind, xero_code, vat_type, active FROM recon_categories WHERE name='Waste Removal'"
    ).fetchone()
    assert row is not None
    assert row['kind'] == 'expense' and row['xero_code'] == '6180'
    assert row['vat_type'] == 'standard' and row['active'] == 1
    # It shows up in the stores' active picker.
    assert any(c['name'] == 'Waste Removal' for c in __import__('northwind.data.database', fromlist=['_']).get_recon_categories())
    # Duplicate (case-insensitive) is rejected, count unchanged.
    client.post('/cash/xero-setup/categories/add', data={'name': 'waste removal'})
    after = conn.execute("SELECT COUNT(*) FROM recon_categories").fetchone()[0]
    assert after == before + 1


def test_archive_category_hides_from_picker(client, conn):
    from northwind.data import database as db
    cid = db.add_recon_category('Temp Widget', '999', 'novat')  # returns (ok, msg)
    row = conn.execute("SELECT id FROM recon_categories WHERE name='Temp Widget'").fetchone()
    db.set_recon_category_active(row['id'], False)
    names = [c['name'] for c in db.get_recon_categories()]
    assert 'Temp Widget' not in names  # archived -> gone from the active picker
