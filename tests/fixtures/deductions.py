"""Uniform, lay-by and undercharge plans, and the payroll ledger behind them.

Money is written to the ``*_cents`` base tables as integer cents; the Rands
views on top are read-only. Where the app owns a multi-table invariant — the
undercharge schedule/ledger triple, a write-off's event row, an adjustment's
audit row — the seed calls the app's own function instead of hand-writing the
rows, so the synthetic database satisfies tools/db_check.py by construction
rather than by luck.
"""
import math

from northwind.data import database as db
from northwind.deductions import plans
from northwind.deductions import requests as reqs

from . import names
from .calendar_math import shift

# Locked payroll periods sit in a fixed, long-past year that nothing else in the
# seed or the test suite writes to. Locks are refusals, so a lock that drifted
# into a period a test uses would fail that test rather than the lock.
LOCKED = {
    'retail': [(2020, m) for m in range(1, 7)],
    'hq': [(2020, m) for m in range(1, 4)],
}

# The oldest month any plan starts in; the payroll sweep replays from here.
HISTORY_START = -14


def seed_locked_periods(conn):
    for sector, periods in LOCKED.items():
        for year, month in periods:
            conn.execute(
                "INSERT OR IGNORE INTO locked_periods (sector, year, month) "
                "VALUES (?,?,?)", (sector, year, month))
    return sum(len(v) for v in LOCKED.values())


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #
def seed_uniform_plans(conn, profile, people):
    """Uniform plans spread across the history window.

    Start months run from HISTORY_START to nearly a year ahead, so once the
    payroll sweep has replayed the past the table naturally holds completed,
    part-paid and not-yet-started plans without any of that being hardcoded.
    """
    roster = people['retail'] + people['hq']
    made = []
    for j in range(profile['uniform_plans']):
        emp = roster[j % len(roster)]
        sku, description = names.UNIFORM_ITEMS[j % len(names.UNIFORM_ITEMS)]
        term = 3 + (j % 10)
        monthly_cents = 15000 + (j % 12) * 2500
        total_cents = monthly_cents * term
        if j % 7 == 3:
            # An entered total that is not a clean multiple of the monthly
            # amount: the last instalment absorbs the difference.
            total_cents -= 3750
        start_year, start_month = shift(HISTORY_START + (j * 5) % 25)
        cur = conn.execute(
            "INSERT INTO uniform_deductions_cents "
            "(employee_id, sku, description, sale_number, total_amount_cents, "
            " monthly_amount_cents, term_months, start_month, start_year, "
            " payments_made, status, notes, balance_remaining_cents) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,'active',?,?)",
            (emp['id'], sku, description, 'SO-%05d' % (4000 + j), total_cents,
             monthly_cents, term, start_month, start_year,
             'Seeded uniform plan', total_cents))
        made.append({'id': cur.lastrowid, 'employee_id': emp['id']})
    return made


def seed_arrears_uniform_plans(conn, arrears):
    """Long plans that payroll never collected — the "overdue instalment" case.

    Each runs from well before the anchor month to well after it, so there are
    several months of arrears AND several months still upcoming. Nothing sits on
    a month boundary, so the portal's now()-relative split is stable whichever
    day the suite runs.
    """
    made = []
    for index, emp in enumerate(arrears):
        # Cycled rather than scaled with the index, so a bigger profile adds
        # more arrears cases instead of shrinking the term towards zero.
        step = index % 6
        term = 20 - step * 2
        monthly_cents = 30000 + step * 5000
        start_year, start_month = shift(-8 - index % 4)
        cur = conn.execute(
            "INSERT INTO uniform_deductions_cents "
            "(employee_id, sku, description, sale_number, total_amount_cents, "
            " monthly_amount_cents, term_months, start_month, start_year, "
            " payments_made, status, notes, balance_remaining_cents) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,'active',?,?)",
            (emp['id'], 'UNI-1009', 'Store kit bundle',
             'SO-%05d' % (7100 + index), monthly_cents * term, monthly_cents,
             term, start_month, start_year,
             'Never picked up by payroll', monthly_cents * term))
        made.append({'id': cur.lastrowid, 'employee_id': emp['id']})
    return made


def seed_layby_plans(conn, profile, people):
    roster = people['retail'] + people['hq']
    made = []
    for j in range(profile['layby_plans']):
        emp = roster[(j * 3 + 1) % len(roster)]
        term = 2 + (j % 6)
        discount_pct = 40.0 if j % 4 else 30.0
        basket = names.LAYBY_ITEMS[j % len(names.LAYBY_ITEMS):][:1] + \
            names.LAYBY_ITEMS[(j * 2 + 1) % len(names.LAYBY_ITEMS):][:1]
        basket_cents = sum(price for _sku, _desc, price in basket)
        total_cents = int(round(basket_cents * (100.0 - discount_pct) / 100.0))
        monthly_cents = int(math.ceil(total_cents / float(term)))
        start_year, start_month = shift(HISTORY_START + 2 + (j * 7) % 22)
        cur = conn.execute(
            "INSERT INTO layby_deductions_cents "
            "(employee_id, description, total_amount_cents, monthly_amount_cents, "
            " term_months, start_month, start_year, payments_made, status, notes, "
            " sale_number, basket_total_cents, discount_pct, balance_remaining_cents) "
            "VALUES (?,?,?,?,?,?,?,0,'active',?,?,?,?,?)",
            (emp['id'], ', '.join(d for _s, d, _p in basket), total_cents,
             monthly_cents, term, start_month, start_year,
             'Seeded lay-by', 'LB-%05d' % (5000 + j), basket_cents,
             discount_pct, total_cents))
        layby_id = cur.lastrowid
        for sku, description, price in basket:
            conn.execute(
                "INSERT INTO layby_items_cents "
                "(layby_id, description, unit_price_cents, quantity, line_total_cents) "
                "VALUES (?,?,?,1,?)",
                (layby_id, '%s (%s)' % (description, sku), price, price))
        made.append({'id': layby_id, 'employee_id': emp['id']})
    return made


def seed_undercharges(conn, profile, people):
    """Undercharges with a real versioned schedule, plus a few overcharges.

    ``create_undercharge_schedule`` is the app's own writer, so the revision and
    its instalment rows are exactly what the app would have produced — which is
    what lets the payroll sweep below link ledger rows to schedule items and
    keeps the timeline integrity check clean.
    """
    roster = people['retail']
    made = []
    for j in range(profile['undercharges']):
        emp = roster[(j * 2 + 3) % len(roster)]
        total_cents = 8000 + (j % 15) * 4300
        if j % 3 == 0:
            method, split = 'full', 1
        else:
            method, split = 'split', 2 + (j % 4)
        year, month = shift(HISTORY_START + 2 + (j * 5) % 20)
        cur = conn.execute(
            "INSERT INTO undercharges_cents "
            "(employee_id, sale_number, total_amount_cents, reason, incident_month, "
            " incident_year, recovery_method, split_months, payments_made, status, "
            " notes, type, start_month, start_year) "
            "VALUES (?,?,?,?,?,?,?,?,0,'pending',?, 'undercharge',?,?)",
            (emp['id'], 'SO-%05d' % (6000 + j), total_cents,
             names.UNDERCHARGE_REASONS[j % len(names.UNDERCHARGE_REASONS)],
             month, year, method, split, 'Seeded undercharge', month, year))
        uc_id = cur.lastrowid
        db.create_undercharge_schedule(
            conn, uc_id, year, month, split, total_cents,
            reason='Original recovery schedule', actor='seed')
        made.append({'id': uc_id, 'employee_id': emp['id']})

    overcharges = []
    for j in range(profile['overcharges']):
        emp = roster[(j * 11 + 5) % len(roster)]
        year, month = shift(-6 - j)
        cur = conn.execute(
            "INSERT INTO undercharges_cents "
            "(employee_id, sale_number, total_amount_cents, reason, incident_month, "
            " incident_year, recovery_method, split_months, payments_made, status, "
            " notes, type, start_month, start_year) "
            "VALUES (?,?,?,?,?,?, 'full', 1, 0, 'pending', ?, 'overcharge', ?, ?)",
            (emp['id'], 'SO-%05d' % (6500 + j), 12500 + j * 3300,
             names.OVERCHARGE_REASONS[j % len(names.OVERCHARGE_REASONS)],
             month, year, 'Customer overcharged, refund owed', month, year))
        overcharges.append({'id': cur.lastrowid, 'employee_id': emp['id']})
    return made, overcharges


def seed_overpayments(conn, profile, people):
    """Walk-in overpayments: some tied to staff, some to a customer name only."""
    made = 0
    for j in range(profile['overpayments']):
        year, month = shift(-4 - j)
        if j % 2:
            emp = people['retail'][(j * 13) % len(people['retail'])]
            employee_id, individual, store = emp['id'], None, emp['store']
        else:
            employee_id = None
            individual = names.person(400 + j)
            store = people['retail'][(j * 7) % len(people['retail'])]['store']
        cents = 15000 + j * 6600
        conn.execute(
            "INSERT INTO overpayments_cents "
            "(employee_id, store, individual_name, sale_number, total_amount_cents, "
            " reason, incident_month, incident_year, status, balance_remaining_cents, "
            " notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (employee_id, store, individual, 'SO-%05d' % (6800 + j), cents,
             'Customer overpaid at the till', month, year,
             'pending' if j % 3 else 'resolved',
             cents if j % 3 else 0, 'Seeded overpayment'))
        made += 1
    return made


# --------------------------------------------------------------------------- #
# Payroll: replay the past through the app's own tick functions
# --------------------------------------------------------------------------- #
def run_payroll(conn, people, skip_ids):
    """Tick every month from HISTORY_START up to (but not including) the anchor.

    Going through ``tick_*_due`` rather than inserting ledger rows by hand is
    what makes the ledger self-consistent: the tick is what moves
    ``payments_made``, recomputes the stored balance, and — for undercharges —
    links the new transaction back to its schedule item, which db_check asserts.

    `skip_ids` is the arrears cohort: their instalments fall due and are never
    collected, which is the state the staff portal has to flag.
    """
    everyone = [e for e in people['retail'] + people['hq'] + people['terminated']
                if e['id'] not in skip_ids]
    ticked = 0
    for offset in range(HISTORY_START, 0):
        year, month = shift(offset)
        for emp in everyone:
            ticked += db.tick_uniform_due(conn, emp['id'], year, month)
            ticked += db.tick_layby_due(conn, emp['id'], year, month)
            ticked += db.tick_undercharges_due(conn, emp['id'], year, month)
    return ticked


def void_a_transaction(conn):
    """One voided ledger row, so the "voided" branch of every total has data.

    Uniform is chosen deliberately: voiding an undercharge transaction without
    also unlinking its schedule item and rewinding ``payments_made`` is exactly
    the inconsistency db_check exists to catch.
    """
    row = conn.execute(
        "SELECT id, plan_id FROM deduction_transactions_cents "
        "WHERE plan_type='uniform' AND COALESCE(voided,0)=0 ORDER BY id LIMIT 1"
    ).fetchone()
    if not row:
        return 0
    conn.execute("UPDATE deduction_transactions_cents SET voided=1 WHERE id=?",
                 (row['id'],))
    return 1


def write_off_some(conn, uniform_plans, layby_plans, undercharges, every=17):
    """Write off a slice of each plan type through the service layer."""
    written = 0
    for plan_type, rows in (('uniform', uniform_plans),
                            ('layby', layby_plans),
                            ('undercharge', undercharges)):
        for index, row in enumerate(rows):
            if index == 0 or index % every:
                continue
            with conn:
                plans.write_off_plan(conn, plan_type, row['id'],
                                     reason='Written off at year end',
                                     actor='seed')
            written += 1
    return written


def adjust_some(conn, uniform_plans, layby_plans, every=23):
    """A handful of audited plan adjustments (plan_adjustments_cents)."""
    adjusted = 0
    for plan_type, rows in (('uniform', uniform_plans), ('layby', layby_plans)):
        for index, row in enumerate(rows):
            if index == 0 or index % every:
                continue
            live = conn.execute(
                "SELECT status FROM %s_deductions_cents WHERE id=?" % plan_type,
                (row['id'],)).fetchone()
            if not live or live['status'] != 'active':
                continue
            try:
                with conn:
                    plans.adjust_plan(conn, plan_type, row['id'], amount=50.0,
                                      note='Goodwill reduction', actor='seed')
                adjusted += 1
            except ValueError:
                continue
    return adjusted


# --------------------------------------------------------------------------- #
# HQ allowances
# --------------------------------------------------------------------------- #
def seed_allowances(conn, people, years):
    """Annual clothing allowances and the purchases drawn against them."""
    allocated = purchases = 0
    for index, emp in enumerate(people['hq']):
        for year_index, year in enumerate(years):
            allocated_cents = 500000 + (index % 4) * 100000
            conn.execute(
                "INSERT OR IGNORE INTO allowances "
                "(employee_id, year, allocated_cents, notes) VALUES (?,?,?,?)",
                (emp['id'], year, allocated_cents,
                 'Standard annual allowance' if year_index else None))
            allocated += 1
            for line in range((index % 3) + 1):
                sku, description, price = names.ALLOWANCE_ITEMS[
                    (index + line) % len(names.ALLOWANCE_ITEMS)]
                quantity = 1 + (line % 2)
                conn.execute(
                    "INSERT INTO allowance_purchases "
                    "(employee_id, year, purchase_date, sku, description, quantity, "
                    " unit_price_cents, line_total_cents, location, sale_number, notes) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (emp['id'], year, '%d-%02d-%02d' % (year, 2 + line * 3, 14),
                     sku, description, quantity, price, price * quantity,
                     emp['store'], 'INV-%05d' % (9000 + index * 10 + line), None))
                purchases += 1
    return allocated, purchases


# --------------------------------------------------------------------------- #
# Staff requests
# --------------------------------------------------------------------------- #
def seed_requests(conn, people, count=6):
    """A worked request queue: one in every status the workflow supports.

    Attached to the END of the roster on purpose. Tests that need "an employee
    with no requests of their own" take the first one they find, so the seeded
    requests must not consume the front of the list.
    """
    pool = [e for e in people['retail'] if e['store']][-count:]
    made = []
    for index, emp in enumerate(pool):
        kind = 'layby' if index % 2 else 'uniform'
        sku, description, price = names.LAYBY_ITEMS[index % len(names.LAYBY_ITEMS)]
        items = [{'description': description, 'sku': sku,
                  'unit_price': price / 100.0, 'quantity': 1 + index % 2}]
        with conn:
            made_row = reqs.create_request(
                conn, emp['id'], kind, items, term=3 + index % 4,
                notes='Seeded request', actor='employee:%s' % emp['id'],
                actor_role='staff')
        req_id = made_row['id']
        stage = index % 5
        try:
            if stage == 1:
                with conn:
                    reqs.claim(conn, req_id, 'admin')
            elif stage == 2:
                with conn:
                    reqs.claim(conn, req_id, 'admin')
                    reqs.add_comment(conn, req_id, 'Which size do you need?',
                                     'admin', ask_for_info=True)
            elif stage == 3:
                with conn:
                    reqs.set_status(conn, req_id, 'declined', 'admin',
                                    message='Outside this season\'s allocation.')
            elif stage == 4:
                start_year, start_month = shift(3)
                with conn:
                    reqs.convert_to_plan(
                        conn, req_id, 'admin', start_year=start_year,
                        start_month=start_month, term=3 + index % 4,
                        sale_number='SO-%05d' % (7500 + index))
        except ValueError:
            pass
        made.append(made_row)
    return made
