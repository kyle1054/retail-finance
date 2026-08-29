"""The shared plan-write service layer (northwind/deductions/plans.py).

This is the file the MCP connector's plan-writing tools and the web routes BOTH go
through, so it is the right place to pin behaviour. Everything here runs on the 3.9
dev venv (the service layer is deliberately Flask-free and fastmcp-free), which is
why it exists: the MCP tool modules can only be imported in the 3.10+ container, so
without this layer the write logic had no locally-runnable coverage at all.

Covers: creation of all three plan types, the lock refusals, the two DIFFERENT
legacy balance fallbacks in adjust, actor stamping, write-off (including the
undercharge ledger/schedule side effects), and route↔service parity.
"""
from northwind.data import database as db
from northwind.deductions import plans


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _emp(conn, sector='retail'):
    return conn.execute(
        "SELECT id FROM employees WHERE sector=? AND status='active' LIMIT 1",
        (sector,)).fetchone()['id']


def _open_period(conn, sector='retail'):
    """A (year, month) that is NOT locked for the sector — far enough out to be free."""
    for year in (2030, 2031):
        for month in range(1, 13):
            if not db.is_period_locked(year, month, sector):
                return year, month
    raise AssertionError("no unlocked period available in the test DB")


def _lock(conn, year, month, sector='retail'):
    conn.execute("INSERT OR IGNORE INTO locked_periods (year, month, sector) "
                 "VALUES (?,?,?)", (year, month, sector))
    conn.commit()


def _unlock(conn, year, month, sector='retail'):
    conn.execute("DELETE FROM locked_periods WHERE year=? AND month=? AND sector=?",
                 (year, month, sector))
    conn.commit()


def _cleanup(conn, table, plan_id):
    conn.execute("DELETE FROM {} WHERE id=?".format(table), (plan_id,))
    conn.commit()


# --------------------------------------------------------------------------- #
# create_uniform_plan
# --------------------------------------------------------------------------- #
def test_create_uniform_plan_writes_cents_and_projects_schedule(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    with conn:
        res = plans.create_uniform_plan(
            conn, emp, monthly=250, term=4, start_year=year, start_month=month,
            sku='SKU1', description='Boots', notes='n', actor='pytest')

    assert res['total'] == 1000.0 and res['monthly'] == 250.0
    row = conn.execute("SELECT * FROM uniform_deductions_cents WHERE id=?",
                       (res['plan_id'],)).fetchone()
    assert row['total_amount_cents'] == 100000
    assert row['monthly_amount_cents'] == 25000
    assert row['balance_remaining_cents'] == 100000   # a new plan owes the whole total
    assert row['term_months'] == 4
    assert (row['start_year'], row['start_month']) == (year, month)

    # The projection is the real allocation, and it sums to the total exactly.
    assert len(res['schedule']) == 4
    assert sum(i['amount'] for i in res['schedule']) == 1000.0
    assert res['schedule'][0] == {'year': year, 'month': month, 'amount': 250.0}
    _cleanup(conn, 'uniform_deductions_cents', res['plan_id'])


def test_create_uniform_plan_total_overrides_monthly_times_term(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    with conn:
        res = plans.create_uniform_plan(conn, emp, monthly=100, term=3, total=250,
                                        start_year=year, start_month=month)
    assert res['total'] == 250.0
    # Final installment absorbs the remainder rather than over-collecting.
    assert sum(i['amount'] for i in res['schedule']) == 250.0
    assert res['schedule'][-1]['amount'] == 50.0
    _cleanup(conn, 'uniform_deductions_cents', res['plan_id'])


def test_create_uniform_plan_refuses_locked_start_period(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    _lock(conn, year, month)
    try:
        with conn:
            plans.create_uniform_plan(conn, emp, monthly=100, term=2,
                                      start_year=year, start_month=month)
        raise AssertionError("a locked start period must refuse")
    except ValueError as exc:
        assert 'locked' in str(exc).lower()
    finally:
        _unlock(conn, year, month)
    # ...and nothing was written.
    assert conn.execute(
        "SELECT COUNT(*) FROM uniform_deductions_cents WHERE employee_id=? "
        "AND start_year=? AND start_month=?", (emp, year, month)).fetchone()[0] == 0


def test_create_plan_refuses_unknown_employee(conn):
    year, month = _open_period(conn)
    for call in (
        lambda: plans.create_uniform_plan(conn, 'NOPE-999', monthly=10, term=1,
                                          start_year=year, start_month=month),
        lambda: plans.create_layby_plan(
            conn, 'NOPE-999', items=[{'description': 'x', 'unit_price': 10}],
            term=1, start_year=year, start_month=month),
        lambda: plans.create_undercharge(conn, 'NOPE-999', total=10,
                                         incident_year=year, incident_month=month),
    ):
        try:
            call()
            raise AssertionError("an unknown employee must refuse")
        except ValueError as exc:
            assert 'not found' in str(exc)


def test_create_uniform_plan_rejects_nonpositive_amounts(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    for monthly, term in ((0, 3), (-5, 3), (100, 0)):
        try:
            plans.create_uniform_plan(conn, emp, monthly=monthly, term=term,
                                      start_year=year, start_month=month)
            raise AssertionError("non-positive amount/term must refuse")
        except ValueError as exc:
            assert 'greater than zero' in str(exc)


# --------------------------------------------------------------------------- #
# create_layby_plan
# --------------------------------------------------------------------------- #
def test_create_layby_plan_applies_discount_and_stores_items(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    items = [{'description': 'Jacket', 'unit_price': 1000, 'quantity': 2},
             {'description': 'Cap', 'unit_price': 200, 'quantity': 1}]
    with conn:
        res = plans.create_layby_plan(conn, emp, items=items, term=4,
                                      start_year=year, start_month=month,
                                      discount_pct=40, sale_number=' S-1 ')

    assert res['basket_total'] == 2200.0
    assert res['total'] == 1320.0          # 40% staff discount
    assert res['monthly'] == 330.0
    row = conn.execute("SELECT * FROM layby_deductions_cents WHERE id=?",
                       (res['plan_id'],)).fetchone()
    assert row['basket_total_cents'] == 220000
    assert row['total_amount_cents'] == 132000
    assert row['balance_remaining_cents'] == 132000
    assert row['sale_number'] == 'S-1'     # trimmed, like the form did
    assert row['description'] == 'Jacket, Cap'

    stored = conn.execute("SELECT description, unit_price_cents, quantity, "
                          "line_total_cents FROM layby_items_cents WHERE layby_id=? "
                          "ORDER BY id", (res['plan_id'],)).fetchall()
    assert [tuple(r) for r in stored] == [('Jacket', 100000, 2, 200000),
                                          ('Cap', 20000, 1, 20000)]
    conn.execute("DELETE FROM layby_items_cents WHERE layby_id=?", (res['plan_id'],))
    _cleanup(conn, 'layby_deductions_cents', res['plan_id'])


def test_create_layby_plan_requires_a_basket_item(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    # A description-less or zero-priced line doesn't count as an item.
    for items in ([], [{'description': '', 'unit_price': 100}],
                  [{'description': 'x', 'unit_price': 0}]):
        try:
            plans.create_layby_plan(conn, emp, items=items, term=2,
                                    start_year=year, start_month=month)
            raise AssertionError("an empty basket must refuse")
        except ValueError as exc:
            assert 'at least one item' in str(exc)


def test_create_layby_plan_rejects_negative_discount(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    try:
        plans.create_layby_plan(conn, emp, items=[{'description': 'x',
                                                   'unit_price': 100}],
                                term=2, start_year=year, start_month=month,
                                discount_pct=-1)
        raise AssertionError("a negative discount must refuse")
    except ValueError as exc:
        assert 'Discount cannot be negative' in str(exc)


# --------------------------------------------------------------------------- #
# create_undercharge
# --------------------------------------------------------------------------- #
def test_create_undercharge_split_builds_exact_schedule(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    with conn:
        res = plans.create_undercharge(
            conn, emp, total=100.01, incident_year=year, incident_month=month,
            recovery='split', split_months=3, reason='till short', actor='pytest')

    assert res['split_months'] == 3
    # Sum in cents — the guarantee is exactness in the stored unit, not in floats.
    assert sum(db.to_cents(i['amount']) for i in res['schedule']) == 10001
    items = conn.execute(
        "SELECT amount_cents FROM undercharge_schedule_items WHERE undercharge_id=? "
        "AND state='scheduled' ORDER BY sequence", (res['plan_id'],)).fetchall()
    assert [r['amount_cents'] for r in items] == [3333, 3333, 3335]
    rev = conn.execute(
        "SELECT actor, reason, installment_count FROM undercharge_schedule_revisions "
        "WHERE undercharge_id=?", (res['plan_id'],)).fetchone()
    assert rev['actor'] == 'pytest'          # the actor reaches the audit row
    assert rev['installment_count'] == 3

    conn.execute("DELETE FROM undercharge_schedule_items WHERE undercharge_id=?",
                 (res['plan_id'],))
    conn.execute("DELETE FROM undercharge_schedule_revisions WHERE undercharge_id=?",
                 (res['plan_id'],))
    _cleanup(conn, 'undercharges_cents', res['plan_id'])


def test_create_overcharge_is_forced_full_and_unscheduled(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    with conn:
        res = plans.create_undercharge(
            conn, emp, total=75, incident_year=year, incident_month=month,
            recovery='split', split_months=6, uc_type='overcharge')

    # An overcharge is a credit back — single month, no deduction schedule.
    assert res['recovery_method'] == 'full' and res['split_months'] == 1
    assert res['schedule'] == []
    assert (res['start_year'], res['start_month']) == (year, month)
    assert conn.execute(
        "SELECT COUNT(*) FROM undercharge_schedule_items WHERE undercharge_id=?",
        (res['plan_id'],)).fetchone()[0] == 0
    _cleanup(conn, 'undercharges_cents', res['plan_id'])


def test_create_undercharge_refuses_locked_recovery_month(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    _lock(conn, year, month)
    try:
        with conn:
            plans.create_undercharge(conn, emp, total=50, incident_year=year,
                                     incident_month=month)
        raise AssertionError("a locked recovery month must refuse")
    except ValueError as exc:
        assert 'locked' in str(exc).lower()
    finally:
        _unlock(conn, year, month)


def test_create_undercharge_rejects_bad_inputs(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    try:
        plans.create_undercharge(conn, emp, total=0, incident_year=year,
                                 incident_month=month)
        raise AssertionError("zero amount must refuse")
    except ValueError as exc:
        assert 'greater than zero' in str(exc)
    try:
        plans.create_undercharge(conn, emp, total=10, incident_year=year,
                                 incident_month=13)
        raise AssertionError("month 13 must refuse")
    except ValueError as exc:
        assert 'valid' in str(exc)


# --------------------------------------------------------------------------- #
# reschedule_undercharge
# --------------------------------------------------------------------------- #
def test_reschedule_undercharge_requires_a_reason_and_replaces_schedule(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    with conn:
        created = plans.create_undercharge(
            conn, emp, total=300, incident_year=year, incident_month=month,
            recovery='split', split_months=3)
    uc_id = created['plan_id']

    try:
        plans.reschedule_undercharge(conn, uc_id, start_year=year,
                                     start_month=month, months=6, reason='  ')
        raise AssertionError("a blank reason must refuse")
    except ValueError as exc:
        assert 'why' in str(exc)

    with conn:
        res = plans.reschedule_undercharge(
            conn, uc_id, start_year=year, start_month=month, months=6,
            reason='agreed with employee', actor='pytest')

    assert res['months'] == 6 and res['rescheduled'] == 300.0
    assert sum(i['amount'] for i in res['schedule']) == 300.0
    # The original 3-month schedule is superseded, not deleted.
    states = dict(conn.execute(
        "SELECT state, COUNT(*) FROM undercharge_schedule_items "
        "WHERE undercharge_id=? GROUP BY state", (uc_id,)).fetchall())
    assert states.get('superseded') == 3
    assert states.get('scheduled') == 6

    conn.execute("DELETE FROM undercharge_schedule_items WHERE undercharge_id=?", (uc_id,))
    conn.execute("DELETE FROM undercharge_schedule_revisions WHERE undercharge_id=?", (uc_id,))
    _cleanup(conn, 'undercharges_cents', uc_id)


# --------------------------------------------------------------------------- #
# adjust_plan
# --------------------------------------------------------------------------- #
def test_adjust_uniform_plan_recomputes_monthly_and_audits_actor(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    with conn:
        created = plans.create_uniform_plan(conn, emp, monthly=250, term=4,
                                            start_year=year, start_month=month)
    plan_id = created['plan_id']

    with conn:
        res = plans.adjust_plan(conn, 'uniform', plan_id, amount=200,
                               note='cash paid', actor='mcp:claude')

    assert res['new_balance'] == 800.0
    assert res['new_monthly'] == 200.0        # 800 over the 4 remaining months
    assert res['status'] == 'active'
    row = conn.execute("SELECT balance_remaining_cents, monthly_amount_cents, status "
                       "FROM uniform_deductions_cents WHERE id=?", (plan_id,)).fetchone()
    assert (row['balance_remaining_cents'], row['monthly_amount_cents']) == (80000, 20000)

    adj = conn.execute("SELECT * FROM plan_adjustments_cents WHERE plan_type='uniform' "
                       "AND plan_id=? ORDER BY id DESC LIMIT 1", (plan_id,)).fetchone()
    assert adj['amount_cents'] == 20000
    assert adj['note'] == 'cash paid'
    assert adj['actor'] == 'mcp:claude'       # migration 0040 — provenance is recorded

    conn.execute("DELETE FROM plan_adjustments_cents WHERE plan_id=? AND plan_type='uniform'",
                 (plan_id,))
    _cleanup(conn, 'uniform_deductions_cents', plan_id)


def test_adjust_plan_settling_the_balance_completes_the_plan(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    with conn:
        created = plans.create_uniform_plan(conn, emp, monthly=100, term=2,
                                            start_year=year, start_month=month)
    with conn:
        res = plans.adjust_plan(conn, 'uniform', created['plan_id'], amount=500)
    assert res['new_balance'] == 0.0 and res['new_monthly'] == 0
    assert res['status'] == 'complete'        # over-payment clamps at zero
    conn.execute("DELETE FROM plan_adjustments_cents WHERE plan_id=? AND plan_type='uniform'",
                 (created['plan_id'],))
    _cleanup(conn, 'uniform_deductions_cents', created['plan_id'])


def test_adjust_plan_new_term_resets_remaining_months(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    with conn:
        created = plans.create_uniform_plan(conn, emp, monthly=300, term=4,
                                            start_year=year, start_month=month)
    with conn:
        res = plans.adjust_plan(conn, 'uniform', created['plan_id'], amount=0,
                               new_term=2)
    assert res['new_balance'] == 1200.0
    assert res['new_monthly'] == 600.0        # 1200 over the 2 new months
    assert res['term_months'] == 2            # payments_made(0) + new_term(2)
    conn.execute("DELETE FROM plan_adjustments_cents WHERE plan_id=? AND plan_type='uniform'",
                 (created['plan_id'],))
    _cleanup(conn, 'uniform_deductions_cents', created['plan_id'])


def test_adjust_keeps_each_plan_types_legacy_balance_fallback(conn):
    """A NULL balance_remaining is derived DIFFERENTLY per plan type, on purpose.

    uniform: total - payments_made * monthly.   layby: the full total.
    These only fire on legacy rows, and unifying them would restate real balances —
    so the divergence is pinned rather than fixed.
    """
    emp = _emp(conn)
    year, month = _open_period(conn)

    uni = conn.execute(
        "INSERT INTO uniform_deductions_cents (employee_id, total_amount_cents, "
        "monthly_amount_cents, balance_remaining_cents, term_months, start_month, "
        "start_year, payments_made, status) VALUES (?,?,?,NULL,?,?,?,?,'active')",
        (emp, 100000, 25000, 4, month, year, 1)).lastrowid
    lay = conn.execute(
        "INSERT INTO layby_deductions_cents (employee_id, total_amount_cents, "
        "monthly_amount_cents, balance_remaining_cents, term_months, start_month, "
        "start_year, payments_made, status) VALUES (?,?,?,NULL,?,?,?,?,'active')",
        (emp, 100000, 25000, 4, month, year, 1)).lastrowid
    conn.commit()

    with conn:
        u = plans.adjust_plan(conn, 'uniform', uni, amount=0)
        l = plans.adjust_plan(conn, 'layby', lay, amount=0)

    assert u['new_balance'] == 750.0    # 1000 - 1 * 250 paid
    assert l['new_balance'] == 1000.0   # falls back to the whole total

    for pt, pid, table in (('uniform', uni, 'uniform_deductions_cents'),
                           ('layby', lay, 'layby_deductions_cents')):
        conn.execute("DELETE FROM plan_adjustments_cents WHERE plan_type=? AND plan_id=?",
                     (pt, pid))
        _cleanup(conn, table, pid)


def test_adjust_plan_rejects_undercharges_and_unknown_plans(conn):
    try:
        plans.adjust_plan(conn, 'undercharge', 1, amount=10)
        raise AssertionError("undercharges have no adjust path")
    except ValueError as exc:
        assert 'uniform' in str(exc)
    try:
        plans.adjust_plan(conn, 'uniform', 99999999, amount=10)
        raise AssertionError("a missing plan must refuse")
    except ValueError as exc:
        assert 'not found' in str(exc)


# --------------------------------------------------------------------------- #
# write_off_plan
# --------------------------------------------------------------------------- #
def test_write_off_uniform_plan_flips_status_and_is_idempotent(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    with conn:
        created = plans.create_uniform_plan(conn, emp, monthly=100, term=3,
                                            start_year=year, start_month=month)
    with conn:
        res = plans.write_off_plan(conn, 'uniform', created['plan_id'],
                                   reason='goodwill', actor='mcp:claude')
    assert res['status'] == 'written_off' and res['written_off'] == 300.0
    assert conn.execute("SELECT status FROM uniform_deductions_cents WHERE id=?",
                        (created['plan_id'],)).fetchone()['status'] == 'written_off'

    # Writing it off twice is a no-op, not a double event.
    with conn:
        again = plans.write_off_plan(conn, 'uniform', created['plan_id'])
    assert again['written_off'] == 0 and 'already' in again['note']
    _cleanup(conn, 'uniform_deductions_cents', created['plan_id'])


def test_write_off_undercharge_records_event_and_cancels_schedule(conn):
    emp = _emp(conn)
    year, month = _open_period(conn)
    with conn:
        created = plans.create_undercharge(
            conn, emp, total=450, incident_year=year, incident_month=month,
            recovery='split', split_months=3)
    uc_id = created['plan_id']

    with conn:
        res = plans.write_off_plan(conn, 'undercharge', uc_id,
                                   reason='unrecoverable', actor='mcp:claude')

    assert res['written_off'] == 450.0
    ev = conn.execute("SELECT event_type, amount_cents, actor, note FROM "
                      "undercharge_events WHERE undercharge_id=?", (uc_id,)).fetchone()
    assert ev['event_type'] == 'write_off' and ev['amount_cents'] == 45000
    assert ev['actor'] == 'mcp:claude' and ev['note'] == 'unrecoverable'
    # No installment is left waiting to be deducted.
    assert conn.execute(
        "SELECT COUNT(*) FROM undercharge_schedule_items WHERE undercharge_id=? "
        "AND state='scheduled'", (uc_id,)).fetchone()[0] == 0

    conn.execute("DELETE FROM undercharge_events WHERE undercharge_id=?", (uc_id,))
    conn.execute("DELETE FROM undercharge_schedule_items WHERE undercharge_id=?", (uc_id,))
    conn.execute("DELETE FROM undercharge_schedule_revisions WHERE undercharge_id=?", (uc_id,))
    _cleanup(conn, 'undercharges_cents', uc_id)


# --------------------------------------------------------------------------- #
# route <-> service parity — the whole point of the extraction
# --------------------------------------------------------------------------- #
def test_route_and_service_create_identical_uniform_rows(client, conn):
    """POST /uniform/add and plans.create_uniform_plan must produce the same row.

    This is the regression guard for the class of bug that started all this: the MCP
    connector holding its own copy of a route's write logic and drifting from it.
    """
    emp = _emp(conn)
    year, month = _open_period(conn)

    r = client.post('/uniform/add', data={
        'employee_id': emp, 'monthly_amount': '175.50', 'term_months': '3',
        'start_month': str(month), 'start_year': str(year),
        'sku': 'PARITY', 'description': 'Fleece', 'sale_number': 'SN-9',
        'notes': 'via route'})
    assert r.status_code in (302, 303)
    via_route = conn.execute(
        "SELECT * FROM uniform_deductions_cents WHERE employee_id=? AND sku='PARITY' "
        "ORDER BY id DESC LIMIT 1", (emp,)).fetchone()
    assert via_route is not None, "the route did not create a plan"

    with conn:
        res = plans.create_uniform_plan(
            conn, emp, monthly='175.50', term='3', start_year=year,
            start_month=month, sku='PARITY', description='Fleece',
            sale_number='SN-9', notes='via service')
    via_service = conn.execute("SELECT * FROM uniform_deductions_cents WHERE id=?",
                               (res['plan_id'],)).fetchone()

    compared = ('employee_id', 'sku', 'description', 'sale_number',
                'total_amount_cents', 'monthly_amount_cents',
                'balance_remaining_cents', 'term_months', 'start_month',
                'start_year', 'status')
    assert {k: via_route[k] for k in compared} == {k: via_service[k] for k in compared}

    _cleanup(conn, 'uniform_deductions_cents', via_route['id'])
    _cleanup(conn, 'uniform_deductions_cents', via_service['id'])


def test_route_rejects_locked_period_through_the_service(client, conn):
    """The lock guard still fires when it is reached through the web route."""
    emp = _emp(conn)
    year, month = _open_period(conn)
    _lock(conn, year, month)
    try:
        r = client.post('/uniform/add', data={
            'employee_id': emp, 'monthly_amount': '100', 'term_months': '2',
            'start_month': str(month), 'start_year': str(year), 'sku': 'LOCKED'},
            follow_redirects=True)
        assert b'locked' in r.data.lower()
    finally:
        _unlock(conn, year, month)
    assert conn.execute("SELECT COUNT(*) FROM uniform_deductions_cents "
                        "WHERE sku='LOCKED'").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# the CC coding read fix (regression guard)
# --------------------------------------------------------------------------- #
def test_lines_missing_coding_agrees_with_the_card_tile_counts(conn):
    """get_cc_lines_missing_coding must never disagree with list_cc_cards.

    The tool it backs used to query `coding_dirty` (the AI suggestion queue) while
    claiming to list lines with no account code — so it returned nothing while the
    app's own tiles showed dozens outstanding.
    """
    for card in db.list_cc_cards():
        scoped = db.get_cc_lines_missing_coding(card_id=card['id'], limit=10000)
        assert len(scoped) == card['coding_missing'], (
            "card {} tile says {} lines need coding, helper returned {}".format(
                card['id'], card['coding_missing'], len(scoped)))
        assert all(r['card_id'] == card['id'] for r in scoped)
        assert all(r['xero_account_code'] is None if 'xero_account_code' in r.keys()
                   else True for r in scoped)


# --------------------------------------------------------------------------- #
# the preview dry-run mechanism (a caller previews a write, then rolls it back)
# --------------------------------------------------------------------------- #
def test_dry_run_pattern_leaves_no_trace(conn):
    """preview_plan runs the REAL service write, then rolls it back.

    That's what makes a preview impossible to drift from its apply — but it only holds
    if BEGIN/rollback genuinely works on a database.get_db() connection. The MCP tool
    module can't be imported on 3.9, so the pattern itself is pinned here.
    """
    emp = _emp(conn)
    year, month = _open_period(conn)

    before = conn.execute("SELECT COUNT(*) FROM uniform_deductions_cents").fetchone()[0]
    adj_before = conn.execute("SELECT COUNT(*) FROM plan_adjustments_cents").fetchone()[0]

    probe = db.get_db()
    try:
        probe.execute("BEGIN")
        result = plans.create_uniform_plan(probe, emp, monthly=999, term=5,
                                          start_year=year, start_month=month,
                                          sku='DRYRUN')
        # Inside the transaction the row exists and the projection is real...
        assert probe.execute("SELECT COUNT(*) FROM uniform_deductions_cents "
                             "WHERE sku='DRYRUN'").fetchone()[0] == 1
        assert len(result['schedule']) == 5
        probe.rollback()
    finally:
        probe.close()

    # ...and afterwards there is no trace of it.
    assert conn.execute("SELECT COUNT(*) FROM uniform_deductions_cents "
                        "WHERE sku='DRYRUN'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM uniform_deductions_cents").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM plan_adjustments_cents").fetchone()[0] == adj_before


def test_dry_run_rolls_back_undercharge_schedule_rows_too(conn):
    """The undercharge path writes to three tables (plan + revision + items) — the
    rollback has to take all of them, or a preview would leave orphan schedule rows."""
    emp = _emp(conn)
    year, month = _open_period(conn)
    counts = lambda: tuple(
        conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
        for t in ("undercharges_cents", "undercharge_schedule_revisions",
                  "undercharge_schedule_items"))
    before = counts()

    probe = db.get_db()
    try:
        probe.execute("BEGIN")
        res = plans.create_undercharge(probe, emp, total=250,
                                       incident_year=year, incident_month=month,
                                       recovery='split', split_months=5)
        assert len(res['schedule']) == 5
        probe.rollback()
    finally:
        probe.close()

    assert counts() == before


# --------------------------------------------------------------------------- #
# edit_plan — restating an existing plan's stored figures
# --------------------------------------------------------------------------- #
def _uniform(conn, **kw):
    """A throwaway uniform plan in an open period; returns its result dict."""
    year, month = _open_period(conn)
    opts = dict(monthly=250, term=4, start_year=year, start_month=month,
                sku='SKU', description='Boots', sale_number='SN', notes='n')
    opts.update(kw)
    with conn:
        return plans.create_uniform_plan(conn, _emp(conn), actor='pytest', **opts)


def test_edit_plan_changes_only_the_fields_given(conn):
    plan = _uniform(conn)
    with conn:
        res = plans.edit_plan(conn, 'uniform', plan['plan_id'],
                              actor='pytest', monthly=300, term_months=5)

    row = conn.execute("SELECT * FROM uniform_deductions_cents WHERE id=?",
                       (plan['plan_id'],)).fetchone()
    assert row['monthly_amount_cents'] == 30000
    assert row['term_months'] == 5
    # untouched fields keep their values rather than being blanked
    assert row['sku'] == 'SKU' and row['description'] == 'Boots'
    assert row['sale_number'] == 'SN' and row['notes'] == 'n'
    assert row['total_amount_cents'] == 100000
    assert (row['start_year'], row['start_month']) == (plan['start_year'],
                                                       plan['start_month'])
    assert res['changes'] == {'monthly': {'from': 250.0, 'to': 300.0},
                              'term_months': {'from': 4, 'to': 5}}
    _cleanup(conn, 'uniform_deductions_cents', plan['plan_id'])


def test_edit_plan_records_an_audited_adjustment_row(conn):
    plan = _uniform(conn)
    with conn:
        plans.edit_plan(conn, 'uniform', plan['plan_id'], actor='mcp:claude',
                        balance_remaining=400)

    audit = conn.execute(
        "SELECT * FROM plan_adjustments_cents WHERE plan_type='uniform' AND plan_id=? "
        "ORDER BY id DESC LIMIT 1", (plan['plan_id'],)).fetchone()
    assert audit is not None, "an edit must leave an audit trail"
    assert audit['actor'] == 'mcp:claude'
    assert audit['amount_cents'] == 60000        # 1000.00 owed -> 400.00 owed
    assert 'balance_remaining' in audit['note']
    _cleanup(conn, 'uniform_deductions_cents', plan['plan_id'])


def test_edit_plan_derives_status_from_payments_and_balance(conn):
    plan = _uniform(conn)
    with conn:
        res = plans.edit_plan(conn, 'uniform', plan['plan_id'], actor='pytest',
                              payments_made=4)
    assert res['status'] == 'complete'           # payments cover the term

    with conn:
        res = plans.edit_plan(conn, 'uniform', plan['plan_id'], actor='pytest',
                              payments_made=1)
    assert res['status'] == 'active'             # and back again
    _cleanup(conn, 'uniform_deductions_cents', plan['plan_id'])


def test_edit_plan_does_not_resurrect_a_written_off_plan(conn):
    """Editing a written-off plan must not make the employee owe it again."""
    plan = _uniform(conn)
    with conn:
        plans.write_off_plan(conn, 'uniform', plan['plan_id'], actor='pytest')
    with conn:
        res = plans.edit_plan(conn, 'uniform', plan['plan_id'], actor='pytest',
                              balance_remaining=900, notes='corrected')

    assert res['status'] == 'written_off'
    row = conn.execute("SELECT status FROM uniform_deductions_cents WHERE id=?",
                       (plan['plan_id'],)).fetchone()
    assert row['status'] == 'written_off'
    _cleanup(conn, 'uniform_deductions_cents', plan['plan_id'])


def test_edit_plan_refuses_to_move_a_plan_into_a_locked_period(conn):
    plan = _uniform(conn)
    year, month = _open_period(conn)
    target_year, target_month = (year + 1), month
    _lock(conn, target_year, target_month)
    try:
        try:
            with conn:
                plans.edit_plan(conn, 'uniform', plan['plan_id'], actor='pytest',
                                start_year=target_year, start_month=target_month)
            raise AssertionError("moving into a locked period should be refused")
        except ValueError as exc:
            assert 'locked' in str(exc).lower()
    finally:
        _unlock(conn, target_year, target_month)

    row = conn.execute("SELECT start_year, start_month FROM uniform_deductions_cents "
                       "WHERE id=?", (plan['plan_id'],)).fetchone()
    assert (row['start_year'], row['start_month']) == (plan['start_year'],
                                                       plan['start_month'])
    _cleanup(conn, 'uniform_deductions_cents', plan['plan_id'])


def test_edit_plan_allows_other_edits_on_a_plan_already_in_a_locked_period(conn):
    """The lock guard is about MOVING a plan, not about freezing its other fields."""
    plan = _uniform(conn)
    _lock(conn, plan['start_year'], plan['start_month'])
    try:
        with conn:
            res = plans.edit_plan(conn, 'uniform', plan['plan_id'], actor='pytest',
                                  notes='typo fixed')
        assert res['changes'] == {'notes': {'from': 'n', 'to': 'typo fixed'}}
    finally:
        _unlock(conn, plan['start_year'], plan['start_month'])
    _cleanup(conn, 'uniform_deductions_cents', plan['plan_id'])


def test_edit_plan_rejects_unknown_fields_and_bad_numbers(conn):
    plan = _uniform(conn)
    for kwargs, expect in (
        ({'discount_pct': 10}, 'not editable'),      # lay-by field on a uniform plan
        ({'nonsense': 1}, 'not editable'),
        ({'monthly': 0}, 'greater than zero'),
        ({'term_months': 0}, 'greater than zero'),
        ({'payments_made': -1}, 'cannot be negative'),
        ({'monthly': 'abc'}, 'must be a number'),
    ):
        try:
            with conn:
                plans.edit_plan(conn, 'uniform', plan['plan_id'], actor='pytest',
                                **kwargs)
            raise AssertionError("{} should be refused".format(kwargs))
        except ValueError as exc:
            assert expect in str(exc).lower(), (kwargs, str(exc))

    try:
        with conn:
            plans.edit_plan(conn, 'undercharge', 1, actor='pytest', notes='x')
        raise AssertionError("undercharges have no edit path")
    except ValueError as exc:
        assert 'uniform' in str(exc)
    _cleanup(conn, 'uniform_deductions_cents', plan['plan_id'])


def test_edit_layby_only_touches_the_basket_when_the_basket_is_edited(conn):
    """A 3-item basket survives an unrelated edit, and collapses correctly when the
    basket itself is restated — the old route overwrote EVERY item row with the full
    basket total, leaving a 3-item lay-by showing 3x its own value."""
    year, month = _open_period(conn)
    with conn:
        plan = plans.create_layby_plan(
            conn, _emp(conn),
            items=[{'description': 'Tee', 'unit_price': 300, 'quantity': 1},
                   {'description': 'Cap', 'unit_price': 200, 'quantity': 1},
                   {'description': 'Bag', 'unit_price': 500, 'quantity': 1}],
            term=2, start_year=year, start_month=month, actor='pytest')
    pid = plan['plan_id']

    def items():
        return conn.execute("SELECT * FROM layby_items_cents WHERE layby_id=? "
                            "ORDER BY id", (pid,)).fetchall()

    assert len(items()) == 3
    with conn:
        plans.edit_plan(conn, 'layby', pid, actor='pytest', notes='unrelated')
    assert len(items()) == 3, "an unrelated edit must not rewrite the basket"
    assert sum(i['line_total_cents'] for i in items()) == 100000

    with conn:
        plans.edit_plan(conn, 'layby', pid, actor='pytest', basket_total=1200,
                        description='Restated basket')
    rows_after = items()
    assert len(rows_after) == 1
    assert rows_after[0]['line_total_cents'] == 120000
    assert rows_after[0]['unit_price_cents'] == 120000
    assert rows_after[0]['description'] == 'Restated basket'
    _cleanup(conn, 'layby_deductions_cents', pid)


def test_edit_route_and_service_produce_identical_uniform_rows(client, conn):
    """POST /uniform/<id>/edit and plans.edit_plan must agree, field for field."""
    a, b = _uniform(conn, sku='EDITA'), _uniform(conn, sku='EDITB')
    form = {'monthly_amount': '199.99', 'term_months': '6', 'total_amount': '1199.94',
            'payments_made': '2', 'balance_remaining': '799.96',
            'start_month': str(a['start_month']), 'start_year': str(a['start_year']),
            'sku': 'EDITED', 'sale_number': 'SN-2', 'description': 'Jacket',
            'notes': 'via route'}
    r = client.post('/uniform/{}/edit'.format(a['plan_id']), data=form)
    assert r.status_code in (302, 303)

    with conn:
        plans.edit_plan(conn, 'uniform', b['plan_id'], actor='pytest',
                        monthly='199.99', term_months='6', total='1199.94',
                        payments_made='2', balance_remaining='799.96',
                        start_month=a['start_month'], start_year=a['start_year'],
                        sku='EDITED', sale_number='SN-2', description='Jacket',
                        notes='via route')

    compared = ('sku', 'description', 'sale_number', 'total_amount_cents',
                'monthly_amount_cents', 'balance_remaining_cents', 'term_months',
                'payments_made', 'start_month', 'start_year', 'status', 'notes')
    via_route = conn.execute("SELECT * FROM uniform_deductions_cents WHERE id=?",
                             (a['plan_id'],)).fetchone()
    via_service = conn.execute("SELECT * FROM uniform_deductions_cents WHERE id=?",
                               (b['plan_id'],)).fetchone()
    assert {k: via_route[k] for k in compared} == {k: via_service[k] for k in compared}
    assert via_route['monthly_amount_cents'] == 19999
    _cleanup(conn, 'uniform_deductions_cents', a['plan_id'])
    _cleanup(conn, 'uniform_deductions_cents', b['plan_id'])


def test_edit_layby_route_still_updates_the_plan(client, conn):
    year, month = _open_period(conn)
    with conn:
        plan = plans.create_layby_plan(
            conn, _emp(conn),
            items=[{'description': 'Tee', 'unit_price': 500, 'quantity': 1}],
            term=2, start_year=year, start_month=month, actor='pytest')

    r = client.post('/layby/{}/edit'.format(plan['plan_id']), data={
        'description': 'Tee (restated)', 'sale_number': 'SN-L', 'basket_total': '600',
        'discount_pct': '40', 'total_amount': '360', 'monthly_amount': '180',
        'balance_remaining': '360', 'term_months': '2', 'payments_made': '0',
        'start_month': str(month), 'start_year': str(year), 'notes': 'edited'})
    assert r.status_code in (302, 303)

    row = conn.execute("SELECT * FROM layby_deductions_cents WHERE id=?",
                       (plan['plan_id'],)).fetchone()
    assert row['description'] == 'Tee (restated)'
    assert row['basket_total_cents'] == 60000
    assert row['total_amount_cents'] == 36000
    assert row['monthly_amount_cents'] == 18000
    assert row['status'] == 'active'
    _cleanup(conn, 'layby_deductions_cents', plan['plan_id'])
