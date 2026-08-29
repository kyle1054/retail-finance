"""Staff requests: the portal ask, the admin workflow, and the plan it becomes.

The money assertions matter most — a converted request must produce exactly the
plan a hand-captured one would, because it goes through the same plans.py calls.
"""
import re
import time

import pytest

from northwind.data import database as db
from northwind.deductions import requests as reqs


@pytest.fixture
def emp(conn):
    """An active retail employee with no requests of their own yet."""
    row = conn.execute(
        "SELECT id, full_name, current_store FROM employees "
        "WHERE status='active' AND sector='retail' AND current_store IS NOT NULL "
        "AND id NOT IN (SELECT employee_id FROM staff_requests) LIMIT 1").fetchone()
    assert row is not None, 'no usable employee in the test database'
    return row


def _make(conn, emp_id, kind='uniform', items=None, **kw):
    with conn:
        return reqs.create_request(
            conn, emp_id, kind,
            items if items is not None else [{'description': 'Sneakers',
                                              'unit_price': 1200, 'quantity': 1}],
            actor='employee:%s' % emp_id, actor_role='staff', **kw)


# ── Creating ─────────────────────────────────────────────────────────────────
def test_create_records_the_ask_and_opens_a_thread(conn, emp):
    made = _make(conn, emp['id'], term=6, notes='before December')
    assert made['ref'].startswith('REQ-')
    row = conn.execute("SELECT * FROM staff_requests WHERE id=?", (made['id'],)).fetchone()
    assert row['status'] == 'submitted'
    assert row['store'] == emp['current_store']
    assert row['created_by'] == 'employee:%s' % emp['id']
    assert row['estimated_total_cents'] == 120000          # cents, not rands
    assert row['requested_term_months'] == 6
    events = conn.execute(
        "SELECT event, to_status FROM staff_request_events WHERE request_id=?",
        (made['id'],)).fetchall()
    assert [(e['event'], e['to_status']) for e in events] == [('created', 'submitted')]


def test_items_may_arrive_without_a_price(conn, emp):
    """Staff routinely don't know the price — that must not block the ask."""
    made = _make(conn, emp['id'], items=[{'description': 'Socks', 'quantity': 2}])
    items = reqs.get_items(conn, made['id'])
    assert items[0]['price'] is None and items[0]['total'] is None
    assert made['estimated_total'] is None


def test_blank_and_bad_asks_are_refused(conn, emp):
    with pytest.raises(ValueError):
        _make(conn, emp['id'], items=[{'description': '   '}])
    with pytest.raises(ValueError):
        _make(conn, emp['id'], items=[{'description': 'Cap', 'unit_price': 'abc'}])
    with pytest.raises(ValueError):
        _make(conn, emp['id'], term=99)
    with pytest.raises(ValueError):
        _make(conn, emp['id'], kind='holiday')


def test_open_requests_are_capped(conn, emp):
    for _ in range(reqs.MAX_OPEN_PER_EMPLOYEE):
        _make(conn, emp['id'])
    with pytest.raises(ValueError, match='already have'):
        _make(conn, emp['id'])
    # Closing one frees the slot again.
    last = conn.execute("SELECT id FROM staff_requests WHERE employee_id=? "
                        "ORDER BY id DESC LIMIT 1", (emp['id'],)).fetchone()
    with conn:
        reqs.set_status(conn, last['id'], 'cancelled', 'employee:%s' % emp['id'],
                        actor_role='staff')
    _make(conn, emp['id'])


# ── Workflow ─────────────────────────────────────────────────────────────────
def test_claim_then_question_then_staff_reply_returns_it_to_the_queue(conn, emp):
    made = _make(conn, emp['id'])
    with conn:
        assert reqs.claim(conn, made['id'], 'admin') == 'in_progress'
        assert reqs.add_comment(conn, made['id'], 'What size?', 'admin',
                                ask_for_info=True) == 'info_needed'
        assert reqs.add_comment(conn, made['id'], 'UK5', 'employee:%s' % emp['id'],
                                actor_role='staff') == 'in_progress'
    row = conn.execute("SELECT claimed_by, status FROM staff_requests WHERE id=?",
                       (made['id'],)).fetchone()
    assert (row['claimed_by'], row['status']) == ('admin', 'in_progress')


def test_declining_needs_a_reason_and_closes_the_request(conn, emp):
    made = _make(conn, emp['id'])
    with conn:
        with pytest.raises(ValueError, match='reason'):
            reqs.set_status(conn, made['id'], 'declined', 'admin')
        reqs.set_status(conn, made['id'], 'declined', 'admin', message='Not this season')
    row = conn.execute("SELECT status, decline_reason, decided_by FROM staff_requests "
                       "WHERE id=?", (made['id'],)).fetchone()
    assert row['status'] == 'declined'
    assert row['decline_reason'] == 'Not this season'
    assert row['decided_by'] == 'admin'
    with conn:
        with pytest.raises(ValueError):        # history stays history
            reqs.set_status(conn, made['id'], 'in_progress', 'admin')
        with pytest.raises(ValueError):
            reqs.add_comment(conn, made['id'], 'one more thing', 'admin')


def test_staff_may_only_cancel_their_own_open_request(conn, emp):
    made = _make(conn, emp['id'])
    staff = 'employee:%s' % emp['id']
    with conn:
        with pytest.raises(ValueError, match='only cancel'):
            reqs.set_status(conn, made['id'], 'approved', staff, actor_role='staff')
        reqs.set_status(conn, made['id'], 'cancelled', staff, actor_role='staff')
    assert conn.execute("SELECT status FROM staff_requests WHERE id=?",
                        (made['id'],)).fetchone()['status'] == 'cancelled'


# ── Becoming a real deduction ────────────────────────────────────────────────
def test_uniform_conversion_creates_the_plan_the_admin_typed(conn, emp):
    made = _make(conn, emp['id'], term=6)
    with conn:
        result = reqs.convert_to_plan(conn, made['id'], 'admin', start_year=2099,
                                     start_month=3, term=6, total=1200, sku='770123',
                                     sale_number='SO-1')
    plan = conn.execute("SELECT * FROM uniform_deductions WHERE id=?",
                        (result['plan_id'],)).fetchone()
    assert plan['total_amount'] == 1200.0
    assert plan['monthly_amount'] == 200.0          # derived when left blank
    assert (plan['term_months'], plan['start_month'], plan['start_year']) == (6, 3, 2099)
    assert plan['sku'] == '770123'
    assert made['ref'] in plan['notes']             # the plan points back at the ask
    row = conn.execute("SELECT * FROM staff_requests WHERE id=?", (made['id'],)).fetchone()
    assert (row['status'], row['plan_type'], row['plan_id']) == \
        ('fulfilled', 'uniform', result['plan_id'])
    # And it cannot be converted twice.
    with conn:
        with pytest.raises(ValueError, match='already'):
            reqs.convert_to_plan(conn, made['id'], 'admin', start_year=2099,
                                 start_month=3, term=6, total=1200)


def test_layby_conversion_applies_the_staff_discount(conn, emp):
    made = _make(conn, emp['id'], kind='layby',
                 items=[{'description': 'Jacket', 'unit_price': 2000, 'quantity': 1},
                        {'description': 'Cap', 'quantity': 2}])
    with conn:
        result = reqs.convert_to_plan(
            conn, made['id'], 'admin', start_year=2099, start_month=3, term=3,
            items=[{'description': 'Jacket', 'unit_price': 2000, 'quantity': 1},
                   {'description': 'Cap', 'unit_price': 250, 'quantity': 2}],
            discount_pct=40)
    plan = conn.execute("SELECT * FROM layby_deductions WHERE id=?",
                        (result['plan_id'],)).fetchone()
    assert plan['basket_total'] == 2500.0
    assert plan['total_amount'] == 1500.0            # 40% staff discount
    assert plan['monthly_amount'] == 500.0
    items = conn.execute("SELECT * FROM layby_items WHERE layby_id=? ORDER BY id",
                         (result['plan_id'],)).fetchall()
    assert [(i['description'], i['line_total']) for i in items] == \
        [('Jacket', 2000.0), ('Cap', 500.0)]


def test_a_layby_cannot_be_converted_while_a_price_is_missing(conn, emp):
    """A request tolerates a blank price; money never may."""
    made = _make(conn, emp['id'], kind='layby',
                 items=[{'description': 'Cap', 'quantity': 1}])
    with conn:
        with pytest.raises(ValueError, match='price'):
            reqs.convert_to_plan(conn, made['id'], 'admin', start_year=2099,
                                 start_month=3, term=3)
    assert conn.execute("SELECT plan_id FROM staff_requests WHERE id=?",
                        (made['id'],)).fetchone()['plan_id'] is None


def test_conversion_respects_a_locked_payroll_period(conn, emp):
    """The lock lives in plans.py; this proves requests can't route around it."""
    made = _make(conn, emp['id'])
    with conn:
        conn.execute("INSERT OR IGNORE INTO locked_periods (sector, year, month) "
                     "VALUES ('retail', 2098, 4)")
    try:
        with conn:
            with pytest.raises(ValueError, match='locked'):
                reqs.convert_to_plan(conn, made['id'], 'admin', start_year=2098,
                                     start_month=4, term=6, total=1200)
        assert conn.execute("SELECT plan_id FROM staff_requests WHERE id=?",
                            (made['id'],)).fetchone()['plan_id'] is None
    finally:
        with conn:
            conn.execute("DELETE FROM locked_periods WHERE year=2098 AND month=4")


# ── Routes: the portal side is store-scoped ──────────────────────────────────
def test_staff_can_submit_and_follow_up_from_the_portal(staff_client):
    client, employee = staff_client
    resp = client.get('/portal/store/%s/request/uniform' % employee['id'])
    assert resp.status_code == 200
    resp = client.post('/portal/store/%s/request/uniform' % employee['id'], data={
        'item_desc_0': 'Ladies sneakers', 'item_sku_0': '660200123456',
        'item_size_0': 'UK5', 'item_qty_0': '1', 'item_price_0': '1200',
        'term_months': '6', 'notes': 'before December',
    }, follow_redirects=True)
    assert resp.status_code == 200
    conn = db.get_db()
    row = conn.execute("SELECT * FROM staff_requests WHERE employee_id=? "
                       "ORDER BY id DESC LIMIT 1", (employee['id'],)).fetchone()
    conn.close()
    assert row['created_by'] == 'employee:%s' % employee['id']
    assert row['created_via'] == 'portal'
    conn2 = db.get_db()
    try:
        assert reqs.get_items(conn2, row['id'])[0]['sku'] == '660200123456'
    finally:
        conn2.close()
    # The profile advertises the ask and lists it back.
    page = client.get('/portal/store/%s' % employee['id']).get_data(as_text=True)
    assert 'Request uniform' in page and row['ref'] in page


def test_a_store_cannot_touch_another_stores_request(staff_client, conn):
    client, employee = staff_client
    other = conn.execute(
        "SELECT id FROM employees WHERE status='active' AND current_store IS NOT NULL "
        "AND current_store <> ? LIMIT 1", (employee['current_store'],)).fetchone()
    made = _make(conn, other['id'])
    assert client.get('/portal/request/%d' % made['id'],
                      follow_redirects=False).status_code == 302
    resp = client.post('/portal/request/%d/cancel' % made['id'], follow_redirects=False)
    assert resp.status_code == 302
    assert conn.execute("SELECT status FROM staff_requests WHERE id=?",
                        (made['id'],)).fetchone()['status'] == 'submitted'
    # Nor may it raise a request in another store's name.
    assert client.post('/portal/store/%s/request/uniform' % other['id'],
                       data={'item_desc_0': 'Sneakers'},
                       follow_redirects=False).status_code == 302


def test_portal_needs_a_store_session(db_copy):
    import app as a
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False
    anon = a.app.test_client()
    resp = anon.get('/portal/request/1', follow_redirects=False)
    assert resp.status_code == 302
    assert '/staff/login' in resp.headers['Location'] or '/portal' in resp.headers['Location']


# ── Routes: the admin queue ──────────────────────────────────────────────────
def test_queue_lists_and_works_a_request(client, conn, emp):
    made = _make(conn, emp['id'], term=6)
    page = client.get('/requests').get_data(as_text=True)
    assert made['ref'] in page and emp['full_name'] in page

    assert client.post('/requests/%d/claim' % made['id'],
                       follow_redirects=True).status_code == 200
    client.post('/requests/%d/comment' % made['id'],
                data={'message': 'What size?', 'ask_for_info': '1'},
                follow_redirects=True)
    assert conn.execute("SELECT status FROM staff_requests WHERE id=?",
                        (made['id'],)).fetchone()['status'] == 'info_needed'

    resp = client.post('/requests/%d/convert' % made['id'], data={
        'total_amount': '1200', 'term_months': '6', 'start_month': '3',
        'start_year': '2099', 'sale_number': 'SO-9'}, follow_redirects=True)
    assert resp.status_code == 200
    row = conn.execute("SELECT status, plan_id FROM staff_requests WHERE id=?",
                       (made['id'],)).fetchone()
    assert row['status'] == 'fulfilled' and row['plan_id']


def test_queue_filters_and_badge_count(client, conn, emp):
    made = _make(conn, emp['id'])
    assert reqs.pending_count(conn) >= 1
    open_page = client.get('/requests?status=open').get_data(as_text=True)
    assert made['ref'] in open_page
    # A closed status filter must not show it.
    with conn:
        reqs.set_status(conn, made['id'], 'declined', 'admin', message='no')
    assert made['ref'] not in client.get('/requests?status=open').get_data(as_text=True)
    assert made['ref'] in client.get('/requests?status=declined').get_data(as_text=True)


def test_admin_pages_do_not_run_a_query_per_request(client, conn, emp):
    """The queue reads items and events in one query each, not per row."""
    for _ in range(3):
        _make(conn, emp['id'])
    ids = [r['id'] for r in conn.execute(
        "SELECT id FROM staff_requests ORDER BY id DESC LIMIT 3").fetchall()]
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        reqs.list_requests(conn, status='open', with_thread=True, limit=50)
    finally:
        conn.set_trace_callback(None)
    # One batched fetch each ("... WHERE request_id IN (...)"), regardless of how
    # many requests the page lists. The item-count subquery inside the main SELECT
    # is part of that same statement, so it is excluded by matching on the IN form.
    item_queries = [s for s in statements
                    if 'FROM staff_request_items WHERE request_id IN' in s]
    event_queries = [s for s in statements
                     if 'FROM staff_request_events WHERE request_id IN' in s]
    assert len(item_queries) == 1 and len(event_queries) == 1, statements
    assert len(ids) == 3


# ── The Fast-Fill parser is shared, not copy-pasted ──────────────────────────
def test_invoice_parser_is_shared_between_the_queue_and_the_employee_page():
    """One parser, two front doors. employee.html used to own the only copy; the
    queue reuses it via static/invoice-fastfill.js, so neither page may grow a
    second definition that can drift."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    shared = (root / 'static' / 'invoice-fastfill.js').read_text(encoding='utf-8')
    base = (root / 'templates' / 'base.html').read_text(encoding='utf-8')
    employee = (root / 'templates' / 'employee.html').read_text(encoding='utf-8')
    queue = (root / 'templates' / 'requests.html').read_text(encoding='utf-8')

    assert 'function parseInvoiceText' in shared
    assert 'invoice-fastfill.js' in base, 'the shared parser must be loaded'
    assert 'function parseInvoiceText' not in employee, 'second copy of the parser'
    assert 'parseInvoiceText(' in employee, 'the employee page still calls the shared one'
    # The queue drives it declaratively — no ids, no inline JavaScript.
    for hook in ('data-fastfill', 'data-fastfill-input', 'data-fastfill-go',
                 'data-fastfill-status', 'data-fastfill-lines'):
        assert hook in queue, hook
    assert '<script' not in queue, 'the queue must stay inline-JS-free'


# ── The SKU the store types in is the point ──────────────────────────────────
def test_portal_insists_on_a_sku_but_the_domain_default_does_not(staff_client, conn):
    """Chasing SKUs afterwards is the slow part of fulfilling a request, so the
    portal form refuses a line without one. The domain default stays permissive —
    the admin-side and demo paths create asks from data that has no tag to read."""
    client, employee = staff_client
    before = conn.execute("SELECT COUNT(*) c FROM staff_requests").fetchone()['c']
    page = client.post('/portal/store/%s/request/uniform' % employee['id'], data={
        'item_desc_0': 'Ladies sneakers', 'item_qty_0': '1'},
        follow_redirects=True).get_data(as_text=True)
    assert 'SKU' in page
    assert conn.execute("SELECT COUNT(*) c FROM staff_requests").fetchone()['c'] == before

    # Same line, with the SKU, goes through.
    client.post('/portal/store/%s/request/uniform' % employee['id'], data={
        'item_desc_0': 'Ladies sneakers', 'item_sku_0': '660200000111', 'item_qty_0': '1'},
        follow_redirects=True)
    row = conn.execute("SELECT id FROM staff_requests WHERE employee_id=? "
                       "ORDER BY id DESC LIMIT 1", (employee['id'],)).fetchone()
    assert reqs.get_items(conn, row['id'])[0]['sku'] == '660200000111'

    # And the domain layer without the flag still accepts a SKU-less line. Clear
    # this employee's open asks first, or the per-person cap answers instead.
    with conn:
        for open_row in conn.execute(
                "SELECT id FROM staff_requests WHERE employee_id=? AND status IN "
                "('submitted','in_progress','info_needed','approved')",
                (employee['id'],)).fetchall():
            reqs.set_status(conn, open_row['id'], 'cancelled',
                            'employee:%s' % employee['id'], actor_role='staff')
        made = reqs.create_request(conn, employee['id'], 'layby',
                                   [{'description': 'Jacket', 'unit_price': 100}],
                                   actor='admin', actor_role='admin')
    assert reqs.get_items(conn, made['id'])[0]['sku'] == ''


# ── Holding up under a lot of items / a lot of requests ──────────────────────
def test_a_long_basket_does_not_produce_a_runaway_summary(conn, emp):
    """The list row shows a one-line summary. Twenty long product names joined
    together built a 2 000-character string that made one row 980px tall."""
    made = _make(conn, emp['id'], kind='layby', items=[
        {'description': 'NORTHWIND x Renton Brushed Twill Tapered Cargo Trouser Black 34 %d' % n,
         'unit_price': 1899.99} for n in range(1, 21)])
    row = [r for r in reqs.list_requests(conn, status='all', employee_id=emp['id'])
           if r['id'] == made['id']][0]
    assert len(row['items']) == 20, 'every item is still there'
    assert len(row['summary']) <= 110
    assert row['summary'].endswith('…')


def test_the_queue_pages_instead_of_rendering_every_request(client, conn, emp):
    """Each row carries its own forms and thread, so an unpaged queue is heavy
    long before it is unreadable — 50 requests rendered 560 KB of markup."""
    for _ in range(3):
        _make(conn, emp['id'])
    page = client.get('/requests?status=all&per_page=2').get_data(as_text=True)
    assert page.count('class="rq-row"') == 2
    assert 'list-pager' in page, 'the shared pager must be shown'
    # Page 2 is a different window of the same filtered list.
    second = client.get('/requests?status=all&per_page=2&page=2').get_data(as_text=True)
    assert second.count('class="rq-row"') == 2
    first_ref = re.search(r'(REQ-\d+)', page).group(1)
    assert first_ref not in second


# ── What a finished request shows ────────────────────────────────────────────
def test_a_finished_request_carries_the_plan_it_created(conn, emp):
    """"Plan created" on its own meant leaving the queue to find out what was
    written, so the request reads its own plan back."""
    made = _make(conn, emp['id'], term=6)
    with conn:
        reqs.convert_to_plan(conn, made['id'], 'admin', start_year=2099, start_month=3,
                             term=6, total=1200, sale_number='SO-7')
    req = reqs.get_request(conn, made['id'])
    plan = req['plan']
    assert plan['total'] == 1200.0 and plan['monthly'] == 200.0 and plan['term'] == 6
    assert plan['start_label'] == 'Mar 2099'
    assert plan['sale_number'] == 'SO-7'
    assert plan['payments_made'] == 0 and plan['remaining'] == 1200.0
    # And the paste-ready line quotes the real figures, not the estimate.
    msg = req['store_message']
    assert 'REQ-' in msg and '1,200.00' in msg and '200.00' in msg and 'Mar 2099' in msg
    # list_requests batches the same lookup rather than querying per row.
    listed = [r for r in reqs.list_requests(conn, status='all', employee_id=emp['id'])
              if r['id'] == made['id']][0]
    assert listed['plan'] == plan and listed['store_message'] == msg


def test_the_paste_ready_line_covers_every_decided_state(conn, emp):
    approved = _make(conn, emp['id'])
    with conn:
        reqs.set_status(conn, approved['id'], 'approved', 'admin')
    assert 'is approved' in reqs.get_request(conn, approved['id'])['store_message']

    declined = _make(conn, emp['id'])
    with conn:
        reqs.set_status(conn, declined['id'], 'declined', 'admin', message='Not on the list')
    msg = reqs.get_request(conn, declined['id'])['store_message']
    assert 'was not approved' in msg and 'Not on the list' in msg

    # An untouched request has nothing to announce yet.
    fresh = _make(conn, emp['id'])
    assert reqs.get_request(conn, fresh['id'])['store_message'] is None


def test_the_queue_shows_the_finished_figures_and_offers_them_for_pasting(client, conn, emp):
    made = _make(conn, emp['id'], term=4)
    with conn:
        reqs.convert_to_plan(conn, made['id'], 'admin', start_year=2099, start_month=5,
                             term=4, total=800)
    page = client.get('/requests?status=fulfilled').get_data(as_text=True)
    assert made['ref'] in page
    assert 'Deduction created' in page
    assert '<dd>R 800.00</dd>' in page and 'R 200.00 × 4' in page and 'May 2099' in page
    assert 'data-copy-text=' in page, 'the decision must be copy-and-pasteable'
    # Nothing is left to do on a finished request, so no action panels are offered.
    row = page[page.index(made['ref']):]
    row = row[:row.index('</div>\n</div>')] if '</div>\n</div>' in row else row
    assert 'rq-panel-go' not in row


def test_converting_twice_cannot_write_two_deductions(conn, emp):
    """A repeat submit must never write a second deduction. Two lines of defence:
    the status/plan_id check on entry (what this exercises — the plan the second
    call creates is rolled back with its transaction), and `AND plan_id IS NULL`
    on the claiming UPDATE for the case where something else got there between
    the read and the write."""
    made = _make(conn, emp['id'], term=6)
    before = conn.execute("SELECT COUNT(*) c FROM uniform_deductions_cents").fetchone()['c']
    with conn:
        first = reqs.convert_to_plan(conn, made['id'], 'admin', start_year=2099,
                                     start_month=3, term=6, total=1200)
    # Second attempt, as a fresh transaction — the plan it creates must not survive.
    with pytest.raises(ValueError, match='already'):
        with conn:
            reqs.convert_to_plan(conn, made['id'], 'admin', start_year=2099,
                                 start_month=3, term=6, total=1200)
    after = conn.execute("SELECT COUNT(*) c FROM uniform_deductions_cents").fetchone()['c']
    assert after == before + 1, 'exactly one deduction exists for this request'
    assert conn.execute("SELECT plan_id FROM staff_requests WHERE id=?",
                        (made['id'],)).fetchone()['plan_id'] == first['plan_id']


def test_portal_scoping_follows_a_transferred_employee(staff_client, conn):
    """The request stores the store it was raised at, but access must follow the
    person: after a transfer the new store sees it and the old one doesn't."""
    client, employee = staff_client
    made = _make(conn, employee['id'])
    assert client.get('/portal/request/%d' % made['id'],
                      follow_redirects=False).status_code == 200

    # A store with its own login, so the moved-to session is as real as the fixture's.
    destination = conn.execute(
        "SELECT store, email FROM store_emails WHERE store <> ? LIMIT 1",
        (employee['current_store'],)).fetchone()
    other = destination['store']
    try:
        with conn:
            conn.execute("UPDATE employees SET current_store=? WHERE id=?",
                         (other, employee['id']))
        # The store that raised it can no longer open it…
        assert client.get('/portal/request/%d' % made['id'],
                          follow_redirects=False).status_code == 302
        # …and the store they moved to can.
        moved = client.application.test_client()
        with moved.session_transaction() as sess:
            sess['staff_store'] = other
            sess['staff_login'] = destination['email']
            sess['staff_shared'] = True
            sess['staff_last_active'] = time.time()
        assert moved.get('/portal/request/%d' % made['id'],
                         follow_redirects=False).status_code == 200
    finally:
        with conn:
            conn.execute("UPDATE employees SET current_store=? WHERE id=?",
                         (employee['current_store'], employee['id']))


def test_every_action_the_workflow_supports_is_on_the_drawer(client, conn, emp):
    """A layout pass once dropped "Approve — plan it later" silently, leaving a
    supported state unreachable from the UI. Each action is checked by the URL it
    posts to, so restyling is free but removing one is not."""
    made = _make(conn, emp['id'])
    page = client.get('/requests').get_data(as_text=True)
    row = page[page.index(made['ref']):]
    for action, url in [
            ('pick it up', url_for_test('request_claim', made['id'])),
            ('message the store', url_for_test('request_comment', made['id'])),
            ('approve into a plan', url_for_test('request_convert', made['id'])),
            ('approve / decline', url_for_test('request_set_status', made['id'])),
    ]:
        assert url in row, 'the drawer no longer offers: %s' % action
    # Approve-later and Decline share the status endpoint; both must be present.
    assert row.count(url_for_test('request_set_status', made['id'])) >= 2
    assert 'value="approved"' in row and 'value="declined"' in row


def url_for_test(endpoint, req_id):
    return {'request_claim': '/requests/%d/claim',
            'request_comment': '/requests/%d/comment',
            'request_convert': '/requests/%d/convert',
            'request_set_status': '/requests/%d/status'}[endpoint] % req_id


def test_the_schema_refuses_nonsense(conn):
    """The state machine lives in Python, but the table refuses the states and
    money it was never meant to hold — so a stray script can't invent either."""
    import sqlite3
    for bad in ("INSERT INTO staff_requests (kind, employee_id) VALUES ('holiday','EMP-0001')",
                "INSERT INTO staff_requests (kind, employee_id, status) "
                "VALUES ('uniform','EMP-0001','maybe')",
                "INSERT INTO staff_requests (kind, employee_id, estimated_total_cents) "
                "VALUES ('uniform','EMP-0001',-5)",
                "INSERT INTO staff_request_items (request_id, description, quantity) "
                "VALUES (1,'x',0)"):
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(bad)
    names = {r['name'] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'staff_request%' OR "
        "name LIKE 'idx_staff_request%'")}
    assert {'staff_requests', 'staff_request_items', 'staff_request_events',
            'idx_staff_requests_status', 'idx_staff_requests_employee',
            'idx_staff_requests_store'} <= names
