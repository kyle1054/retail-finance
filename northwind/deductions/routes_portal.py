"""Staff-facing portal (read-only, authenticated at store level).

After login via /staff/login (store email + shared store password), the session
stores the store name. The portal shows that store's active staff and each
person's deductions — and nothing from any other store.
"""
from datetime import datetime

from flask import render_template, redirect, url_for, flash, session
from northwind.data import database as db
from northwind.core import app
from northwind.deductions import requests as reqs

ACTIVE_STATUSES = {'active', 'pending', 'partial'}


def _add_months(year, month, n):
    """Return (year, month) n whole months after the given start."""
    total = month + n
    return (year + (total - 1) // 12, ((total - 1) % 12) + 1)


def _period_label(year, month):
    if not year or not month:
        return '—'
    return f"{db.MONTH_NAMES[month]} {year}"


def _employee_summary(emp_id):
    """Build normalised active/history deduction views for one employee."""
    conn = db.get_db()
    emp = conn.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if not emp:
        conn.close()
        return None

    uniforms = conn.execute(
        "SELECT * FROM uniform_deductions WHERE employee_id=? ORDER BY start_year, start_month, id",
        (emp_id,)).fetchall()
    laybys = conn.execute(
        "SELECT * FROM layby_deductions WHERE employee_id=? ORDER BY start_year, start_month, id",
        (emp_id,)).fetchall()
    undercharges = conn.execute(
        "SELECT * FROM undercharges WHERE employee_id=? ORDER BY incident_year, incident_month, id",
        (emp_id,)).fetchall()
    # Their own requests (any status) and whether they may raise another one.
    requests = reqs.list_requests(conn, status='all', employee_id=emp_id, limit=20)
    can_request = reqs.open_request_count(conn, emp_id) < reqs.MAX_OPEN_PER_EMPLOYEE
    conn.close()

    active, history, reimbursements = [], [], []

    def file_plan(view):
        (active if view['is_active'] else history).append(view)

    for r in uniforms:
        total = r['total_amount'] if r['total_amount'] is not None \
            else round(r['term_months'] * r['monthly_amount'], 2)
        remaining = db.calc_uniform_balance(r) if r['status'] == 'active' else 0.0
        end_y, end_m = _add_months(r['start_year'], r['start_month'], r['term_months'] - 1)
        file_plan({
            'category': 'Uniform',
            'icon': 'bi-bag-fill',
            'description': r['description'] or r['sku'] or 'Uniform',
            'monthly': r['monthly_amount'],
            'total': round(total, 2),
            'remaining': round(remaining, 2),
            'paid': round(total - remaining, 2),
            'payments_made': r['payments_made'],
            'term': r['term_months'],
            'start': _period_label(r['start_year'], r['start_month']),
            'expected_end': _period_label(end_y, end_m),
            'status': r['status'],
            'is_active': r['status'] == 'active',
        })

    for r in laybys:
        total = r['total_amount'] or 0
        remaining = (r['balance_remaining'] if r['balance_remaining'] is not None
                     else (r['term_months'] - r['payments_made']) * r['monthly_amount']) \
            if r['status'] == 'active' else 0.0
        end_y, end_m = _add_months(r['start_year'], r['start_month'], r['term_months'] - 1)
        file_plan({
            'category': 'Lay-by',
            'icon': 'bi-cart-fill',
            'description': r['description'] or (f"Sale {r['sale_number']}" if r['sale_number'] else 'Lay-by'),
            'monthly': r['monthly_amount'],
            'total': round(total, 2),
            'remaining': round(remaining, 2),
            'paid': round(total - remaining, 2),
            'payments_made': r['payments_made'],
            'term': r['term_months'],
            'start': _period_label(r['start_year'], r['start_month']),
            'expected_end': _period_label(end_y, end_m),
            'status': r['status'],
            'is_active': r['status'] == 'active',
        })

    for r in undercharges:
        if r['type'] == 'overcharge':
            reimbursements.append({
                'description': r['reason'] or 'Reimbursement',
                'total': round(r['total_amount'], 2),
                'status': r['status'],
                'period': _period_label(
                    r['reimburse_year'] or r['incident_year'],
                    r['reimburse_month'] or r['incident_month']),
            })
            continue

        start_y = r['start_year'] if r['start_year'] is not None else r['incident_year']
        start_m = r['start_month'] if r['start_month'] is not None else r['incident_month']
        total = r['total_amount']
        if r['recovery_method'] == 'full':
            term, monthly = 1, total
            end_y, end_m = start_y, start_m
            remaining = total if r['status'] in ACTIVE_STATUSES else 0.0
        else:
            term = r['split_months'] or 1
            monthly = total / term
            end_y, end_m = _add_months(start_y, start_m, term - 1)
            remaining = monthly * (term - r['payments_made']) if r['status'] in ACTIVE_STATUSES else 0.0
        file_plan({
            'category': 'Undercharge',
            'icon': 'bi-exclamation-circle-fill',
            'description': r['reason'] or (f"Sale {r['sale_number']}" if r['sale_number'] else 'Undercharge'),
            'monthly': round(monthly, 2),
            'total': round(total, 2),
            'remaining': round(remaining, 2),
            'paid': round(total - remaining, 2),
            'payments_made': r['payments_made'],
            'term': term,
            'start': _period_label(start_y, start_m),
            'expected_end': _period_label(end_y, end_m),
            'status': r['status'],
            'is_active': r['status'] in ACTIVE_STATUSES,
        })

    # "What comes off my next payslip, and when am I done" — the two questions
    # staff actually ask, and the page could not answer either. Months that fell
    # due and were never collected count too: dropping them would understate the
    # balance and promise an earlier clear date than the ledger supports.
    now = datetime.now()
    owing_months = [m for m in db.get_employee_schedule(emp_id)
                    if (m['total'] - m['total_paid']) > 0.005]
    upcoming = [m for m in owing_months
                if (m['year'], m['month']) >= (now.year, now.month)]

    def _month(row):
        if not row:
            return None
        return {'label': '%s %s' % (db.MONTH_FULL[row['month']], row['year']),
                'due': round(row['total'] - row['total_paid'], 2)}

    return {
        'emp': emp,
        'requests': requests,
        'can_request': can_request,
        'active': active,
        'history': history,
        'reimbursements': reimbursements,
        'outstanding': db.get_outstanding_summary(emp_id),
        'next_due': _month(upcoming[0] if upcoming else None),
        'clear_after': _month(owing_months[-1] if owing_months else None),
        # Anything overdue is money the ledger still wants but payroll never
        # took, so it must not read as "coming up".
        'overdue': [_month(m) for m in owing_months if m not in upcoming],
    }


# ── Authenticated staff portal (store-level) ─────────────────────────────────

@app.route('/portal/me')
def portal_me():
    """Legacy bookmark from the per-employee portal — now store-level."""
    return redirect(url_for('portal_store'))


@app.route('/portal/store')
def portal_store():
    store = session.get('staff_store')
    if not store:
        return redirect(url_for('staff_login'))
    staff = db.get_store_portal_data(store)
    return render_template('portal_store.html', store=store, staff=staff)


@app.route('/portal/store/<emp_id>')
def portal_store_employee(emp_id):
    store = session.get('staff_store')
    if not store:
        return redirect(url_for('staff_login'))
    summary = _employee_summary(emp_id)
    # A store session may only open profiles of its own staff.
    if summary is None or summary['emp']['current_store'] != store:
        flash('Employee not found at your store.', 'danger')
        return redirect(url_for('portal_store'))
    return render_template('portal_employee.html', **summary)
