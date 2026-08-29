from flask import render_template
from datetime import datetime
from northwind.data import database as db
from northwind.services import money
from northwind.core import app


@app.route('/admin')
def dashboard():
    conn = db.get_db()
    # All counts and outstanding amounts in a single round-trip
    # All deduction aggregates are scoped to retail staff so the Retail dashboard
    # never counts HQ employees or their lay-bys (`R` = retail employee ids).
    R = "employee_id IN (SELECT id FROM employees WHERE sector='retail')"
    row = conn.execute(f'''
        SELECT
            (SELECT SUM(status='active')    FROM employees WHERE sector='retail') AS active_employees,
            (SELECT SUM(status='terminated') FROM employees WHERE sector='retail') AS terminated_employees,
            (SELECT COUNT(*) FROM uniform_deductions WHERE status='active' AND {R}) AS active_uniform_plans,
            (SELECT COUNT(*) FROM layby_deductions  WHERE status='active' AND {R}) AS active_layby_plans,
            (SELECT COUNT(*) FROM undercharges
             WHERE status IN ('pending','partial') AND (type IS NULL OR type='undercharge') AND {R}) AS pending_undercharges,
            (SELECT COUNT(*) FROM undercharges
             WHERE status='pending' AND type='overcharge' AND {R}) AS pending_overcharges,
            (SELECT COALESCE(SUM(COALESCE(balance_remaining, CASE WHEN term_months > payments_made
                 THEN COALESCE(total_amount, term_months * monthly_amount) - (payments_made * ROUND(monthly_amount,2))
                 ELSE 0 END)), 0) FROM uniform_deductions WHERE status='active' AND {R}) AS uniform_outstanding,
            (SELECT COALESCE(SUM(COALESCE(balance_remaining,(term_months-payments_made)*monthly_amount)),0)
             FROM layby_deductions WHERE status='active' AND {R}) AS layby_outstanding
    ''').fetchone()
    stats = dict(row)
    uc_total = round(sum(
        values['undercharges']
        for values in db._outstanding_by_employee(conn, sector='retail').values()
    ), 2)
    stats['undercharge_outstanding'] = uc_total
    stats['total_outstanding'] = round(stats['uniform_outstanding'] + stats['layby_outstanding'] + uc_total, 2)
    
    layby_cap_violators = conn.execute('''
        SELECT e.id, e.full_name, e.current_store, 
               SUM(l.monthly_amount) as total_monthly,
               SUM(l.balance_remaining) as total_remaining,
               COUNT(l.id) as plan_count
        FROM employees e
        JOIN layby_deductions l ON e.id = l.employee_id
        WHERE l.status = 'active' AND e.sector = 'retail'
        GROUP BY e.id
        HAVING total_monthly > 833.00
        ORDER BY total_monthly DESC
    ''').fetchall()

    conn.close()
    history = db.get_payroll_history(6)
    top_debtors = db.get_top_debtors(10)
    top_underchargers = db.get_top_undercharge_employees(8)
    repeat_offenders = db.get_repeat_offenders(8)
    error_stores = db.get_error_hotspots_by_store(8)
    now = datetime.now()

    monthly_data = db.get_monthly_data(now.year, now.month)
    outstanding_ticks = [d for d in monthly_data if d['total'] > 0]
    pending_ticks = [d for d in outstanding_ticks if not d.get('all_done')]
    completed_ticks = len(outstanding_ticks) - len(pending_ticks)
    stores_in_month = {}
    for item in outstanding_ticks:
        store = item['employee']['current_store'] or '(No store)'
        stores_in_month.setdefault(store, []).append(item)
    ready_stores = sum(1 for rows in stores_in_month.values()
                       if rows and all(row.get('all_done') for row in rows))
    payroll_progress = round((completed_ticks / len(outstanding_ticks)) * 100) \
        if outstanding_ticks else 100

    return render_template('index.html', stats=stats, now=now, history=history,
                           top_debtors=top_debtors, top_underchargers=top_underchargers,
                           repeat_offenders=repeat_offenders, error_stores=error_stores,
                           outstanding_ticks=outstanding_ticks,
                           pending_tick_preview=pending_ticks[:5],
                           pending_tick_count=len(pending_ticks),
                           completed_tick_count=completed_ticks,
                           payroll_progress=payroll_progress,
                           payroll_store_count=len(stores_in_month),
                           payroll_ready_store_count=ready_stores,
                           layby_cap_violators=layby_cap_violators)


# ── Employees ───────────────────────────────────────────────────────────────
