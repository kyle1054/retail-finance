"""Staff allowances — HQ/DC annual goods budget.

Every HQ-sector employee (HQ or DC) can get a per-year allocation;
purchases (captured item-by-item, usually via the invoice Fast-Fill parser on
the employee profile) draw it down. This is not a payroll deduction: there are
no installments and no payroll locks — just a budget and a running remainder.
Overspend is allowed and shown in red, matching the old Excel tracker.
"""
from datetime import datetime
from flask import render_template, request, redirect, url_for, flash
from northwind.data import database as db
from northwind.core import app, employee_detail_url, HQ_STORES

SECTOR = 'hq'


def _back_to(emp_id):
    """Purchases/allocations are edited from the profile or the overview —
    return to wherever the form was submitted from."""
    ref = request.referrer or ''
    if not emp_id or '/hq/allowances' in ref:
        return redirect(url_for('hq_allowances',
                                year=request.form.get('year', ''),
                                location=request.form.get('location') or None))
    return redirect(employee_detail_url(emp_id, SECTOR))


@app.route('/hq/allowances')
def hq_allowances():
    year = request.args.get('year', type=int) or datetime.now().year
    location = request.args.get('location', '')
    if location not in HQ_STORES:
        location = ''
    rows = db.get_allowances_overview(year, SECTOR)
    if location:
        rows = [r for r in rows if r['current_store'] == location]
    totals = {
        'allocated': round(sum(r['allocated'] for r in rows), 2),
        'spent': round(sum(r['spent'] for r in rows), 2),
        'remaining': round(sum(r['remaining'] for r in rows), 2),
    }
    return render_template('allowances.html', rows=rows, year=year,
                           totals=totals, location=location)


@app.route('/hq/allowances/set', methods=['POST'])
def hq_set_allowance():
    emp_id = request.form.get('employee_id', '').strip()
    try:
        year = int(request.form['year'])
        allocated = float(request.form['allocated'])
    except (ValueError, TypeError, KeyError):
        flash('Please enter a valid year and amount.', 'danger')
        return _back_to(emp_id)
    if not emp_id or allocated < 0:
        flash('Employee and a non-negative amount are required.', 'danger')
        return _back_to(emp_id)
    db.set_allowance(emp_id, year, allocated, request.form.get('notes') or None)
    flash(f'Allowance for {year} set to R{allocated:.2f}.', 'success')
    return _back_to(emp_id)


@app.route('/hq/allowances/add-purchase', methods=['POST'])
def hq_add_allowance_purchase():
    emp_id = request.form.get('employee_id', '').strip()
    purchase_date = request.form.get('purchase_date', '').strip()
    try:
        year = int(purchase_date[:4]) if purchase_date else datetime.now().year
    except ValueError:
        flash('Please pick a valid purchase date.', 'danger')
        return _back_to(emp_id)

    items = []
    i = 0
    while f'item_desc_{i}' in request.form:
        desc = request.form.get(f'item_desc_{i}', '').strip()
        sku = request.form.get(f'item_sku_{i}', '').strip()
        try:
            price = float(request.form.get(f'item_price_{i}', 0) or 0)
            qty = int(request.form.get(f'item_qty_{i}', 1) or 1)
        except (ValueError, TypeError):
            price, qty = 0, 1
        if desc and price > 0:
            items.append({'sku': sku, 'desc': desc, 'price': price, 'qty': qty})
        i += 1

    if not items:
        flash('Add at least one item with a price.', 'danger')
        return _back_to(emp_id)

    db.add_allowance_purchases(
        emp_id, year, purchase_date or None, items,
        location=request.form.get('location') or None,
        sale_number=request.form.get('sale_number', '').strip() or None,
        notes=request.form.get('notes', '').strip() or None)
    flash(f'{len(items)} purchase line(s) added to the {year} allowance.', 'success')
    return _back_to(emp_id)


@app.route('/hq/allowances/purchase/<int:purchase_id>/delete', methods=['POST'])
def hq_delete_allowance_purchase(purchase_id):
    emp_id = db.delete_allowance_purchase(purchase_id)
    if emp_id:
        flash('Purchase line removed.', 'success')
    else:
        flash('Purchase not found.', 'danger')
    return _back_to(emp_id or '')
