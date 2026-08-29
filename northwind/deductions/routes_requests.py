"""Staff requests — the portal side (staff ask) and the admin queue (we deliver).

Staff pick their own name in the portal and ask for a uniform or a lay-by; the ask
lands in /requests, where an admin picks it up, questions it if needed, and either
declines it or turns it into a real deduction plan. All workflow and validation lives
in northwind/deductions/requests.py; these handlers only parse forms and flash messages.

There is deliberately NO automated email: every decision screen offers a copy-ready
summary to paste into WhatsApp, and the status is always visible in the portal, so
nothing depends on a message being delivered.
"""
from datetime import datetime

from flask import (render_template, request, redirect, url_for, flash, session,
                   abort)
from northwind.data import database as db
from northwind.core import app, employee_detail_url, current_admin_sector
from northwind.deductions import requests as reqs
from northwind.deductions.pagination import paginate

# Item rows offered on the staff form. Blank rows are ignored, so this is just
# "how many things can you ask for in one go" — four compact rows cover a uniform
# order without needing any JavaScript to add more (the portal ships no Bootstrap JS).
ITEM_ROWS = 4


def _collect_items(form, rows=None):
    """Read indexed item_* fields off a submitted form."""
    items, i = [], 0
    limit = rows if rows is not None else reqs.MAX_ITEMS
    while i < limit:
        if f'item_desc_{i}' not in form:
            break
        items.append({'description': form.get(f'item_desc_{i}', ''),
                      'sku': form.get(f'item_sku_{i}', ''),
                      'size': form.get(f'item_size_{i}', ''),
                      'unit_price': form.get(f'item_price_{i}', ''),
                      'quantity': form.get(f'item_qty_{i}', 1) or 1})
        i += 1
    return items


# ── Staff portal ─────────────────────────────────────────────────────────────
def _portal_employee(emp_id):
    """The employee row, but only if the logged-in store owns them."""
    store = session.get('staff_store')
    if not store:
        return None
    row = db.get_db().execute(
        "SELECT id, full_name, job_title, current_store FROM employees WHERE id=?",
        (emp_id,)).fetchone()
    if row is None or row['current_store'] != store:
        return None
    return row


def _portal_request(req_id):
    """A request, but only if it belongs to someone at the logged-in store *now*.

    Scoped on the employee's CURRENT store, not the store stamped on the request:
    staff transfer, and the snapshot would have left the old store able to open a
    moved colleague's request while their new store could not.
    """
    store = session.get('staff_store')
    if not store:
        return None
    req = reqs.get_request(db.get_db(), req_id)
    if req is None:
        return None
    current = db.get_db().execute(
        "SELECT current_store FROM employees WHERE id=?", (req['employee_id'],)).fetchone()
    if current is None or current['current_store'] != store:
        return None
    return req


@app.route('/portal/store/<emp_id>/request/<kind>', methods=['GET', 'POST'])
def portal_request_new(emp_id, kind):
    if kind not in reqs.KINDS:
        abort(404)
    emp = _portal_employee(emp_id)
    if emp is None:
        flash('Employee not found at your store.', 'danger')
        return redirect(url_for('portal_store'))

    if request.method == 'POST':
        conn = db.get_db()
        try:
            with conn:
                created = reqs.create_request(
                    conn, emp_id, kind,
                    items=_collect_items(request.form, ITEM_ROWS),
                    term=request.form.get('term_months') or None,
                    notes=request.form.get('notes', ''),
                    actor='employee:%s' % emp_id, actor_role='staff',
                    created_via='portal', require_sku=True)
        except ValueError as exc:
            flash(str(exc), 'danger')
            return render_template('portal_request_new.html', emp=emp, kind=kind,
                                   kind_label=reqs.KIND_LABELS[kind],
                                   item_rows=range(ITEM_ROWS),
                                   max_term=reqs.MAX_TERM_MONTHS,
                                   form=request.form)
        finally:
            conn.close()
        flash('Request %s sent. We will come back to you here.' % created['ref'], 'success')
        return redirect(url_for('portal_request_detail', req_id=created['id']))

    return render_template('portal_request_new.html', emp=emp, kind=kind,
                           kind_label=reqs.KIND_LABELS[kind],
                           item_rows=range(ITEM_ROWS),
                           max_term=reqs.MAX_TERM_MONTHS, form={})


@app.route('/portal/request/<int:req_id>')
def portal_request_detail(req_id):
    req = _portal_request(req_id)
    if req is None:
        flash('Request not found.', 'danger')
        return redirect(url_for('portal_store'))
    return render_template('portal_request.html', req=req,
                           can_cancel=req['status'] in reqs.STAFF_CANCELLABLE)


@app.route('/portal/request/<int:req_id>/comment', methods=['POST'])
def portal_request_comment(req_id):
    req = _portal_request(req_id)
    if req is None:
        flash('Request not found.', 'danger')
        return redirect(url_for('portal_store'))
    conn = db.get_db()
    try:
        with conn:
            reqs.add_comment(conn, req_id, request.form.get('message', ''),
                             actor='employee:%s' % req['employee_id'],
                             actor_role='staff')
        flash('Reply sent.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    finally:
        conn.close()
    return redirect(url_for('portal_request_detail', req_id=req_id))


@app.route('/portal/request/<int:req_id>/cancel', methods=['POST'])
def portal_request_cancel(req_id):
    req = _portal_request(req_id)
    if req is None:
        flash('Request not found.', 'danger')
        return redirect(url_for('portal_store'))
    conn = db.get_db()
    try:
        with conn:
            reqs.set_status(conn, req_id, 'cancelled',
                            actor='employee:%s' % req['employee_id'],
                            actor_role='staff',
                            message=request.form.get('message', ''))
        flash('Request cancelled.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    finally:
        conn.close()
    return redirect(url_for('portal_store_employee', emp_id=req['employee_id']))


# ── Admin queue ──────────────────────────────────────────────────────────────
@app.route('/requests')
def requests_list():
    status = request.args.get('status', 'open')
    if status not in reqs.STATUSES and status not in ('open', 'all'):
        status = 'open'
    kind = request.args.get('kind', '')
    store = request.args.get('store', '')
    conn = db.get_db()
    try:
        sector = current_admin_sector()
        rows = reqs.list_requests(conn, status=status, kind=kind or None,
                                  store=store or None, sector=sector,
                                  with_thread=True)
        counts = reqs.counts_by_status(conn, sector)
    finally:
        conn.close()
    # Every row carries its own forms and thread, so an unpaged queue is heavy
    # long before it is unreadable — 50 requests rendered 560 KB of markup.
    rows, pager = paginate(rows, noun='requests', per_page=25)
    now = datetime.now()
    return render_template('requests.html', requests=rows, counts=counts, pager=pager,
                           status=status, kind=kind, store=store,
                           statuses=reqs.STATUSES,
                           status_labels=reqs.STATUS_LABELS,
                           kinds=reqs.KINDS, kind_labels=reqs.KIND_LABELS,
                           max_term=reqs.MAX_TERM_MONTHS, MONTH_NAMES=db.MONTH_NAMES,
                           default_year=now.year, default_month=now.month)


def _back_to_queue():
    return redirect(request.referrer or url_for('requests_list'))


def _actor():
    return session.get('admin_username') or session.get('admin_display_name') or 'admin'


@app.route('/requests/<int:req_id>/claim', methods=['POST'])
def request_claim(req_id):
    conn = db.get_db()
    try:
        with conn:
            reqs.claim(conn, req_id, _actor())
        flash('Request picked up — it is yours to finish.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    finally:
        conn.close()
    return _back_to_queue()


@app.route('/requests/<int:req_id>/comment', methods=['POST'])
def request_comment(req_id):
    conn = db.get_db()
    try:
        with conn:
            reqs.add_comment(conn, req_id, request.form.get('message', ''),
                             actor=_actor(), actor_role='admin',
                             ask_for_info=bool(request.form.get('ask_for_info')))
        flash('Message added to the request.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    finally:
        conn.close()
    return _back_to_queue()


@app.route('/requests/<int:req_id>/status', methods=['POST'])
def request_set_status(req_id):
    to_status = request.form.get('status', '')
    conn = db.get_db()
    try:
        with conn:
            reqs.set_status(conn, req_id, to_status, actor=_actor(),
                            actor_role='admin',
                            message=request.form.get('message', ''))
        flash('Request marked %s.' % reqs.STATUS_LABELS.get(to_status, to_status).lower(),
              'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    finally:
        conn.close()
    return _back_to_queue()


@app.route('/requests/<int:req_id>/convert', methods=['POST'])
def request_convert(req_id):
    conn = db.get_db()
    result = None
    try:
        with conn:
            result = reqs.convert_to_plan(
                conn, req_id, actor=_actor(),
                start_year=request.form.get('start_year'),
                start_month=request.form.get('start_month'),
                term=request.form.get('term_months'),
                items=_collect_items(request.form) or None,
                total=request.form.get('total_amount') or None,
                monthly=request.form.get('monthly_amount') or None,
                sku=request.form.get('sku', ''),
                sale_number=request.form.get('sale_number', ''),
                discount_pct=request.form.get('discount_pct', 40) or 40,
                notes=request.form.get('notes') or None)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return _back_to_queue()
    finally:
        conn.close()
    flash('%s plan created for %s — R%.2f over %d month%s from %s %d.' % (
        reqs.KIND_LABELS[result['plan_type']], result['employee_name'],
        result['total'], result['term_months'],
        '' if result['term_months'] == 1 else 's',
        db.MONTH_NAMES[result['start_month']], result['start_year']), 'success')
    return redirect(employee_detail_url(result['employee_id'], result['sector']))
