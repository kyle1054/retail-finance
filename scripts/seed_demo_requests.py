"""Seed (or remove) DEMO staff requests — one in every state, so the whole
uniform/lay-by request flow can be walked through without waiting for real asks.

Everything it creates carries `created_via='demo'`, which is how `--remove` finds
it again: the requests, their items and thread, and the two real deduction plans
that the "Done" examples produced. Nothing else is touched.

It is IDEMPOTENT — a seed run purges any previous demo rows first, so re-running
never duplicates.

    python seed_demo_requests.py            # seed
    python seed_demo_requests.py --remove   # take it all away again

Local dev only in practice; it writes real plans, so don't run it on production
unless you genuinely want demo deductions on real employees.
"""
import os
import sys

# Repo root importable so `import database` resolves when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
from datetime import date

from northwind.data import database as db
from northwind.deductions import requests as reqs

MARKER = 'demo'          # staff_requests.created_via
ADMIN = 'admin'           # the admin the demo thread is attributed to

# Eight asks, one per state the queue can show. `age` is how many days ago it was
# raised, so the list has a believable order instead of eight identical timestamps.
DEMO = [
    dict(key='new_uniform', kind='uniform', age=0, term=6,
         notes='Mine are worn through — I need them before the December rush.',
         items=[dict(description='Ladies trainer, sage', size='UK5', unit_price=1290, quantity=1, sku='660200120451'),
                dict(description='Logo tee, black', size='M', quantity=2, sku='660200120452')],
         state='submitted'),
    dict(key='new_layby', kind='layby', age=1, term=3,
         notes='Saw it in store today.',
         items=[dict(description='Quilted jacket', size='L', unit_price=2499, quantity=1, sku='660200330871')],
         state='submitted'),
    dict(key='working', kind='uniform', age=2, term=4,
         notes='Second pair — the first ones are done in.',
         items=[dict(description='Mens trainer, off-white', size='UK9', unit_price=1290, quantity=1, sku='660200118820')],
         state='in_progress'),
    dict(key='waiting', kind='layby', age=3, term=6,
         notes='Would like it before month end if possible.',
         items=[dict(description='Felted overshirt', size='M', unit_price=1899, quantity=1, sku='660200118821'),
                dict(description='Twill trouser, stone', size='32', quantity=1, sku='660200441002')],
         state='info_needed',
         question='We only have the overshirt in L at the moment — is L okay, or shall we order M?'),
    dict(key='approved', kind='uniform', age=4, term=6,
         notes='Store manager said I could ask.',
         items=[dict(description='Ladies trainer, slate', size='UK6', unit_price=1290, quantity=1, sku='660200441003')],
         state='approved'),
    dict(key='done', kind='layby', age=6, term=3,
         notes='Happy with 3 months.',
         items=[dict(description='Quilted jacket, navy', size='M', unit_price=2499, quantity=1, sku='660200772310'),
                dict(description='Beanie', unit_price=299, quantity=1, sku='660200772311')],
         state='fulfilled'),
    dict(key='done_uniform', kind='uniform', age=7, term=6,
         notes='',
         items=[dict(description='Mens trainer, slate', size='UK10', unit_price=1290, quantity=1, sku='660200905517')],
         state='fulfilled'),
    dict(key='declined', kind='uniform', age=8, term=6,
         notes='Would love the new season pair.',
         items=[dict(description='Seasonal runner', size='UK7', unit_price=2490, quantity=1, sku='660200905518')],
         state='declined',
         reason='Limited editions are not on the staff uniform list — pick from the core range.'),
    dict(key='cancelled', kind='layby', age=9, term=4,
         notes='',
         items=[dict(description='Canvas holdall', unit_price=3499, quantity=1, sku='660200660145')],
         state='cancelled'),
]


# --------------------------------------------------------------------------- #
# Removal
# --------------------------------------------------------------------------- #
def purge(conn):
    """Delete every demo request, its thread, and the plans it created."""
    rows = conn.execute(
        "SELECT id, plan_type, plan_id FROM staff_requests WHERE created_via=?",
        (MARKER,)).fetchall()
    for row in rows:
        conn.execute("DELETE FROM staff_request_events WHERE request_id=?", (row['id'],))
        conn.execute("DELETE FROM staff_request_items WHERE request_id=?", (row['id'],))
        if row['plan_id']:
            if row['plan_type'] == 'layby':
                conn.execute("DELETE FROM layby_items_cents WHERE layby_id=?", (row['plan_id'],))
                conn.execute("DELETE FROM layby_deductions_cents WHERE id=?", (row['plan_id'],))
            elif row['plan_type'] == 'uniform':
                conn.execute("DELETE FROM uniform_deductions_cents WHERE id=?", (row['plan_id'],))
    conn.execute("DELETE FROM staff_requests WHERE created_via=?", (MARKER,))
    return len(rows)


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #
def _candidates(conn, wanted):
    """Active retail employees to hang the demo asks on, spread over stores."""
    rows = conn.execute(
        "SELECT id, full_name, current_store FROM employees "
        "WHERE status='active' AND COALESCE(sector,'retail')='retail' "
        "AND current_store IS NOT NULL AND current_store <> '' "
        "ORDER BY current_store, id").fetchall()
    if len(rows) < wanted:
        raise SystemExit('Only %d usable employees — need %d.' % (len(rows), wanted))
    # One per store first, so the queue's store column isn't all one shop.
    picked, seen = [], set()
    for row in rows:
        if row['current_store'] not in seen:
            picked.append(row)
            seen.add(row['current_store'])
        if len(picked) == wanted:
            return picked
    for row in rows:                      # top up if there aren't enough stores
        if row not in picked:
            picked.append(row)
        if len(picked) == wanted:
            break
    return picked


def _first_unlocked_period():
    """Next month, walked forward past any locked retail payroll period."""
    today = date.today()
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    for _ in range(24):
        if not db.is_period_locked(year, month, 'retail'):
            return year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    raise SystemExit('Every period in the next two years is locked.')


def _age(conn, req_id, days):
    """Backdate the ask and its thread so the queue reads like a real week."""
    conn.execute("UPDATE staff_requests SET created_at=datetime('now', ?) WHERE id=?",
                 ('-%d days' % days, req_id))
    conn.execute("UPDATE staff_request_events SET at=datetime('now', ?) WHERE request_id=?",
                 ('-%d days' % days, req_id))


def seed(conn):
    people = _candidates(conn, len(DEMO))
    year, month = _first_unlocked_period()
    made = []

    for spec, person in zip(DEMO, people):
        emp_id = person['id']
        staff_actor = 'employee:%s' % emp_id
        created = reqs.create_request(
            conn, emp_id, spec['kind'], spec['items'], term=spec['term'],
            notes=spec['notes'], actor=staff_actor, actor_role='staff',
            created_via=MARKER)
        req_id = created['id']
        _age(conn, req_id, spec['age'])
        state = spec['state']

        if state in ('in_progress', 'info_needed', 'approved', 'fulfilled'):
            reqs.claim(conn, req_id, ADMIN)
        if state == 'info_needed':
            reqs.add_comment(conn, req_id, spec['question'], ADMIN, ask_for_info=True)
        if state == 'approved':
            reqs.add_comment(conn, req_id, 'Approved — ordering it with the next store drop.',
                             ADMIN)
            reqs.set_status(conn, req_id, 'approved', ADMIN)
        if state == 'declined':
            reqs.set_status(conn, req_id, 'declined', ADMIN, message=spec['reason'])
        if state == 'cancelled':
            reqs.add_comment(conn, req_id, 'I found one myself, thanks!', staff_actor,
                             actor_role='staff')
            reqs.set_status(conn, req_id, 'cancelled', staff_actor, actor_role='staff')
        if state == 'fulfilled':
            # Prices must be real before money is created, so fill in the line the
            # staff member left blank — exactly what the admin does in the drawer.
            items = [dict(description=i['description'], quantity=i.get('quantity', 1),
                          sku=i.get('sku', ''),
                          unit_price=i.get('unit_price') or 349)
                     for i in spec['items']]
            total = sum(i['unit_price'] * i['quantity'] for i in items)
            reqs.convert_to_plan(
                conn, req_id, ADMIN, start_year=year, start_month=month,
                term=spec['term'], items=items,
                total=(total if spec['kind'] == 'uniform' else None),
                sale_number='SO-DEMO%d' % req_id)

        made.append((created['ref'], person['full_name'], person['current_store'],
                     spec['kind'], state))
    return made, (year, month)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remove', action='store_true',
                        help='delete the demo requests (and their plans) and exit')
    args = parser.parse_args()

    conn = db.get_db()
    try:
        removed = purge(conn)
        if args.remove:
            conn.commit()
            print('Removed %d demo request(s) ✓' % removed)
            return
        made, period = seed(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        print('Rolled back — nothing was written.', file=sys.stderr)
        raise
    finally:
        conn.close()

    if removed:
        print('(replaced %d previous demo request(s))' % removed)
    print('Seeded %d demo staff requests ✓   plans start %s %d' % (
        len(made), db.MONTH_NAMES[period[1]], period[0]))
    for ref, name, store, kind, state in made:
        print('  %-11s %-9s %-12s %-28s %s' % (
            ref, kind, reqs.STATUS_LABELS[state], name[:28], store))
    print('  Open /requests to work them. Remove later with:')
    print('    python seed_demo_requests.py --remove')


if __name__ == '__main__':
    main()
