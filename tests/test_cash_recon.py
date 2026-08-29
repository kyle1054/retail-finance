"""Cash-recon ledger tests: entry add/edit/delete, opening-float carry-forward,
the all-stores overview buckets, the store-side add-entry validation, and a
regression guard that the placeholder Shopify/Variance columns stay gone.

The shared DB copy is session-scoped, so each test uses its OWN store and a
far-future month (2099-xx) to stay isolated from real data and each other.
"""
import time

from northwind.data import database as db


def _store(conn, name):
    conn.execute("INSERT OR IGNORE INTO stores (name) VALUES (?)", (name,))
    conn.commit()
    db.invalidate_stores_cache()
    return name


def _cat(conn, kind):
    return conn.execute(
        "SELECT id FROM recon_categories WHERE kind=? LIMIT 1", (kind,)).fetchone()['id']


def _expense_cat(conn, like):
    return conn.execute(
        "SELECT id FROM recon_categories WHERE kind='expense' AND lower(name) LIKE ? LIMIT 1",
        (like,)).fetchone()['id']


def test_add_entry_sets_direction_and_amount(conn):
    store = _store(conn, 'PYTEST ADD')
    exp = _expense_cat(conn, 'milk%')
    inc = _cat(conn, 'income')
    db.add_recon_entry(store, '2099-05-04', exp, 45.00, 'MARKET CO', created_by=store)
    db.add_recon_entry(store, '2099-05-06', inc, 100.00, 'till', created_by=store)

    month = db.get_recon_month(store, 2099, 5)
    kinds = {(e['description'], e['direction'], e['expense'], e['income']) for e in month['entries']}
    # Expense defaults to 'out', income to 'in'; amounts round-trip through cents.
    assert any(d == 'out' and exp_amt == 45.00 for (_n, d, exp_amt, _i) in kinds)
    assert any(d == 'in' and inc_amt == 100.00 for (_n, d, _e, inc_amt) in kinds)
    assert month['total_in'] == 100.00
    assert month['total_out'] == 45.00


def test_opening_float_carries_forward(conn):
    store = _store(conn, 'PYTEST CARRY')
    inc = _cat(conn, 'income')
    exp = _expense_cat(conn, 'coffee%')
    db.set_recon_opening(store, 2099, 5, 500.00)          # explicit May opening
    db.add_recon_entry(store, '2099-05-10', inc, 200.00, 'sale', created_by=store)
    db.add_recon_entry(store, '2099-05-12', exp, 50.00, 'beans', created_by=store)

    may = db.get_recon_month(store, 2099, 5)
    assert may['opening'] == 500.00
    assert may['closing'] == 650.00                        # 500 + 200 - 50

    # June has NO explicit opening → it must carry May's closing forward.
    june = db.get_recon_month(store, 2099, 6)
    assert june['opening_explicit'] is False
    assert june['opening'] == 650.00


def test_update_and_delete_entry(conn):
    store = _store(conn, 'PYTEST EDIT')
    exp = _expense_cat(conn, 'coffee%')
    exp2 = _expense_cat(conn, 'milk%')
    db.add_recon_entry(store, '2099-04-01', exp, 30.00, 'orig', created_by=store)
    eid = conn.execute(
        "SELECT id FROM cash_recon_entries WHERE store=? AND note='orig'", (store,)).fetchone()['id']

    db.update_recon_entry(eid, '2099-04-02', exp2, 75.50, 'edited')
    row = conn.execute(
        "SELECT entry_date, category_id, amount_cents, note FROM cash_recon_entries WHERE id=?",
        (eid,)).fetchone()
    assert row['entry_date'] == '2099-04-02'
    assert row['category_id'] == exp2
    assert row['amount_cents'] == 7550
    assert row['note'] == 'edited'

    db.delete_recon_entry(eid)
    assert conn.execute("SELECT 1 FROM cash_recon_entries WHERE id=?", (eid,)).fetchone() is None


def test_opening_balance_route_sets_the_float(client, conn):
    """Admin posts the opening float; it persists as an EXPLICIT opening for that
    month. Invalid input must not blank an existing float."""
    store = _store(conn, 'PYTEST OPENING RT')
    r = client.post(f'/cash/{store}/2099/2/opening',
                    data={'opening': '750.00'}, follow_redirects=False)
    assert r.status_code in (302, 303)
    month = db.get_recon_month(store, 2099, 2)
    assert month['opening'] == 750.00 and month['opening_explicit'] is True
    # A non-numeric post bounces and leaves the saved float untouched.
    bad = client.post(f'/cash/{store}/2099/2/opening',
                      data={'opening': 'R lots'}, follow_redirects=False)
    assert bad.status_code in (302, 303)
    assert db.get_recon_month(store, 2099, 2)['opening'] == 750.00


def test_opening_balance_route_denies_store_session(staff_client, conn):
    """Setting the float is an admin action — a store session is refused even
    though the card is hidden from it (defense in depth)."""
    c, emp = staff_client
    store = emp['current_store']
    r = c.post(f'/cash/{store}/2099/3/opening',
               data={'opening': '999.00'}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert db.get_recon_month(store, 2099, 3)['opening_explicit'] is False


def test_edit_entry_route_updates_and_scopes_to_real_owner(client, conn):
    """The admin edit route updates the entry and redirects to the entry's REAL
    owner store — the store is taken from the entry id, never a posted field."""
    store = _store(conn, 'PYTEST EDIT RT')
    exp = _expense_cat(conn, 'coffee%')
    exp2 = _expense_cat(conn, 'milk%')
    db.add_recon_entry(store, '2099-04-01', exp, 30.00, 'orig', created_by=store)
    eid = conn.execute(
        "SELECT id FROM cash_recon_entries WHERE store=? AND note='orig'", (store,)).fetchone()['id']

    r = client.post(f'/cash/entry/{eid}/edit',
                    data={'category_id': exp2, 'amount': '88.00', 'entry_date': '2099-04-05',
                          'note': 'edited via route', 'year': 2099, 'month': 4},
                    follow_redirects=False)
    assert r.status_code in (302, 303)
    assert db.get_recon_entry_store(eid) == store        # owner unchanged
    row = conn.execute(
        "SELECT category_id, amount_cents, note, entry_date FROM cash_recon_entries WHERE id=?",
        (eid,)).fetchone()
    assert (row['category_id'], row['amount_cents'], row['note'], row['entry_date']) == \
        (exp2, 8800, 'edited via route', '2099-04-05')
    # An unknown entry id bounces rather than 500ing.
    missing = client.post('/cash/entry/99999999/edit',
                          data={'category_id': exp2, 'amount': '5', 'note': 'x'},
                          follow_redirects=False)
    assert missing.status_code in (302, 303)


def test_edit_entry_route_denies_store_session(staff_client, conn):
    """Editing any entry is admin-only; a store session is refused outright."""
    c, emp = staff_client
    store = emp['current_store']
    exp = _expense_cat(conn, 'milk%')
    db.add_recon_entry(store, '2099-04-20', exp, 12.00, 'store owned', created_by=store)
    eid = conn.execute(
        "SELECT id FROM cash_recon_entries WHERE store=? AND note='store owned'",
        (store,)).fetchone()['id']
    r = c.post(f'/cash/entry/{eid}/edit',
               data={'category_id': exp, 'amount': '999', 'entry_date': '2099-04-20',
                     'note': 'hijack', 'year': 2099, 'month': 4}, follow_redirects=False)
    assert r.status_code in (302, 303)
    row = conn.execute("SELECT amount_cents, note FROM cash_recon_entries WHERE id=?",
                       (eid,)).fetchone()
    assert (row['amount_cents'], row['note']) == (1200, 'store owned')   # untouched


def test_overview_range_buckets(conn):
    store = _store(conn, 'PYTEST BUCKETS')
    inc = _cat(conn, 'income')
    exp = _expense_cat(conn, 'milk%')
    banked = _cat(conn, 'transfer')
    adjust = _cat(conn, 'adjustment')
    db.set_recon_opening(store, 2099, 3, 1000.00)
    db.add_recon_entry(store, '2099-03-02', inc, 5000.00, 'sales', created_by=store)
    db.add_recon_entry(store, '2099-03-05', exp, 120.00, 'milk', created_by=store)
    db.add_recon_entry(store, '2099-03-08', banked, 3000.00, 'to bank', created_by=store)
    db.add_recon_entry(store, '2099-03-09', adjust, 20.00, 'short', created_by=store)

    rows = db.get_recon_overview_range('2099-03-01', '2099-03-31', stores=[store])
    assert len(rows) == 1
    r = rows[0]
    assert r['opening'] == 1000.00
    assert r['total_in'] == 5000.00
    assert r['total_expense'] == 120.00
    assert r['total_banked'] == 3000.00
    assert r['total_adjust'] == 20.00
    # closing = opening + in - expense - banked - adjust
    assert r['closing'] == 1000.00 + 5000.00 - 120.00 - 3000.00 - 20.00
    assert r['entry_count'] == 4


def test_store_add_entry_validation(staff_client, conn):
    c, emp = staff_client
    store = emp['current_store']
    exp = _expense_cat(conn, 'milk%')

    def count():
        return conn.execute(
            "SELECT COUNT(*) FROM cash_recon_entries WHERE store=? AND substr(entry_date,1,7)='2099-09'",
            (store,)).fetchone()[0]

    base = count()
    # Amount <= 0 rejected.
    c.post(f'/cash/{store}/2099/9/add',
           data={'category_id': exp, 'amount': '0', 'entry_date': '2099-09-03', 'note': 'x'})
    assert count() == base
    # Missing reason rejected (server-side, not just client).
    c.post(f'/cash/{store}/2099/9/add',
           data={'category_id': exp, 'amount': '10', 'entry_date': '2099-09-03', 'note': '   '})
    assert count() == base
    # Valid entry accepted.
    c.post(f'/cash/{store}/2099/9/add',
           data={'category_id': exp, 'amount': '12.50', 'entry_date': '2099-09-03', 'note': 'valid'})
    assert count() == base + 1


def test_store_login_cookie_survives_a_closed_browser(db_copy):
    """A store works on a shop-floor tablet. Its session cookie MUST carry an
    Expires — a bare browser-session cookie is dropped whenever iPadOS closes or
    evicts Safari, and the next POST then came back as an expired-session redirect
    that threw away whatever the store had typed.

    Both store-login doors are checked: the unified landing form and /staff/login.
    """
    import app as a
    from northwind.auth import routes as routes_auth
    from northwind.services import security
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False

    rows = db.get_all_store_emails()
    if not rows:
        return
    email = rows[0]['email']
    shared = routes_auth.STAFF_PORTAL_PASSWORD

    for path, form in (('/', {'identifier': email, 'password': shared}),
                       ('/staff/login', {'action': 'login', 'email': email,
                                         'password': shared})):
        security.reset(security.make_key('login', email, '127.0.0.1'))
        security.reset(security.make_key('staff', email, '127.0.0.1'))
        c = a.app.test_client()
        r = c.post(path, data=form)
        assert r.status_code == 302, f'{path} should have logged the store in'
        cookie = next(v for v in r.headers.getlist('Set-Cookie')
                      if v.startswith('session='))
        assert 'Expires=' in cookie, f'{path} set a browser-session cookie: {cookie}'


def test_add_entry_flags_a_confirmed_save(staff_client, conn):
    """Only a landed entry redirects with ?added=1 — that flag is what tells the
    browser it may drop the saved draft. A rejection must NOT carry it, or a
    tablet would clear the draft on the very bounce it exists to survive."""
    c, emp = staff_client
    store = emp['current_store']
    exp = _expense_cat(conn, 'milk%')

    ok = c.post(f'/cash/{store}/2099/10/add',
                data={'category_id': exp, 'amount': '31.00',
                      'entry_date': '2099-10-02', 'note': 'draft flag'})
    assert 'added=1' in ok.headers['Location']

    rejected = c.post(f'/cash/{store}/2099/10/add',
                      data={'category_id': exp, 'amount': '31.00',
                            'entry_date': '2099-10-02', 'note': '  '})
    assert 'added=1' not in rejected.headers['Location']


def test_entry_cannot_land_outside_the_month_it_was_posted_from(staff_client, conn):
    """The date field used to default to TODAY whatever month was on screen, so an
    entry added from an older month was filed under today's date, the page it
    returned to never showed it, and the store read that as a failed save. The date
    input is now bounded to the month; the server refuses a mismatch outright."""
    c, emp = staff_client
    store = emp['current_store']
    exp = _expense_cat(conn, 'milk%')

    def count(ym):
        return conn.execute(
            "SELECT COUNT(*) FROM cash_recon_entries WHERE store=? "
            "AND substr(entry_date,1,7)=?", (store, ym)).fetchone()[0]

    before_jul, before_aug = count('2099-07'), count('2099-08')
    r = c.post(f'/cash/{store}/2099/7/add',
               data={'category_id': exp, 'amount': '250.00',
                     'entry_date': '2099-08-05', 'note': 'wrong month'})
    assert count('2099-07') == before_jul          # not silently filed here
    assert count('2099-08') == before_aug          # nor over on August's page
    assert 'added=1' not in r.headers['Location']  # so the draft is kept

    # The ledger form offers only dates inside the month being viewed.
    page = c.get(f'/cash/{store}/2099/7').get_data(as_text=True)
    assert 'min="2099-07-01"' in page
    assert 'max="2099-07-31"' in page
    # Today clamped into the month: the 1st for a month still ahead of today (2099),
    # the month's last day for one already past, today itself for the current month.
    assert 'value="2099-07-01"' in page


def test_entry_date_must_be_a_real_day(staff_client, conn):
    """The in-month guard parses the date instead of comparing its first 7 chars.
    Nothing downstream parses entry_date — add_recon_entry stores the string as
    given — so a prefix-only check let '2099-09' and '2099-09-99' through and left
    rows whose date breaks the day grouping, the admin edit form and the day link."""
    c, emp = staff_client
    store = emp['current_store']
    exp = _expense_cat(conn, 'milk%')

    def rows():
        return conn.execute(
            "SELECT entry_date FROM cash_recon_entries WHERE store=? "
            "AND substr(entry_date,1,7)='2099-09'", (store,)).fetchall()

    before = len(rows())
    for bad in ('2099-09', '2099-09-99', '2099-09-1x'):
        r = c.post(f'/cash/{store}/2099/9/add',
                   data={'category_id': exp, 'amount': '12.00',
                         'entry_date': bad, 'note': 'malformed date'})
        assert 'added=1' not in r.headers['Location'], bad
    assert len(rows()) == before

    # A real day inside the month still goes through, stored normalised.
    ok = c.post(f'/cash/{store}/2099/9/add',
                data={'category_id': exp, 'amount': '12.00',
                      'entry_date': '2099-09-07', 'note': 'good date'})
    assert 'added=1' in ok.headers['Location']
    assert '2099-09-07' in [row['entry_date'] for row in rows()]


def test_saved_rows_carry_a_draft_match_key(staff_client, conn):
    """Each ledger row stamps category|cents|day. A restored draft matches itself
    against those, so an entry that saved but whose response was lost (dropped wifi,
    evicted tab) is recognised instead of being re-offered and posted twice."""
    c, emp = staff_client
    store = emp['current_store']
    exp = _expense_cat(conn, 'milk%')
    db.add_recon_entry(store, '2099-12-04', exp, 42.50, 'match key', created_by=store)
    body = c.get(f'/cash/{store}/2099/12').get_data(as_text=True)
    assert f'data-line="{exp}|4250|2099-12-04"' in body
    # The reason is part of the match: it separates a deliberate second identical
    # line from a re-offered draft of the one already saved.
    assert 'data-note="match key"' in body


def test_sticky_ledger_header_is_not_trapped_in_a_scroll_container(client):
    """An overflow on either axis makes .cr-sheet-scroll a scroll container on both,
    which pins the sticky thead to a box that never scrolls vertically — the header
    stops sticking. That wrapper is a narrow-PHONE measure (the sheet does not fit),
    so it must not be keyed on touch, or every iPad loses its column headers."""
    css = client.get('/static/cash-shell.css').get_data(as_text=True)
    scroll_rule = next(line for line in css.splitlines()
                       if '.cr-sheet-scroll { overflow-x:auto' in line)
    block = css[:css.index(scroll_rule)]
    guard = block.rsplit('@media', 1)[1].split('{')[0]
    assert 'pointer' not in guard, f'sheet scroller keyed on touch: @media{guard}'
    assert 'max-width:700px' in guard
    # And where the scroller DOES apply, sticky is switched off — that override has
    # the same specificity as the base rule, so it only wins on source order.
    sticky = css.index('.cr-ledger thead th { position:sticky')
    static = css.index('.cr-ledger thead th { position:static')
    assert static > sticky, 'the position:static override is dead — it precedes the base rule'


def test_ledger_is_touch_sized_for_tablets(staff_client, conn):
    """The store ledger renders the tablet affordances: a numeric keypad for the
    amount, inline error slots instead of alert(), and the draft-restored note."""
    c, emp = staff_client
    store = emp['current_store']
    db.add_recon_entry(store, '2099-11-03', _expense_cat(conn, 'milk%'),
                       9.00, 'touch test', created_by=store)
    r = c.get(f'/cash/{store}/2099/11')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'inputmode="decimal"' in body       # numeric keypad, not full QWERTY
    assert 'id="amountError"' in body          # inline validation slots exist
    assert 'id="catError"' in body
    assert 'id="noteError"' in body
    assert 'id="draftNote"' in body            # restored-draft confirmation
    assert 'cr-iconbtn' in body                # row actions carry the sized class


def test_touch_stylesheet_covers_the_ipad_range(client):
    """Tablet rules must key on pointer, not width: every iPad is 744-1366px wide,
    so the old width-only breakpoints left the whole range desktop-sized."""
    r = client.get('/static/cash-shell.css')
    assert r.status_code == 200
    css = r.get_data(as_text=True)
    assert '(pointer: coarse)' in css
    # Row actions must not be hover-gated on touch (invisible icons otherwise).
    assert '@media (hover: hover) and (pointer: fine)' in css
    # 16px input floor stops iOS zooming the page on focus.
    assert 'font-size:16px' in css
    # Bootstrap assigns headings their own dark colour, so inheritance from the
    # dark period panel is not enough. Keep the month explicitly readable.
    assert '.cash-period-current h2' in css
    assert '.cash-period-current h2 { margin:5px 0 2px;color:#fff' in css


def test_overview_has_no_placeholder_columns(client, conn):
    """Regression guard: the Shopify & Variance 'coming soon' table COLUMNS were
    removed — the admin overview must never render them as columns again. (The
    header still legitimately links to the separate Shopify-compare export.)"""
    store = _store(conn, 'PYTEST NOCOL')
    db.add_recon_entry(store, '2099-08-04', _expense_cat(conn, 'milk%'), 10.00, 'm', created_by=store)
    r = client.get('/cash?start=2099-08-01&end=2099-08-31')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert store in body                       # the table actually rendered
    assert '>Shopify</th>' not in body         # no Shopify column header
    assert '>Variance</th>' not in body        # no Variance column header
    assert 'coming soon' not in body           # no placeholder caption


def test_overview_has_store_navigation_and_movement_filters(client, conn):
    store = _store(conn, 'PYTEST QUICK OPEN')
    body = client.get('/cash?start=2099-08-01&end=2099-08-31').get_data(as_text=True)
    assert 'data-cash-store-search' in body
    assert 'data-cash-store-filter="in"' in body
    assert 'data-cash-store-filter="out"' in body
    # Each row links straight to its reconciliation. There is deliberately no
    # store-picker <select> as well: it duplicated every store name already on
    # the page, cost an extra round trip, and dropped the selected date range.
    assert f'Open {store} cash reconciliation' in body
    assert 'Choose a store reconciliation' not in body
    assert f'/cash/{store}/2099/8/summary'.replace(' ', '%20') in body
