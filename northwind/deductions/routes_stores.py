from flask import render_template, request, redirect, url_for, flash
from datetime import datetime
from northwind.data import database as db
from northwind.core import app


@app.route('/stores/add', methods=['POST'])
def add_store():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Store name cannot be empty.', 'danger')
        return redirect(url_for('stores_report'))
    conn = db.get_db()
    try:
        conn.execute("INSERT INTO stores (name) VALUES (?)", (name,))
        conn.commit()
        db.invalidate_stores_cache()
        flash(f'Store "{name}" added.', 'success')
    except Exception:
        flash(f'Store "{name}" already exists.', 'warning')
    finally:
        conn.close()
    return redirect(url_for('stores_report'))


@app.route('/stores/delete', methods=['POST'])
def delete_store():
    name = request.form.get('name', '').strip()
    conn = db.get_db()
    emp_count = conn.execute(
        "SELECT COUNT(*) FROM employees WHERE current_store=? AND status='active'", (name,)
    ).fetchone()[0]
    if emp_count > 0:
        flash(f'Cannot remove "{name}" — {emp_count} active employee(s) still assigned there.', 'danger')
        conn.close()
        return redirect(url_for('stores_report'))
    conn.execute("DELETE FROM stores WHERE name=?", (name,))
    conn.commit()
    conn.close()
    db.invalidate_stores_cache()
    flash(f'Store "{name}" removed.', 'success')
    return redirect(url_for('stores_report'))


# ── Payroll Sync ─────────────────────────────────────────────────────────────


@app.route('/stores')
def stores_report():
    conn = db.get_db()
    rows = conn.execute('''
        SELECT e.current_store,
            COUNT(DISTINCT e.id) as emp_count,
            COALESCE(u.uniform_out, 0)   as uniform_out,
            COALESCE(l.layby_out, 0)     as layby_out,
            COALESCE(u.uniform_plans, 0) as uniform_plans,
            COALESCE(l.layby_plans, 0)   as layby_plans
        FROM employees e
        LEFT JOIN (
            SELECT e2.current_store,
                SUM((ud.term_months - ud.payments_made) * ud.monthly_amount) as uniform_out,
                COUNT(ud.id) as uniform_plans
            FROM uniform_deductions ud
            JOIN employees e2 ON e2.id = ud.employee_id
            WHERE ud.status = 'active' AND ud.payments_made < ud.term_months AND e2.status = 'active' AND e2.sector = 'retail'
            GROUP BY e2.current_store
        ) u ON u.current_store = e.current_store
        LEFT JOIN (
            SELECT e3.current_store,
                SUM(COALESCE(ld.balance_remaining, (ld.term_months - ld.payments_made) * ld.monthly_amount)) as layby_out,
                COUNT(ld.id) as layby_plans
            FROM layby_deductions ld
            JOIN employees e3 ON e3.id = ld.employee_id
            WHERE ld.status = 'active' AND ld.payments_made < ld.term_months AND e3.status = 'active' AND e3.sector = 'retail'
            GROUP BY e3.current_store
        ) l ON l.current_store = e.current_store
        WHERE e.status = 'active' AND e.sector = 'retail'
        GROUP BY e.current_store
        ORDER BY e.current_store
    ''').fetchall()

    uc_rows = conn.execute(
        "SELECT uc.id,e.id employee_id,e.current_store FROM undercharges uc "
        "JOIN employees e ON e.id=uc.employee_id "
        "WHERE (uc.type IS NULL OR uc.type='undercharge') AND e.sector='retail'"
    ).fetchall()
    # Batched: the per-row version costs five statements each, so this page issued
    # 243 for 47 undercharges and grew linearly with the table.
    accounts = db.get_undercharge_accounts([r['id'] for r in uc_rows], conn)
    uc_map = {}
    for r in uc_rows:
        account = accounts.get(r['id'])
        if account is None:          # id with no matching plan — same as the
            continue                 # per-row function returning None
        amount = db.to_rands(account['remaining_cents'])
        if amount <= 0:
            continue
        item = uc_map.setdefault(r['current_store'], {'uc_out': 0, 'uc_count': 0})
        item['uc_out'] += amount
        item['uc_count'] += 1
    conn.close()
    stores = []
    for r in rows:
        s = r['current_store']
        uc = uc_map.get(s, {'uc_out': 0, 'uc_count': 0})
        total = round((r['uniform_out'] or 0) + (r['layby_out'] or 0) + uc['uc_out'], 2)
        stores.append({
            'store': s, 'emp_count': r['emp_count'],
            'uniform_out': round(r['uniform_out'] or 0, 2),
            'layby_out': round(r['layby_out'] or 0, 2),
            'uc_out': round(uc['uc_out'], 2),
            'total': total,
            'uniform_plans': r['uniform_plans'],
            'layby_plans': r['layby_plans'],
            'uc_count': uc['uc_count'],
        })

    now = datetime.now()
    return render_template('stores.html', stores=stores, now=now,
                           grand_uniform=sum(s['uniform_out'] for s in stores),
                           grand_layby=sum(s['layby_out'] for s in stores),
                           grand_uc=sum(s['uc_out'] for s in stores),
                           grand_total=sum(s['total'] for s in stores))
