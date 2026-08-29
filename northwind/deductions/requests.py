"""Staff requests — the ask, its workflow, and its conversion into a deduction plan.

Staff raise a uniform or lay-by request from the portal; an admin works it in the
queue at /requests and, on approval, this module hands the numbers to
``northwind/deductions/plans.py`` so the money is created by exactly the same code path as
a hand-captured plan. Nothing here writes to a money table.

Same conventions as ``plans.py``: Flask-free, the caller owns the transaction
(wrap in ``with conn:``), and every refusal is a ``ValueError`` carrying the message
the UI shows.
"""
from northwind.data import database as db
from northwind.services import money
from northwind.deductions import plans

KINDS = ('uniform', 'layby')
KIND_LABELS = {'uniform': 'Uniform', 'layby': 'Lay-by'}
KIND_ICONS = {'uniform': 'bi-bag-fill', 'layby': 'bi-cart-fill'}

# Ordered for display; 'fulfilled' is the end of the happy path (plan created).
STATUSES = ('submitted', 'in_progress', 'info_needed', 'approved',
            'fulfilled', 'declined', 'cancelled')
STATUS_LABELS = {
    'submitted': 'New', 'in_progress': 'Being sorted', 'info_needed': 'Waiting on staff',
    'approved': 'Approved', 'fulfilled': 'Done', 'declined': 'Declined',
    'cancelled': 'Cancelled',
}
# Statuses that still need someone to do something — the queue's default view and
# the cap on how many a person may have running at once.
OPEN_STATUSES = frozenset({'submitted', 'in_progress', 'info_needed', 'approved'})
# Statuses an admin may still convert into a plan from.
CONVERTIBLE = OPEN_STATUSES

# Which status changes are legal. Deliberately explicit rather than "anything goes":
# a declined/cancelled/fulfilled request is history, and re-opening it would leave
# the event thread lying about what happened.
TRANSITIONS = {
    'submitted': frozenset({'in_progress', 'info_needed', 'approved', 'declined', 'cancelled'}),
    'in_progress': frozenset({'info_needed', 'approved', 'declined', 'cancelled'}),
    'info_needed': frozenset({'in_progress', 'approved', 'declined', 'cancelled'}),
    'approved': frozenset({'in_progress', 'declined', 'cancelled'}),
    'fulfilled': frozenset(),
    'declined': frozenset(),
    'cancelled': frozenset(),
}
# What the staff side may do to their own request, and from where.
STAFF_CANCELLABLE = frozenset({'submitted', 'info_needed'})

MAX_OPEN_PER_EMPLOYEE = 3
MAX_ITEMS = 20
MAX_NOTE_CHARS = 1000
MAX_TERM_MONTHS = 12


# --------------------------------------------------------------------------- #
# Normalising the ask
# --------------------------------------------------------------------------- #
def normalize_items(items, require_price=False, require_sku=False):
    """Validate/normalise requested lines into [{description, sku, size, qty, price, total}].

    A line counts once it has a description — unlike a lay-by *plan*, a price is
    optional here, because staff routinely ask for something without knowing what it
    costs and the admin fills that in at approval. `require_price` is what the
    conversion step passes to insist on real prices before money is created;
    `require_sku` is what the portal passes, because the SKU is printed on the price
    tag and is what the office actually orders against — chasing it afterwards is the
    slowest part of fulfilling a request.
    """
    out = []
    for raw in (items or []):
        desc = str(raw.get('description') or raw.get('desc') or '').strip()
        if not desc:
            continue
        try:
            qty = int(raw.get('quantity', raw.get('qty', 1)) or 1)
        except (TypeError, ValueError):
            raise ValueError("Quantities must be whole numbers.")
        price_raw = raw.get('unit_price', raw.get('price', None))
        if price_raw in (None, ''):
            price = None
        else:
            try:
                price = float(price_raw)
            except (TypeError, ValueError):
                raise ValueError("Prices must be numbers.")
            if price < 0:
                raise ValueError("Prices cannot be negative.")
        if qty < 1:
            raise ValueError("Quantities must be at least 1.")
        if require_price and not price:
            raise ValueError("Every item needs a price before the plan can be created.")
        sku = str(raw.get('sku') or '').strip()[:60]
        if require_sku and not sku:
            raise ValueError(
                "Please add the SKU for \u201c%s\u201d \u2014 it's on the price tag." % desc[:40])
        out.append({'description': desc[:200], 'sku': sku,
                    'size': str(raw.get('size') or '').strip()[:40],
                    'qty': qty, 'price': price,
                    'total': None if price is None else round(price * qty, 2)})
    if not out:
        raise ValueError("Add at least one item to the request.")
    if len(out) > MAX_ITEMS:
        raise ValueError("A request can hold at most %d items." % MAX_ITEMS)
    return out


def _estimated_total(lines):
    """Sum of the priced lines, or None when nothing was priced."""
    priced = [l['total'] for l in lines if l['total'] is not None]
    return round(sum(priced), 2) if priced else None


def _valid_term(term):
    if term in (None, ''):
        return None
    try:
        term = int(term)
    except (TypeError, ValueError):
        raise ValueError("Preferred term must be a whole number of months.")
    if not 1 <= term <= MAX_TERM_MONTHS:
        raise ValueError("Preferred term must be between 1 and %d months." % MAX_TERM_MONTHS)
    return term


def _clean_note(text):
    return (str(text or '').strip())[:MAX_NOTE_CHARS]


def open_request_count(conn, emp_id):
    marks = ','.join('?' * len(OPEN_STATUSES))
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM staff_requests WHERE employee_id=? AND status IN (%s)" % marks,
        (emp_id,) + tuple(sorted(OPEN_STATUSES))).fetchone()
    return row['n']


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def _log(conn, req_id, event, actor, actor_role, from_status=None, to_status=None,
         message=None):
    conn.execute(
        "INSERT INTO staff_request_events "
        "(request_id, actor, actor_role, event, from_status, to_status, message) "
        "VALUES (?,?,?,?,?,?,?)",
        (req_id, actor or 'unknown', actor_role, event, from_status, to_status,
         _clean_note(message) or None))


def create_request(conn, emp_id, kind, items, term=None, notes='', actor=None,
                   actor_role='staff', created_via='portal', require_sku=False):
    """Log a staff request. Returns {id, ref, kind, employee_id, ...}."""
    if kind not in KINDS:
        raise ValueError("A request is either for a uniform or a lay-by.")
    emp_id, full_name, sector = plans.require_employee(conn, emp_id)
    row = conn.execute("SELECT current_store, status FROM employees WHERE id=?",
                       (emp_id,)).fetchone()
    if (row['status'] or 'active') != 'active':
        raise ValueError("%s is not an active employee." % full_name)

    lines = normalize_items(items, require_sku=require_sku)
    term = _valid_term(term)
    notes = _clean_note(notes)

    if open_request_count(conn, emp_id) >= MAX_OPEN_PER_EMPLOYEE:
        raise ValueError(
            "You already have %d requests open. Please wait for those to be finished "
            "before asking for something else." % MAX_OPEN_PER_EMPLOYEE)

    cur = conn.execute('''
        INSERT INTO staff_requests
            (kind, employee_id, store, sector, requested_term_months,
             estimated_total_cents, notes, created_by, created_via)
        VALUES (?,?,?,?,?,?,?,?,?)
    ''', (kind, emp_id, row['current_store'], sector, term,
          money.to_cents(_estimated_total(lines)), notes,
          actor or ('employee:%s' % emp_id), created_via))
    req_id = cur.lastrowid
    ref = 'REQ-%06d' % req_id
    conn.execute("UPDATE staff_requests SET ref=? WHERE id=?", (ref, req_id))

    for i, line in enumerate(lines):
        conn.execute(
            "INSERT INTO staff_request_items "
            "(request_id, description, sku, size, quantity, unit_price_cents, sort_order) "
            "VALUES (?,?,?,?,?,?,?)",
            (req_id, line['description'], line['sku'] or None, line['size'] or None,
             line['qty'], money.to_cents(line['price']), i))

    _log(conn, req_id, 'created', actor or ('employee:%s' % emp_id), actor_role,
         to_status='submitted',
         message='%s request for %s' % (KIND_LABELS[kind], full_name))

    return {'id': req_id, 'ref': ref, 'kind': kind, 'employee_id': emp_id,
            'employee_name': full_name, 'sector': sector, 'status': 'submitted',
            'items': lines, 'estimated_total': _estimated_total(lines),
            'requested_term_months': term}


def _require_request(conn, req_id):
    row = conn.execute("SELECT * FROM staff_requests WHERE id=?", (req_id,)).fetchone()
    if row is None:
        raise ValueError("Request not found.")
    return row


def claim(conn, req_id, actor):
    """Admin takes ownership. Idempotent for the same admin."""
    row = _require_request(conn, req_id)
    if row['status'] not in ('submitted', 'info_needed'):
        if row['claimed_by'] == actor:
            return row['status']
        raise ValueError("This request is already being worked on.")
    conn.execute(
        "UPDATE staff_requests SET status='in_progress', claimed_by=?, "
        "claimed_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
        (actor, req_id))
    _log(conn, req_id, 'status', actor, 'admin', row['status'], 'in_progress',
         'Picked up by %s' % actor)
    return 'in_progress'


def set_status(conn, req_id, to_status, actor, actor_role='admin', message=None):
    """Move a request along the workflow, recording who and why."""
    row = _require_request(conn, req_id)
    from_status = row['status']
    if to_status == from_status:
        return from_status
    if to_status not in STATUSES:
        raise ValueError("Unknown request status %r." % to_status)
    if to_status not in TRANSITIONS[from_status]:
        raise ValueError("A %s request cannot be moved to %s." % (
            STATUS_LABELS[from_status].lower(), STATUS_LABELS[to_status].lower()))
    if actor_role == 'staff':
        # Staff may only withdraw their own ask; everything else is the admin's call.
        if to_status != 'cancelled' or from_status not in STAFF_CANCELLABLE:
            raise ValueError("You can only cancel a request that is still open.")
    if to_status == 'declined' and not _clean_note(message):
        raise ValueError("Please give a reason for declining, so the staff member knows why.")

    sets = ["status=?", "updated_at=datetime('now')"]
    params = [to_status]
    if to_status in ('approved', 'declined', 'cancelled'):
        sets += ["decided_by=?", "decided_at=datetime('now')"]
        params.append(actor)
    if to_status == 'declined':
        sets.append("decline_reason=?")
        params.append(_clean_note(message))
    params.append(req_id)
    conn.execute("UPDATE staff_requests SET %s WHERE id=?" % ', '.join(sets), params)
    _log(conn, req_id, 'status', actor, actor_role, from_status, to_status, message)
    return to_status


def add_comment(conn, req_id, message, actor, actor_role='admin', ask_for_info=False):
    """Add a message to the thread.

    An admin can flip the request to 'info_needed' in the same breath; a staff reply
    to an 'info_needed' request pulls it back into the queue, otherwise a question
    answered would sit there looking like it was still waiting.
    """
    row = _require_request(conn, req_id)
    message = _clean_note(message)
    if not message:
        raise ValueError("Type a message first.")
    if row['status'] in ('declined', 'cancelled', 'fulfilled'):
        raise ValueError("This request is closed.")
    _log(conn, req_id, 'comment', actor, actor_role, message=message)
    conn.execute("UPDATE staff_requests SET updated_at=datetime('now') WHERE id=?", (req_id,))
    if ask_for_info and row['status'] != 'info_needed':
        set_status(conn, req_id, 'info_needed', actor, actor_role)
        return 'info_needed'
    if actor_role == 'staff' and row['status'] == 'info_needed':
        conn.execute(
            "UPDATE staff_requests SET status='in_progress', updated_at=datetime('now') "
            "WHERE id=?", (req_id,))
        _log(conn, req_id, 'status', actor, 'staff', 'info_needed', 'in_progress',
             'Staff answered')
        return 'in_progress'
    return row['status']


def convert_to_plan(conn, req_id, actor, start_year, start_month, term,
                    items=None, total=None, monthly=None, sku='', sale_number='',
                    description=None, discount_pct=40, notes=None):
    """Approve a request by creating the real deduction plan for it.

    The numbers come from the admin's approval form (prices confirmed, term and start
    period chosen) and are handed to plans.create_uniform_plan / create_layby_plan —
    the same functions the manual add forms and the MCP tools use, so a request-born
    plan is indistinguishable from a hand-captured one. Locked payroll periods are
    refused there, as always.
    """
    row = _require_request(conn, req_id)
    if row['plan_id']:
        raise ValueError("This request has already been turned into a plan.")
    if row['status'] not in CONVERTIBLE:
        raise ValueError("A %s request cannot be turned into a plan." %
                         STATUS_LABELS[row['status']].lower())

    plan_notes = _clean_note(notes if notes is not None else row['notes'])
    trail = 'From staff request %s' % row['ref']
    plan_notes = ('%s — %s' % (plan_notes, trail)) if plan_notes else trail

    if row['kind'] == 'layby':
        lines = normalize_items(items if items is not None else get_items(conn, req_id),
                                require_price=True)
        result = plans.create_layby_plan(
            conn, row['employee_id'],
            items=[{'description': l['description'], 'unit_price': l['price'],
                    'quantity': l['qty']} for l in lines],
            term=term, start_year=start_year, start_month=start_month,
            discount_pct=discount_pct, sale_number=sale_number, notes=plan_notes,
            actor=actor)
    else:
        lines = normalize_items(items if items is not None else get_items(conn, req_id))
        if total in (None, ''):
            total = _estimated_total(lines)
        if total in (None, ''):
            raise ValueError("Enter the total before creating the plan.")
        try:
            total = float(total)
            term_n = int(term)
        except (TypeError, ValueError):
            raise ValueError("Please enter valid numbers for the total and term.")
        if monthly in (None, ''):
            monthly = round(total / term_n, 2) if term_n else 0
        result = plans.create_uniform_plan(
            conn, row['employee_id'], monthly=monthly, term=term_n,
            start_year=start_year, start_month=start_month, total=total,
            sku=sku or (lines[0]['sku'] if lines else ''),
            description=description or ', '.join(l['description'] for l in lines),
            sale_number=sale_number, notes=plan_notes, actor=actor)

    # `AND plan_id IS NULL` makes the claim on the request atomic with creating the
    # plan: if anything else got there first (a double-submitted form, a second
    # admin) this UPDATE touches no rows, we raise, and the caller's `with conn:`
    # rolls the just-created plan back. Without it, two clicks could write two real
    # deductions for one request — the only place in this module money is at stake.
    claimed = conn.execute(
        "UPDATE staff_requests SET status='fulfilled', plan_type=?, plan_id=?, "
        "decided_by=?, decided_at=datetime('now'), updated_at=datetime('now') "
        "WHERE id=? AND plan_id IS NULL",
        (result['plan_type'], result['plan_id'], actor, req_id))
    if claimed.rowcount != 1:
        raise ValueError("This request has already been turned into a plan.")
    _log(conn, req_id, 'converted', actor, 'admin', row['status'], 'fulfilled',
         '%s plan created: R%.2f over %d month(s) from %s %d' % (
             KIND_LABELS[row['kind']], result['total'], result['term_months'],
             db.MONTH_NAMES[result['start_month']], result['start_year']))
    return result


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def _item(row):
    price = money.to_rands(row['unit_price_cents'])
    return {'description': row['description'], 'sku': row['sku'] or '',
            'size': row['size'] or '', 'qty': row['quantity'], 'price': price,
            'total': None if price is None else round(price * row['quantity'], 2)}


def get_items(conn, req_id):
    return [_item(r) for r in conn.execute(
        "SELECT description, sku, size, quantity, unit_price_cents FROM staff_request_items "
        "WHERE request_id=? ORDER BY sort_order, id", (req_id,)).fetchall()]


def _items_for(conn, req_ids):
    """{request_id: [item, ...]} in ONE query — the queue lists many requests and a
    per-row get_items() would be an N+1 sweep over the whole page."""
    if not req_ids:
        return {}
    marks = ','.join('?' * len(req_ids))
    grouped = {rid: [] for rid in req_ids}
    for row in conn.execute(
            "SELECT request_id, description, sku, size, quantity, unit_price_cents "
            "FROM staff_request_items WHERE request_id IN (%s) "
            "ORDER BY request_id, sort_order, id" % marks, tuple(req_ids)).fetchall():
        grouped[row['request_id']].append(_item(row))
    return grouped


def _event(row):
    return {'at': row['at'], 'actor': row['actor'] or '',
            'actor_role': row['actor_role'] or '', 'event': row['event'],
            'from_status': row['from_status'], 'to_status': row['to_status'],
            'to_label': STATUS_LABELS.get(row['to_status'], row['to_status']),
            'message': row['message'] or ''}


def _events_for(conn, req_ids):
    """{request_id: [event, ...]} in ONE query, for the same reason as _items_for."""
    if not req_ids:
        return {}
    marks = ','.join('?' * len(req_ids))
    grouped = {rid: [] for rid in req_ids}
    for row in conn.execute(
            "SELECT * FROM staff_request_events WHERE request_id IN (%s) "
            "ORDER BY request_id, id" % marks, tuple(req_ids)).fetchall():
        grouped[row['request_id']].append(_event(row))
    return grouped


def _created_by_label(row, employee_name):
    """Who logged the ask, in words. 'employee:EMP-0003' is the person themselves —
    printing the raw id on the queue read like a system error."""
    created_by = row['created_by'] or ''
    if created_by == 'employee:%s' % row['employee_id']:
        return '%s, in the portal' % (employee_name or 'the staff member')
    return created_by or 'unknown'


def _plans_for(conn, rows):
    """{(plan_type, plan_id): plan summary} for the requests that became plans.

    A finished request said only "plan created", which meant leaving the queue to
    find out what was actually written. Balances come from the same helpers the
    rest of the app uses — never re-derived here.
    """
    wanted = {}
    for row in rows:
        if row['plan_id'] and row['plan_type'] in ('uniform', 'layby'):
            wanted.setdefault(row['plan_type'], []).append(row['plan_id'])
    out = {}
    for kind, ids in wanted.items():
        table = 'uniform_deductions' if kind == 'uniform' else 'layby_deductions'
        marks = ','.join('?' * len(ids))
        for p in conn.execute("SELECT * FROM %s WHERE id IN (%s)" % (table, marks),
                              tuple(ids)).fetchall():
            total = p['total_amount']
            if total is None:
                total = round((p['term_months'] or 0) * (p['monthly_amount'] or 0), 2)
            if kind == 'uniform':
                remaining = db.calc_uniform_balance(p) if p['status'] == 'active' else 0.0
            else:
                remaining = (p['balance_remaining'] if p['balance_remaining'] is not None
                             else (p['term_months'] - p['payments_made']) * p['monthly_amount']) \
                    if p['status'] == 'active' else 0.0
            out[(kind, p['id'])] = {
                'total': round(total, 2), 'monthly': p['monthly_amount'],
                'term': p['term_months'], 'payments_made': p['payments_made'],
                'status': p['status'], 'remaining': round(remaining or 0, 2),
                'start_label': '%s %s' % (db.MONTH_NAMES[p['start_month']], p['start_year']),
                'sale_number': p['sale_number'] or '',
                'discount_pct': (p['discount_pct'] if kind == 'layby' else None),
            }
    return out


def store_message(req):
    """The line to paste to the store, for whatever state the request is in.

    There is no automated email in this build (deliberate), so
    the decision has to be copy-and-pasteable or it doesn't reach anyone.
    """
    who = req.get('employee_name') or 'the staff member'
    ref, kind = req['ref'], KIND_LABELS.get(req['kind'], req['kind']).lower()
    plan = req.get('plan')
    if plan:
        months = '%d month%s' % (plan['term'], '' if plan['term'] == 1 else 's')
        return ("%s: your %s request %s is approved and set up. R%s comes off your pay "
                "over %s (R%s a month), starting %s.%s"
                % (who, kind, ref, '{:,.2f}'.format(plan['total']), months,
                   '{:,.2f}'.format(plan['monthly'] or 0), plan['start_label'],
                   ' Sale %s.' % plan['sale_number'] if plan['sale_number'] else ''))
    if req['status'] == 'approved':
        return ("%s: your %s request %s is approved. We are getting the item for you — "
                "the deduction is set up when it is handed over." % (who, kind, ref))
    if req['status'] == 'declined':
        return ("%s: your %s request %s was not approved.%s"
                % (who, kind, ref,
                   ' Reason: %s' % req['decline_reason'] if req['decline_reason'] else ''))
    return None


def _row_to_dict(row):
    employee_name = row['full_name'] if 'full_name' in row.keys() else None
    return {
        'id': row['id'], 'ref': row['ref'], 'kind': row['kind'],
        'kind_label': KIND_LABELS.get(row['kind'], row['kind']),
        'icon': KIND_ICONS.get(row['kind'], 'bi-inbox'),
        'employee_id': row['employee_id'], 'store': row['store'] or '',
        'sector': row['sector'], 'status': row['status'],
        'status_label': STATUS_LABELS.get(row['status'], row['status']),
        'is_open': row['status'] in OPEN_STATUSES,
        'requested_term_months': row['requested_term_months'],
        'estimated_total': money.to_rands(row['estimated_total_cents']),
        'notes': row['notes'] or '', 'created_at': row['created_at'],
        'created_by': row['created_by'] or '', 'created_via': row['created_via'] or '',
        'updated_at': row['updated_at'], 'claimed_by': row['claimed_by'] or '',
        'decided_by': row['decided_by'] or '', 'decided_at': row['decided_at'],
        'decline_reason': row['decline_reason'] or '',
        'plan_type': row['plan_type'], 'plan_id': row['plan_id'],
        'employee_name': employee_name,
        'job_title': row['job_title'] if 'job_title' in row.keys() else None,
        'created_by_label': _created_by_label(row, employee_name),
    }


def get_request(conn, req_id, with_thread=True):
    row = conn.execute(
        "SELECT r.*, e.full_name, e.job_title FROM staff_requests r "
        "JOIN employees e ON e.id = r.employee_id WHERE r.id=?", (req_id,)).fetchone()
    if row is None:
        return None
    out = _row_to_dict(row)
    out['items'] = get_items(conn, req_id)
    out['plan'] = _plans_for(conn, [row]).get((row['plan_type'], row['plan_id']))
    out['store_message'] = store_message(out)
    if with_thread:
        out['events'] = [_event(e) for e in conn.execute(
            "SELECT * FROM staff_request_events WHERE request_id=? ORDER BY id",
            (req_id,)).fetchall()]
    return out


def list_requests(conn, status='open', kind=None, store=None, sector=None,
                  employee_id=None, limit=300, with_thread=False):
    """Newest-first requests, each with its items (and optionally its thread).

    `status` takes a single status, 'open' (anything still needing action) or 'all'.
    Items and events are fetched in one query each, not per row."""
    where, params = [], []
    if status == 'open':
        where.append("r.status IN (%s)" % ','.join('?' * len(OPEN_STATUSES)))
        params += sorted(OPEN_STATUSES)
    elif status and status != 'all':
        where.append("r.status = ?")
        params.append(status)
    if kind in KINDS:
        where.append("r.kind = ?")
        params.append(kind)
    if store:
        where.append("r.store = ?")
        params.append(store)
    if sector:
        where.append("r.sector = ?")
        params.append(sector)
    if employee_id:
        where.append("r.employee_id = ?")
        params.append(employee_id)
    sql = ("SELECT r.*, e.full_name, e.job_title, "
           "(SELECT COUNT(*) FROM staff_request_items i WHERE i.request_id=r.id) AS item_count "
           "FROM staff_requests r JOIN employees e ON e.id = r.employee_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.created_at DESC, r.id DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    ids = [row['id'] for row in rows]
    items = _items_for(conn, ids)
    events = _events_for(conn, ids) if with_thread else {}
    plans_by_id = _plans_for(conn, rows)
    out = []
    for row in rows:
        d = _row_to_dict(row)
        d['plan'] = plans_by_id.get((row['plan_type'], row['plan_id']))
        d['store_message'] = store_message(d)
        d['item_count'] = row['item_count']
        d['items'] = items.get(row['id'], [])
        # A one-line summary, not the whole basket: twenty items with long product
        # names built a 2 000-character string that made one list row 980px tall.
        summary = ', '.join(i['description'] for i in d['items'])
        d['summary'] = summary if len(summary) <= 110 else summary[:109].rstrip(' ,') + '…'
        if with_thread:
            d['events'] = events.get(row['id'], [])
        out.append(d)
    return out


def counts_by_status(conn, sector=None):
    sql = "SELECT status, COUNT(*) AS n FROM staff_requests"
    params = ()
    if sector:
        sql += " WHERE sector = ?"
        params = (sector,)
    sql += " GROUP BY status"
    counts = {s: 0 for s in STATUSES}
    for row in conn.execute(sql, params).fetchall():
        counts[row['status']] = row['n']
    counts['open'] = sum(counts[s] for s in OPEN_STATUSES)
    counts['all'] = sum(counts[s] for s in STATUSES)
    return counts


def pending_count(conn, sector=None):
    """Requests nobody has picked up yet — the number worth putting on a badge."""
    sql = "SELECT COUNT(*) AS n FROM staff_requests WHERE status='submitted'"
    params = ()
    if sector:
        sql += " AND sector = ?"
        params = (sector,)
    return conn.execute(sql, params).fetchone()['n']
