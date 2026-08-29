"""Plan write operations, shared by the web routes and the MCP connector.

Every function here was lifted verbatim out of a route handler in
``routes_uniforms.py`` / ``routes_laybys.py`` / ``routes_undercharges.py``. The
routes now parse the form and call these; the MCP tools in ``northwind_mcp/tools_plans.py``
call the SAME functions. One implementation, two front doors — so an agent-created
plan and a browser-created plan cannot drift apart (they already had: the MCP's own
copy of the adjust math used a different NULL-``total_amount`` fallback than the
route it mirrored).

Conventions:

* **Flask-free.** No ``request``/``flash``/``session`` — so this is unit-testable on
  the 3.9 dev venv, unlike anything that imports fastmcp.
* **The caller owns the transaction.** Every function takes an open ``conn`` and does
  NOT commit, matching ``database.create_undercharge_schedule``. Wrap calls in
  ``with conn:`` so a failure rolls the whole thing back.
* **Failures raise ``ValueError``** carrying the exact message the UI used to flash,
  so the route can flash it and the MCP tool can return it unchanged.
* **Money math is byte-identical to the routes'** — the same float formulas, then
  ``to_cents`` at the write boundary. Deliberately NOT "improved" into pure-cents
  math: these figures are already stored and pinned by ``tests/golden_entered_amounts``,
  and changing which cent lands where would silently restate real balances.
* **``actor``** is recorded wherever the schema has somewhere to put it, so an
  ``mcp:claude`` write is distinguishable from an ``admin`` one in the audit trail.
"""
from northwind.data import database as db
from northwind.services import money

# The sector-aware lock check needs a sector; an employee who doesn't exist would
# silently default to 'retail', so existence is checked explicitly instead.
PLAN_TYPES = ('uniform', 'layby', 'undercharge')
_TABLES = {'uniform': 'uniform_deductions', 'layby': 'layby_deductions',
           'undercharge': 'undercharges'}


# --------------------------------------------------------------------------- #
# Shared guards
# --------------------------------------------------------------------------- #
def require_employee(conn, emp_id):
    """Return (emp_id, full_name, sector) or raise ValueError.

    The routes relied on the FK to reject a bad employee_id, which surfaced as a
    500. Checking here gives both front doors the same friendly refusal.
    """
    row = conn.execute(
        "SELECT id, full_name, sector, status FROM employees WHERE id=?",
        (emp_id,)).fetchone()
    if row is None:
        raise ValueError("Employee {!r} not found.".format(emp_id))
    return row['id'], row['full_name'], (row['sector'] or 'retail')


def _valid_period(year, month, label="start period"):
    valid = db.validate_month_year(year, month)
    if valid is None:
        raise ValueError("A valid {} (month 1-12) is required.".format(label))
    return valid


def _require_unlocked(year, month, sector, label="starting payroll period"):
    if db.is_period_locked(year, month, sector):
        raise ValueError(
            "Cannot add deduction. The {} ({}-{:02d}, {}) is locked.".format(
                label, year, month, sector))


def project_schedule(start_year, start_month, total_amount, monthly_amount, term):
    """Month-by-month installments a uniform/lay-by plan will produce (Rands).

    Uses ``money.schedule_cents`` — the same allocation the rest of the app reads —
    so a preview shows exactly what the plan will deduct, final-month remainder
    included. Powers the MCP preview tools; nothing is written.
    """
    out = []
    for i, cents in enumerate(money.schedule_cents(total_amount, monthly_amount, term)):
        year, month = db._uc_add_month(start_year, start_month, i)
        out.append({"year": year, "month": month, "amount": money.to_rands(cents)})
    return out


# --------------------------------------------------------------------------- #
# Uniform
# --------------------------------------------------------------------------- #
def create_uniform_plan(conn, emp_id, monthly, term, start_year, start_month,
                        total=None, sku='', description='', sale_number='',
                        notes='', actor=None):
    """Create a uniform deduction plan. Mirrors POST /uniform/add.

    `total` defaults to monthly * term (the form's behaviour when the total field is
    left blank). Refuses if the starting payroll period is locked for the employee's
    sector.
    """
    emp_id, full_name, sector = require_employee(conn, emp_id)
    start_year, start_month = _valid_period(start_year, start_month)
    try:
        monthly = float(monthly)
        term = int(term)
        total_amt = float(total) if total is not None else round(monthly * term, 2)
    except (TypeError, ValueError):
        raise ValueError("Please enter valid numbers for amount, term and start period.")

    if monthly <= 0 or term <= 0 or total_amt < 0:
        raise ValueError("Monthly amount and term must be greater than zero.")

    _require_unlocked(start_year, start_month, sector)

    cur = conn.execute('''
        INSERT INTO uniform_deductions_cents
            (employee_id, sku, description, sale_number, total_amount_cents,
             monthly_amount_cents, balance_remaining_cents, term_months,
             start_month, start_year, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (emp_id, sku or '', description or '', sale_number or '',
          db.to_cents(total_amt), db.to_cents(monthly), db.to_cents(total_amt),
          term, start_month, start_year, notes or ''))

    return {"plan_type": "uniform", "plan_id": cur.lastrowid,
            "employee_id": emp_id, "employee_name": full_name, "sector": sector,
            "total": round(total_amt, 2), "monthly": round(monthly, 2),
            "term_months": term, "start_year": start_year,
            "start_month": start_month, "actor": actor or 'admin',
            "schedule": project_schedule(start_year, start_month, total_amt,
                                         monthly, term)}


# --------------------------------------------------------------------------- #
# Lay-by
# --------------------------------------------------------------------------- #
def normalize_layby_items(items):
    """Validate/normalise a lay-by basket into [{desc, price, qty, total}].

    Mirrors the route's collection loop: a line needs a description and a price > 0
    to count, quantity defaults to 1.
    """
    out = []
    for raw in (items or []):
        desc = str(raw.get('description') or raw.get('desc') or '').strip()
        try:
            price = float(raw.get('unit_price', raw.get('price', 0)) or 0)
            qty = int(raw.get('quantity', raw.get('qty', 1)) or 1)
        except (TypeError, ValueError):
            raise ValueError("Lay-by item prices and quantities must be numbers.")
        if desc and price > 0:
            out.append({'desc': desc, 'price': price, 'qty': qty,
                        'total': round(price * qty, 2)})
    if not out:
        raise ValueError("Add at least one item to the lay-by.")
    return out


def create_layby_plan(conn, emp_id, items, term, start_year, start_month,
                      discount_pct=40, sale_number='', notes='', actor=None):
    """Create a lay-by plan plus its basket items. Mirrors POST /layby/add.

    `items` is a list of {description, unit_price, quantity}. The staff discount is
    applied to the basket total, then spread over `term` months.
    """
    emp_id, full_name, sector = require_employee(conn, emp_id)
    start_year, start_month = _valid_period(start_year, start_month)
    try:
        term = int(term)
        discount_pct = float(discount_pct)
    except (TypeError, ValueError):
        raise ValueError("Please enter valid numbers for term, discount and start period.")

    if term <= 0:
        raise ValueError("Term must be at least one month.")
    if discount_pct < 0:
        raise ValueError("Discount cannot be negative.")

    lines = normalize_layby_items(items)
    _require_unlocked(start_year, start_month, sector)

    basket_total = round(sum(it['total'] for it in lines), 2)
    discounted_total = round(basket_total * (1 - discount_pct / 100), 2)
    monthly_amount = round(discounted_total / term, 2)
    description = ', '.join(it['desc'] for it in lines)

    cur = conn.execute('''
        INSERT INTO layby_deductions_cents
            (employee_id, sale_number, description, basket_total_cents, discount_pct,
             total_amount_cents, monthly_amount_cents, balance_remaining_cents,
             term_months, start_month, start_year, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (emp_id, (sale_number or '').strip(), description,
          db.to_cents(basket_total), discount_pct, db.to_cents(discounted_total),
          db.to_cents(monthly_amount), db.to_cents(discounted_total),
          term, start_month, start_year, notes or ''))
    layby_id = cur.lastrowid

    for it in lines:
        conn.execute(
            "INSERT INTO layby_items_cents "
            "(layby_id, description, unit_price_cents, quantity, line_total_cents) "
            "VALUES (?,?,?,?,?)",
            (layby_id, it['desc'], db.to_cents(it['price']), it['qty'],
             db.to_cents(it['total'])))

    return {"plan_type": "layby", "plan_id": layby_id,
            "employee_id": emp_id, "employee_name": full_name, "sector": sector,
            "basket_total": basket_total, "discount_pct": discount_pct,
            "total": discounted_total, "monthly": monthly_amount,
            "term_months": term, "start_year": start_year,
            "start_month": start_month, "items": lines, "actor": actor or 'admin',
            "schedule": project_schedule(start_year, start_month, discounted_total,
                                         monthly_amount, term)}


# --------------------------------------------------------------------------- #
# Undercharge ("cash miss")
# --------------------------------------------------------------------------- #
def create_undercharge(conn, emp_id, total, incident_year, incident_month,
                       recovery='full', split_months=1, start_year=None,
                       start_month=None, uc_type='undercharge', reason='',
                       sale_number='', notes='', actor=None):
    """Create an undercharge ('cash miss') or overcharge. Mirrors POST /undercharge/add.

    An **overcharge** is a credit back to the employee: it is forced to single-month
    'full' recovery in the incident month and gets no deduction schedule. An
    **undercharge** recovers either in 'full' (one month) or 'split' over
    `split_months`, starting at (start_year, start_month) — defaulting to the incident
    month — and gets an exact recovery schedule via create_undercharge_schedule, which
    refuses if ANY month it would land in is locked.
    """
    emp_id, full_name, sector = require_employee(conn, emp_id)
    if uc_type not in ('undercharge', 'overcharge'):
        raise ValueError("type must be 'undercharge' or 'overcharge'.")
    incident_year, incident_month = _valid_period(incident_year, incident_month,
                                                 "incident period")
    try:
        total_amount = float(total)
    except (TypeError, ValueError):
        raise ValueError("Please enter valid numbers for amount, split and dates.")

    if uc_type == 'overcharge':
        recovery, split_months = 'full', 1
        start_year, start_month = incident_year, incident_month
    else:
        if recovery not in ('full', 'split'):
            raise ValueError("recovery must be 'full' or 'split'.")
        try:
            split_months = int(split_months) if recovery == 'split' else 1
        except (TypeError, ValueError):
            raise ValueError("Please enter valid numbers for amount, split and dates.")
        start_year, start_month = _valid_period(
            start_year if start_year is not None else incident_year,
            start_month if start_month is not None else incident_month)

    if total_amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    if recovery == 'split' and split_months <= 0:
        raise ValueError("Split must be at least one month.")

    # For undercharges the STARTING DEDUCTION period is what must be open. (The
    # schedule builder re-checks every month in the run and raises on any lock.)
    if uc_type != 'overcharge':
        _require_unlocked(start_year, start_month, sector,
                          "starting deduction payroll period")

    cur = conn.execute('''
        INSERT INTO undercharges_cents
            (employee_id, sale_number, total_amount_cents, reason, incident_month,
             incident_year, start_month, start_year, recovery_method, split_months,
             notes, type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (emp_id, sale_number or '', db.to_cents(total_amount), reason or '',
          incident_month, incident_year, start_month, start_year, recovery,
          split_months, notes or '', uc_type))
    uc_id = cur.lastrowid

    schedule = []
    if uc_type != 'overcharge':
        db.create_undercharge_schedule(
            conn, uc_id, start_year, start_month, split_months,
            db.to_cents(total_amount), kind='deduction',
            reason='Original recovery schedule', actor=actor or 'admin')
        for i, cents in enumerate(db._uc_split_cents(db.to_cents(total_amount),
                                                    split_months)):
            year, month = db._uc_add_month(start_year, start_month, i)
            schedule.append({"year": year, "month": month,
                             "amount": money.to_rands(cents)})

    return {"plan_type": "undercharge", "type": uc_type, "plan_id": uc_id,
            "employee_id": emp_id, "employee_name": full_name, "sector": sector,
            "total": round(total_amount, 2), "recovery_method": recovery,
            "split_months": split_months, "incident_year": incident_year,
            "incident_month": incident_month, "start_year": start_year,
            "start_month": start_month, "actor": actor or 'admin',
            "schedule": schedule}


def reschedule_undercharge(conn, uc_id, start_year, start_month, months, reason,
                           actor=None):
    """Replace an undercharge's unpaid future installments with a new exact schedule.

    Mirrors POST /undercharge/<id>/reschedule. Only the REMAINING employee balance is
    rescheduled; completed installments are never touched. A reason is mandatory (this
    is a change to what someone owes each month). Returns the new installment list.
    """
    uc = conn.execute("SELECT * FROM undercharges_cents WHERE id=?",
                      (uc_id,)).fetchone()
    if not uc or (uc['type'] or 'undercharge') != 'undercharge':
        raise ValueError("Undercharge {} not found.".format(uc_id))

    reason = (reason or '').strip()
    if not reason:
        raise ValueError("Please record why the remaining balance is being rescheduled.")

    account = db.get_undercharge_account(uc_id, conn)
    if account['remaining_cents'] <= 0:
        raise ValueError("This undercharge has no remaining employee balance to schedule.")

    start_year, start_month = _valid_period(start_year, start_month)
    try:
        months = int(months)
    except (TypeError, ValueError):
        raise ValueError("Enter a valid start month, year, and number of months.")

    db.create_undercharge_schedule(
        conn, uc_id, start_year, start_month, months, account['remaining_cents'],
        kind='deduction', reason=reason, actor=actor or 'admin')

    # Compatibility fields stay descriptive only — payments are ledger-derived, and
    # split_months stays >= payments_made so old audits still read sensibly.
    lifetime_term = account['payment_count'] + months
    conn.execute(
        "UPDATE undercharges_cents SET recovery_method=?,split_months=?,"
        "payments_made=?,status=? WHERE id=?",
        ('full' if lifetime_term == 1 else 'split', lifetime_term,
         account['payment_count'],
         'partial' if account['payment_count'] else 'pending', uc_id))

    installments = []
    for i, cents in enumerate(db._uc_split_cents(account['remaining_cents'], months)):
        year, month = db._uc_add_month(start_year, start_month, i)
        installments.append({"year": year, "month": month,
                             "amount": money.to_rands(cents)})

    return {"plan_type": "undercharge", "plan_id": uc_id,
            "employee_id": uc['employee_id'],
            "rescheduled": money.to_rands(account['remaining_cents']),
            "months": months, "start_year": start_year, "start_month": start_month,
            "already_paid_installments": account['payment_count'],
            "reason": reason, "actor": actor or 'admin',
            "schedule": installments}


# --------------------------------------------------------------------------- #
# Adjust / write off (all three plan types)
# --------------------------------------------------------------------------- #
def adjust_plan(conn, plan_type, plan_id, amount, note=None, new_term=None,
                actor=None):
    """Record an ad-hoc payment against a uniform or lay-by plan and recompute the
    monthly installment. Mirrors POST /uniform|/layby/<id>/adjust.

    Writes an audited ``plan_adjustments_cents`` row (now including `actor`).
    Undercharges have no adjust path — they use reschedule_undercharge / the
    undercharge event ledger instead.
    """
    if plan_type not in ('uniform', 'layby'):
        raise ValueError("plan_type must be 'uniform' or 'layby' "
                         "(undercharges have no adjust path).")
    table = _TABLES[plan_type]

    plan = conn.execute(
        "SELECT * FROM {} WHERE id=?".format(table), (plan_id,)).fetchone()
    if plan is None:
        raise ValueError("{} plan {} not found.".format(plan_type, plan_id))

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ValueError("Adjustment amount must be a number.")

    # The two plan types derive a missing balance DIFFERENTLY, and each is preserved
    # exactly as its route had it — these fallbacks only fire on legacy rows with a
    # NULL balance_remaining (every row created since carries one), so "unifying"
    # them would silently restate historical balances for no benefit.
    if plan_type == 'uniform':
        # Unset total = term * monthly; unset balance = total less what has been paid
        # at the entered monthly rate.
        total_amt = (plan['total_amount'] if plan['total_amount'] is not None
                     else round((plan['term_months'] or 0) * (plan['monthly_amount'] or 0), 2))
        current_balance = (plan['balance_remaining'] if plan['balance_remaining'] is not None
                           else total_amt - ((plan['payments_made'] or 0)
                                             * round(plan['monthly_amount'] or 0, 2)))
    else:  # layby — falls back to the full total (note: a 0 balance does too)
        current_balance = plan['balance_remaining'] or plan['total_amount']
    new_balance = round(max(0, current_balance - amount), 2)

    term_months = plan['term_months'] or 0
    payments_made = plan['payments_made'] or 0
    remaining_months = term_months - payments_made

    if new_term and int(new_term) > 0:
        remaining_months = int(new_term)
        term_months = payments_made + remaining_months
        conn.execute(
            "UPDATE {}_cents SET term_months=? WHERE id=?".format(table),
            (term_months, plan_id))

    new_monthly = round(new_balance / remaining_months, 2) if remaining_months > 0 else 0
    status = 'complete' if new_balance <= 0.01 else 'active'

    conn.execute(
        "UPDATE {}_cents SET balance_remaining_cents=?, monthly_amount_cents=?, "
        "status=? WHERE id=?".format(table),
        (db.to_cents(new_balance), db.to_cents(new_monthly), status, plan_id))
    conn.execute(
        "INSERT INTO plan_adjustments_cents "
        "(plan_type, plan_id, amount_cents, note, new_monthly_cents, actor) "
        "VALUES (?,?,?,?,?,?)",
        (plan_type, plan_id, db.to_cents(amount), note or '',
         db.to_cents(new_monthly), actor or 'admin'))

    return {"plan_type": plan_type, "plan_id": plan_id,
            "employee_id": plan['employee_id'], "amount": round(amount, 2),
            "new_balance": new_balance, "new_monthly": new_monthly,
            "term_months": term_months, "status": status,
            "actor": actor or 'admin'}


def edit_plan(conn, plan_type, plan_id, actor=None, **fields):
    """Edit an existing uniform or lay-by plan in place. Mirrors POST
    /uniform|/layby/<id>/edit.

    This is the "correct the record" path, distinct from ``adjust_plan`` (which
    records a *payment* against the balance): here the stored figures themselves are
    being restated, so any field can be set directly.

    Editable fields — pass only the ones to change, ``None`` leaves a field alone:

      both      description, sale_number, notes, total, monthly, balance_remaining,
                term_months, payments_made, start_year, start_month
      uniform   sku
      layby     basket_total, discount_pct

    `status` is not editable directly: it is derived the way the routes derive it
    (complete once payments cover the term or the balance is settled). Three
    deliberate differences from the raw route SQL this replaces, all guards the
    routes lacked:

    * a written-off plan is **not** silently resurrected to 'active' by an edit;
    * **moving** the start period into a locked payroll month is refused (editing
      any other field on a plan that already sits in a locked month is fine);
    * lay-by figures get the same numeric validation uniform already had, instead
      of raising straight out of ``float()`` as a 500.

    Every edit writes an audited ``plan_adjustments_cents`` row carrying the
    balance delta and a summary of what changed, so an ``mcp:claude`` restatement
    is as traceable as an admin one.
    """
    if plan_type not in ('uniform', 'layby'):
        raise ValueError("plan_type must be 'uniform' or 'layby' "
                         "(undercharges are edited via reschedule_undercharge).")
    table = _TABLES[plan_type]

    allowed = {'description', 'sale_number', 'notes', 'total', 'monthly',
               'balance_remaining', 'term_months', 'payments_made',
               'start_year', 'start_month'}
    allowed |= {'sku'} if plan_type == 'uniform' else {'basket_total', 'discount_pct'}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError("Not editable on a {} plan: {}.".format(
            plan_type, ", ".join(sorted(unknown))))

    plan = conn.execute(
        "SELECT * FROM {} WHERE id=?".format(table), (plan_id,)).fetchone()
    if plan is None:
        raise ValueError("{} plan {} not found.".format(plan_type, plan_id))
    _, full_name, sector = require_employee(conn, plan['employee_id'])

    given = {k: v for k, v in fields.items() if v is not None}

    def _num(key, column, cast, current=None):
        """The new value for a numeric field, or its unchanged current value."""
        cur = plan[column] if current is None else current
        if key not in given:
            return cur
        try:
            return cast(given[key])
        except (TypeError, ValueError):
            raise ValueError("{} must be a number.".format(key.replace('_', ' ').capitalize()))

    def _text(key, column):
        return (str(given[key]).strip() if key in given
                else (plan[column] if plan[column] is not None else ''))

    total = _num('total', 'total_amount', float)
    monthly = _num('monthly', 'monthly_amount', float)
    balance = _num('balance_remaining', 'balance_remaining', float,
                   current=(plan['balance_remaining'] if plan['balance_remaining']
                            is not None else plan['total_amount']))
    term = _num('term_months', 'term_months', int)
    payments_made = _num('payments_made', 'payments_made', int)
    start_year = _num('start_year', 'start_year', int)
    start_month = _num('start_month', 'start_month', int)

    if monthly is None or monthly <= 0 or term is None or term <= 0:
        raise ValueError("Amount and term must be greater than zero, and values "
                         "cannot be negative.")
    if (total or 0) < 0 or (payments_made or 0) < 0:
        raise ValueError("Amount and term must be greater than zero, and values "
                         "cannot be negative.")

    start_year, start_month = _valid_period(start_year, start_month)
    moved = (start_year, start_month) != (plan['start_year'], plan['start_month'])
    if moved:
        _require_unlocked(start_year, start_month, sector,
                          label="new starting payroll period")

    balance = round(max(0, balance or 0), 2)
    # Mirrors the routes' derivation, except that a written-off plan stays written
    # off — an edit must not quietly make someone owe a balance again.
    if plan['status'] == 'written_off':
        status = 'written_off'
    else:
        status = 'complete' if (payments_made >= term or balance <= 0.01) else 'active'

    sets = {'total_amount_cents': db.to_cents(total or 0),
            'monthly_amount_cents': db.to_cents(monthly),
            'balance_remaining_cents': db.to_cents(balance),
            'term_months': term, 'payments_made': payments_made,
            'start_year': start_year, 'start_month': start_month,
            'status': status,
            'description': _text('description', 'description'),
            'sale_number': _text('sale_number', 'sale_number'),
            'notes': _text('notes', 'notes')}
    if plan_type == 'uniform':
        sets['sku'] = _text('sku', 'sku')
    else:
        basket_total = round(_num('basket_total', 'basket_total', float) or 0, 2)
        discount_pct = _num('discount_pct', 'discount_pct', float) or 0
        if discount_pct < 0 or basket_total < 0:
            raise ValueError("Basket total and discount cannot be negative.")
        sets['basket_total_cents'] = db.to_cents(basket_total)
        sets['discount_pct'] = discount_pct

    conn.execute(
        "UPDATE {}_cents SET {} WHERE id=?".format(
            table, ", ".join("{}=?".format(c) for c in sets)),
        list(sets.values()) + [plan_id])

    # A lay-by's basket rows are display-only, but they must not contradict the
    # header. Only touched when the basket itself was edited, so an unrelated edit
    # (a note, a term) leaves a multi-item basket intact. When it IS edited the
    # basket collapses to one line, as the form has always done — but by replacing
    # the rows, not by overwriting every row with the full basket total (which left
    # an N-item lay-by showing N x the basket).
    if plan_type == 'layby' and ({'basket_total', 'description'} & set(given)):
        desc = sets['description'] or 'Lay-by Item'
        conn.execute("DELETE FROM layby_items_cents WHERE layby_id=?", (plan_id,))
        conn.execute(
            "INSERT INTO layby_items_cents "
            "(layby_id, description, unit_price_cents, quantity, line_total_cents) "
            "VALUES (?,?,?,1,?)",
            (plan_id, desc, sets['basket_total_cents'], sets['basket_total_cents']))

    old_balance = (plan['balance_remaining'] if plan['balance_remaining'] is not None
                   else plan['total_amount']) or 0
    # key -> (column on the Rands view, new value) for everything editable, so the
    # before/after is read off one place rather than re-derived per field.
    new_values = {'total': ('total_amount', round(total or 0, 2)),
                  'monthly': ('monthly_amount', round(monthly, 2)),
                  'balance_remaining': ('balance_remaining', balance),
                  'term_months': ('term_months', term),
                  'payments_made': ('payments_made', payments_made),
                  'start_year': ('start_year', start_year),
                  'start_month': ('start_month', start_month),
                  'description': ('description', sets['description']),
                  'sale_number': ('sale_number', sets['sale_number']),
                  'notes': ('notes', sets['notes'])}
    if plan_type == 'uniform':
        new_values['sku'] = ('sku', sets['sku'])
    else:
        new_values['basket_total'] = ('basket_total', basket_total)
        new_values['discount_pct'] = ('discount_pct', discount_pct)

    changes = {}
    for key in given:
        column, new = new_values[key]
        if new != plan[column]:
            changes[key] = {"from": plan[column], "to": new}

    conn.execute(
        "INSERT INTO plan_adjustments_cents "
        "(plan_type, plan_id, amount_cents, note, new_monthly_cents, actor) "
        "VALUES (?,?,?,?,?,?)",
        (plan_type, plan_id, db.to_cents(round(old_balance - balance, 2)),
         "Plan edited: {}".format(", ".join(sorted(changes)) or "no field changed"),
         db.to_cents(monthly), actor or 'admin'))

    return {"plan_type": plan_type, "plan_id": plan_id,
            "employee_id": plan['employee_id'], "employee_name": full_name,
            "sector": sector, "total": round(total or 0, 2),
            "monthly": round(monthly, 2), "balance_remaining": balance,
            "term_months": term, "payments_made": payments_made,
            "start_year": start_year, "start_month": start_month,
            "status": status, "changes": changes, "actor": actor or 'admin',
            "schedule": project_schedule(start_year, start_month, balance,
                                         monthly, max(term - payments_made, 0))}


def write_off_plan(conn, plan_type, plan_id, reason=None, actor=None):
    """Write off a plan's remaining balance. Mirrors the three /write-off routes.

    Uniform and lay-by are a status flip. An **undercharge** additionally records a
    `write_off` event for the remaining cents and cancels its unpaid scheduled
    installments, so the ledger and the schedule agree with the status.
    """
    if plan_type not in PLAN_TYPES:
        raise ValueError("plan_type must be one of: " + ", ".join(PLAN_TYPES))
    table = _TABLES[plan_type]

    plan = conn.execute(
        "SELECT * FROM {} WHERE id=?".format(table), (plan_id,)).fetchone()
    if plan is None:
        raise ValueError("{} plan {} not found.".format(plan_type, plan_id))
    if plan['status'] == 'written_off':
        return {"plan_type": plan_type, "plan_id": plan_id,
                "employee_id": plan['employee_id'], "status": "written_off",
                "written_off": 0, "note": "already written off"}

    written_off_cents = 0
    if plan_type == 'undercharge' and (plan['type'] or 'undercharge') == 'undercharge':
        account = db.get_undercharge_account(plan_id, conn)
        written_off_cents = max(account['remaining_cents'], 0)
        if written_off_cents > 0:
            db.record_undercharge_event(
                conn, plan_id, 'write_off', written_off_cents,
                note=reason or 'Remaining balance written off',
                actor=actor or 'admin')
        conn.execute(
            "UPDATE undercharge_schedule_items SET state='cancelled',"
            "state_reason='Remaining balance written off',"
            "state_changed_at=datetime('now') WHERE undercharge_id=? "
            "AND state='scheduled' AND transaction_id IS NULL", (plan_id,))
    elif plan_type in ('uniform', 'layby'):
        written_off_cents = db.to_cents(plan['balance_remaining']) or 0

    conn.execute(
        "UPDATE {}_cents SET status='written_off' WHERE id=?".format(table),
        (plan_id,))

    return {"plan_type": plan_type, "plan_id": plan_id,
            "employee_id": plan['employee_id'], "status": "written_off",
            "written_off": money.to_rands(written_off_cents),
            "reason": reason or None, "actor": actor or 'admin'}
