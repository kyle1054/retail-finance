"""Invoice / sale-number search.

Finds any deduction plan (uniform, lay-by, undercharge/overcharge) by its
sale or invoice number — the `sale_number` field — across both the Retail and
HQ sectors, and links straight to the owning employee's profile.
"""
from flask import render_template, request, jsonify, url_for
from northwind.data import database as db
from northwind.core import app, employee_detail_url, current_admin_sector


def _search_sale_number(conn, q, sector=None):
    """Return matching plans across all three deduction types, newest first.

    `sector` ('retail'/'hq') scopes results for a sector-limited admin; None
    (super admin) searches both."""
    like = f'%{q}%'
    # Optional sector scope appended to every query's WHERE clause.
    sec_sql, sec_params = ('', ())
    if sector:
        sec_sql, sec_params = (' AND e.sector = ?', (sector,))
    results = []

    uniform = conn.execute(
        "SELECT x.id, x.sale_number, x.description, x.total_amount, x.status, "
        "       x.payments_made, x.term_months, x.created_at, "
        "       e.id AS emp_id, e.full_name, e.current_store, e.sector "
        "FROM uniform_deductions x JOIN employees e ON e.id = x.employee_id "
        "WHERE x.sale_number LIKE ?" + sec_sql, (like,) + sec_params).fetchall()
    for r in uniform:
        results.append({
            'kind': 'Uniform', 'badge': 'primary', 'sale_number': r['sale_number'],
            'description': r['description'] or '', 'total_amount': r['total_amount'],
            'status': r['status'], 'progress': f"{r['payments_made']}/{r['term_months']}",
            'created_at': r['created_at'], 'emp_id': r['emp_id'],
            'full_name': r['full_name'], 'current_store': r['current_store'],
            'url': employee_detail_url(r['emp_id'], r['sector']),
        })

    layby = conn.execute(
        "SELECT x.id, x.sale_number, x.description, x.total_amount, x.status, "
        "       x.payments_made, x.term_months, x.created_at, "
        "       e.id AS emp_id, e.full_name, e.current_store, e.sector "
        "FROM layby_deductions x JOIN employees e ON e.id = x.employee_id "
        "WHERE x.sale_number LIKE ?" + sec_sql, (like,) + sec_params).fetchall()
    for r in layby:
        results.append({
            'kind': 'Lay-by', 'badge': 'info', 'sale_number': r['sale_number'],
            'description': r['description'] or '', 'total_amount': r['total_amount'],
            'status': r['status'], 'progress': f"{r['payments_made']}/{r['term_months']}",
            'created_at': r['created_at'], 'emp_id': r['emp_id'],
            'full_name': r['full_name'], 'current_store': r['current_store'],
            'url': employee_detail_url(r['emp_id'], r['sector']),
        })

    uc = conn.execute(
        "SELECT x.id, x.sale_number, x.reason, x.total_amount, x.status, "
        "       x.payments_made, x.split_months, x.recovery_method, x.type, x.created_at, "
        "       e.id AS emp_id, e.full_name, e.current_store, e.sector "
        "FROM undercharges x JOIN employees e ON e.id = x.employee_id "
        "WHERE x.sale_number LIKE ?" + sec_sql, (like,) + sec_params).fetchall()
    for r in uc:
        is_over = r['type'] == 'overcharge'
        account = None if is_over else db.get_undercharge_account(r['id'], conn)
        progress = ('—' if is_over else
                    f"{account['payment_count']} paid · "
                    f"R{db.to_rands(account['remaining_cents']):.2f} left")
        results.append({
            'kind': 'Overcharge' if is_over else 'Undercharge',
            'badge': 'success' if is_over else 'warning',
            'sale_number': r['sale_number'], 'description': r['reason'] or '',
            'total_amount': r['total_amount'],
            'status': account['status'] if account else r['status'],
            'progress': progress, 'created_at': r['created_at'], 'emp_id': r['emp_id'],
            'full_name': r['full_name'], 'current_store': r['current_store'],
            'url': employee_detail_url(r['emp_id'], r['sector']),
        })

    # Newest first; rows without a created_at sort last.
    results.sort(key=lambda d: d['created_at'] or '', reverse=True)
    return results


@app.route('/invoice-search')
def invoice_search():
    q = request.args.get('q', '').strip()
    results = []
    if q:
        conn = db.get_db()
        try:
            results = _search_sale_number(conn, q, current_admin_sector())
        finally:
            conn.close()
    return render_template('invoice_search.html', q=q, results=results)


@app.route('/search')
def global_search():
    """Live JSON search for the top-nav jump box — finds people, stores and cards
    by name (not sale numbers; that's /invoice-search). Sector-scoped exactly like
    _search_sale_number: a retail/hq admin only sees their sector's employees, and
    stores + credit cards (both retail concepts) are hidden from HQ admins."""
    q = request.args.get('q', '').strip()
    out = {'employees': [], 'stores': [], 'cards': []}
    if len(q) < 2:
        return jsonify(out)

    sector = current_admin_sector()
    like = f'%{q}%'
    conn = db.get_db()
    try:
        # Employees — name or id, newest-status-first isn't meaningful here so
        # order by name. Sector scope for limited admins; both sectors for super.
        sql = ("SELECT id, full_name, current_store, job_title, status, sector "
               "FROM employees WHERE (full_name LIKE ? OR id LIKE ?)")
        params = [like, like]
        if sector:
            sql += " AND sector = ?"
            params.append(sector)
        sql += " ORDER BY (status='active') DESC, full_name COLLATE NOCASE LIMIT 8"
        for r in conn.execute(sql, params).fetchall():
            out['employees'].append({
                'label': r['full_name'],
                'sub': ' · '.join(x for x in (r['current_store'], r['job_title']) if x)
                       + ('' if r['status'] == 'active' else ' · (inactive)'),
                'url': employee_detail_url(r['id'], r['sector']),
            })
    finally:
        conn.close()

    # Stores are retail-side (the /employees list a retail or super admin can
    # reach) — hide them from an HQ-scoped admin.
    if sector != 'hq':
        ql = q.lower()
        for name in db.get_stores():
            if ql in name.lower():
                out['stores'].append({
                    'label': name, 'sub': 'Store',
                    'url': url_for('employees', store=name),
                })
                if len(out['stores']) >= 8:
                    break

    # Credit Card Reconciliation is a super-only section (admin_endpoint_allowed
    # blocks every 'cc_' endpoint for scoped admins), so only offer cards to a
    # super admin — otherwise we'd link a retail admin to a page they can't open.
    if sector is None:
        ql = q.lower()
        for c in db.list_cc_cards():
            hay = f"{c['display_name'] or ''} {c['card_name'] or ''}".lower()
            if ql in hay:
                out['cards'].append({
                    'label': c['display_name'] or c['card_name'],
                    'sub': 'Credit card',
                    'url': url_for('cc_card', card_id=c['id']),
                })
                if len(out['cards']) >= 8:
                    break

    return jsonify(out)
