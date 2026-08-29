"""Recent Activity — a cross-category feed of newly captured deductions.

Answers "what has been added lately?" across uniforms, lay-bys and
undercharges/overcharges in both sectors, newest first, grouped by day.
Each entry links to the owning employee; lay-bys expand inline to show
their basket items via the existing /layby/<id>/items JSON endpoint.
"""
from flask import render_template, request
from northwind.data import database as db
from northwind.core import app, employee_detail_url, HQ_STORES, current_admin_sector

VALID_DAYS = (7, 30, 90, 365)
VALID_CATS = ('all', 'uniform', 'layby', 'undercharge', 'overcharge')


def _recent_entries(conn, days, store, cat, sector=None):
    """Unified newest-first list of plans created in the last `days` days.

    `sector` ('retail'/'hq') scopes the feed for a sector-limited admin; None
    (super admin) shows both."""
    cutoff = (f'-{days} days',)
    store_sql, store_params = '', ()
    if store:
        store_sql += ' AND e.current_store = ?'
        store_params += (store,)
    if sector:
        store_sql += ' AND e.sector = ?'
        store_params += (sector,)
    entries = []

    if cat in ('all', 'uniform'):
        rows = conn.execute(
            "SELECT x.id, x.sale_number, x.description, x.total_amount, "
            "       x.monthly_amount, x.term_months, x.status, x.created_at, "
            "       e.id AS emp_id, e.full_name, e.current_store, e.sector "
            "FROM uniform_deductions x JOIN employees e ON e.id = x.employee_id "
            "WHERE x.created_at >= datetime('now', ?)" + store_sql,
            cutoff + store_params).fetchall()
        for r in rows:
            entries.append({
                'kind': 'Uniform', 'icon': 'bi-bag-fill', 'color': 'amber',
                'plan_id': r['id'], 'sale_number': r['sale_number'] or '',
                'description': r['description'] or 'Uniform',
                'total': r['total_amount'] or (r['term_months'] * r['monthly_amount']),
                'monthly': r['monthly_amount'], 'term': r['term_months'],
                'status': r['status'], 'created_at': r['created_at'],
                'emp_id': r['emp_id'], 'full_name': r['full_name'],
                'store': r['current_store'], 'sector': r['sector'],
                'url': employee_detail_url(r['emp_id'], r['sector']),
            })

    if cat in ('all', 'layby'):
        rows = conn.execute(
            "SELECT x.id, x.sale_number, x.description, x.total_amount, "
            "       x.monthly_amount, x.term_months, x.status, x.created_at, "
            "       x.basket_total, x.discount_pct, x.balance_remaining, "
            "       e.id AS emp_id, e.full_name, e.current_store, e.sector "
            "FROM layby_deductions x JOIN employees e ON e.id = x.employee_id "
            "WHERE x.created_at >= datetime('now', ?)" + store_sql,
            cutoff + store_params).fetchall()
        for r in rows:
            entries.append({
                'kind': 'Lay-by', 'icon': 'bi-cart-fill', 'color': 'teal',
                'plan_id': r['id'], 'sale_number': r['sale_number'] or '',
                'description': r['description'] or 'Lay-by',
                'total': r['total_amount'] or 0,
                'monthly': r['monthly_amount'], 'term': r['term_months'],
                'status': r['status'], 'created_at': r['created_at'],
                'basket_total': r['basket_total'], 'discount_pct': r['discount_pct'],
                'balance': r['balance_remaining'],
                'emp_id': r['emp_id'], 'full_name': r['full_name'],
                'store': r['current_store'], 'sector': r['sector'],
                'url': employee_detail_url(r['emp_id'], r['sector']),
                'expandable': True,
            })

    if cat in ('all', 'undercharge', 'overcharge'):
        type_sql = ''
        type_params = ()
        if cat != 'all':
            type_sql = " AND x.type = ?"
            type_params = (cat,)
        rows = conn.execute(
            "SELECT x.id, x.sale_number, x.reason, x.total_amount, x.status, "
            "       x.type, x.recovery_method, x.split_months, x.created_at, "
            "       e.id AS emp_id, e.full_name, e.current_store, e.sector "
            "FROM undercharges x JOIN employees e ON e.id = x.employee_id "
            "WHERE x.created_at >= datetime('now', ?)" + store_sql + type_sql,
            cutoff + store_params + type_params).fetchall()
        for r in rows:
            is_over = r['type'] == 'overcharge'
            split = r['split_months'] or 1
            entries.append({
                'kind': 'Overcharge' if is_over else 'Undercharge',
                'icon': 'bi-arrow-counterclockwise' if is_over else 'bi-exclamation-circle-fill',
                'color': 'blue' if is_over else 'red',
                'plan_id': r['id'], 'sale_number': r['sale_number'] or '',
                'description': r['reason'] or ('Overcharge' if is_over else 'Undercharge'),
                'total': r['total_amount'],
                'monthly': (r['total_amount'] / split) if r['recovery_method'] == 'split' else None,
                'term': split if r['recovery_method'] == 'split' else 1,
                'status': r['status'], 'created_at': r['created_at'],
                'emp_id': r['emp_id'], 'full_name': r['full_name'],
                'store': r['current_store'], 'sector': r['sector'],
                'url': employee_detail_url(r['emp_id'], r['sector']),
            })

    entries.sort(key=lambda d: d['created_at'] or '', reverse=True)
    return entries


@app.route('/activity')
def recent_activity():
    days = request.args.get('days', 30, type=int)
    if days not in VALID_DAYS:
        days = 30
    store = request.args.get('store', '')
    cat = request.args.get('cat', 'all')
    if cat not in VALID_CATS:
        cat = 'all'

    sector = current_admin_sector()
    conn = db.get_db()
    try:
        entries = _recent_entries(conn, days, store, cat, sector)
        stores = [r['name'] for r in conn.execute(
            "SELECT name FROM stores ORDER BY name").fetchall()]
    finally:
        conn.close()

    # Group newest-first entries by calendar day for display.
    day_groups = []
    for e in entries:
        day = (e['created_at'] or '')[:10] or 'Unknown'
        if not day_groups or day_groups[-1]['day'] != day:
            day_groups.append({'day': day, 'entries': [], 'total': 0.0})
        day_groups[-1]['entries'].append(e)
        day_groups[-1]['total'] += e['total'] or 0

    summary = {
        'count': len(entries),
        'value': sum(e['total'] or 0 for e in entries),
        'laybys': sum(1 for e in entries if e['kind'] == 'Lay-by'),
        'uniforms': sum(1 for e in entries if e['kind'] == 'Uniform'),
        'undercharges': sum(1 for e in entries if e['kind'] in ('Undercharge', 'Overcharge')),
    }

    # Store filter list mirrors the admin's sector scope.
    if sector == 'hq':
        store_options = list(HQ_STORES)
    elif sector == 'retail':
        store_options = stores
    else:
        store_options = stores + HQ_STORES
    return render_template('activity.html', day_groups=day_groups, summary=summary,
                           days=days, store_filter=store, cat_filter=cat,
                           stores=store_options, valid_days=VALID_DAYS)
