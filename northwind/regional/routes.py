"""Regional Managers (RMs) — a read-only per-store cash dashboard, plus the
super-admin management pages that provision RMs and assign stores.

Two audiences:
  • A logged-in PORTAL PERSON (session['cc_user'] = their email) who is an ACTIVE
    Regional Manager gets a read-only cash dashboard scoped to ONLY their assigned
    stores. The same person may also be a cardholder (one shared login); the hub
    lets them pick. Every RM route re-derives the store scope from the DB and
    verifies any URL <store> is in it — the URL is never trusted.
  • A SUPER admin manages the RM roster (create / reset password / activate) and
    assigns each store's RM.

Security notes:
  - RM pages are strictly read-only: no add/edit/delete/opening controls exist and
    none are registered here.
  - The cash-sales toggle (?sales=1) is OFF by default. When off, no route emits
    the income ('in') figures — the templates receive include_sales and hide the
    income rows + the In/Sales column + the sales totals. Opening / expenses /
    banked / adjustments / closing are always shown.
  - Money is integer cents in the DB; all dashboard figures come from the existing
    cash-recon helpers in Rands at the template boundary.
"""
import calendar
from datetime import datetime, date, timedelta
from flask import render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash

from northwind import core
from northwind.data import database as db
from northwind.services import security
from northwind.core import app, ADMIN_AUTH_ENABLED

# Unambiguous alphabet for generated passwords (mirrors routes_credit_card so a
# person who is both an RM and a cardholder sees the same style of credential).
_PW_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def _generate_password():
    """A readable, shareable password like 'NORTHWIND-7K4P-9QXM'."""
    import secrets
    block = lambda: ''.join(secrets.choice(_PW_ALPHABET) for _ in range(4))
    return f'NORTHWIND-{block()}-{block()}'


# ── Shared request helpers (date range + sales toggle) ───────────────────────

def _month_bounds(y, m):
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _range_from_request():
    """Inclusive [start, end] from ?start/?end (ISO), default = current month.
    Mirrors routes_cash_recon._range_from_request. Returns (start_iso, end_iso,
    s, e)."""
    now = datetime.now()

    def parse(v):
        try:
            return datetime.strptime((v or '').strip(), '%Y-%m-%d').date()
        except ValueError:
            return None

    s = parse(request.args.get('start'))
    e = parse(request.args.get('end'))
    if s is None or e is None:
        s, e = _month_bounds(now.year, now.month)
    if e < s:
        s, e = e, s
    return s.isoformat(), e.isoformat(), s, e


def _sales_on():
    """Whether the cash-sales toggle is on. OFF by default — sales are hidden
    unless ?sales=1 (or truthy) is explicitly present."""
    return request.args.get('sales', '') in ('1', 'true', 'on', 'yes')


def _range_context(s, e, start_iso, end_iso):
    """Prev/next month links + a friendly range label (matches cash_overview)."""
    p_first, _ = _month_bounds(*((s.year - 1, 12) if s.month == 1 else (s.year, s.month - 1)))
    prev_first, prev_last = _month_bounds(p_first.year, p_first.month)
    n_year, n_month = (s.year + 1, 1) if s.month == 12 else (s.year, s.month + 1)
    next_first, next_last = _month_bounds(n_year, n_month)
    _, last = _month_bounds(s.year, s.month)
    is_full_month = (s.day == 1 and e == last)
    if is_full_month:
        range_label = f"{db.MONTH_FULL[s.month]} {s.year}"
    else:
        range_label = f"{s.strftime('%d %b %Y')} – {e.strftime('%d %b %Y')}"
    return {
        'start': start_iso, 'end': end_iso, 'range_label': range_label,
        'is_full_month': is_full_month,
        'prev_start': prev_first.isoformat(), 'prev_end': prev_last.isoformat(),
        'next_start': next_first.isoformat(), 'next_end': next_last.isoformat(),
    }


def _display_date(value):
    if not value:
        return 'No entries yet'
    try:
        return date.fromisoformat(value[:10]).strftime('%d %b %Y')
    except (TypeError, ValueError):
        return value


def _display_timestamp(value):
    if not value:
        return 'No updates yet'
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).strftime('%d %b · %H:%M')
    except (TypeError, ValueError):
        return value


def _pct_change(current, previous):
    if not previous:
        return None
    return round(((current - previous) / abs(previous)) * 100)


# ── Chart geometry (server-rendered inline SVG — CSP-safe, no JS libs) ────────
# The templates just emit <rect>/<text>/<polyline> from the primitives below.

_CHART_PALETTE = ['#823721', '#6b3e18', '#2f5a37', '#4a5d6b', '#8a6d3b',
                  '#615b5c', '#7a4a52', '#3b5d52', '#9c7a2c', '#54607a']


def _hbar_geometry(items, w=560, bar_h=24, gap=10, pad_left=170, pad_right=96, pad_top=6):
    """Horizontal-bar chart geometry. items: [{label, value, color?}]."""
    vals = [max(0.0, float(i['value'])) for i in items]
    mx = max(vals) if vals else 0.0
    if mx <= 0:
        mx = 1.0
    plot_w = w - pad_left - pad_right
    rows, y = [], pad_top
    for idx, it in enumerate(items):
        v = max(0.0, float(it['value']))
        bw = (v / mx) * plot_w
        rows.append({
            'label': it['label'], 'value': it['value'],
            'x': pad_left, 'y': y, 'w': round(bw, 1) if v > 0 else 0, 'h': bar_h,
            'color': it.get('color', _CHART_PALETTE[idx % len(_CHART_PALETTE)]),
            'label_x': pad_left - 10, 'text_y': round(y + bar_h * 0.68, 1),
            'val_x': round(pad_left + (bw if v > 0 else 0) + 8, 1),
            'url': it.get('url'),
            'drill_key': it.get('drill_key'),
        })
        y += bar_h + gap
    top = max(items, key=lambda i: float(i['value'])) if items else None
    summary = (f"Highest: {top['label']} at R {float(top['value']):,.2f}."
               if top else 'No values in this range.')
    return {'rows': rows, 'w': w, 'h': y + pad_top, 'pad_left': pad_left,
            'summary': summary}


def _column_geometry(days, w=760, h=230, pad_left=52, pad_right=16, pad_top=16, pad_bottom=38):
    """Daily columns (expenses) + a closing-float polyline overlay."""
    exps = [max(0.0, float(d['expense'])) for d in days]
    mx = max(exps) if exps else 0.0
    if mx <= 0:
        mx = 1.0
    closings = [float(d['day_closing']) for d in days]
    cmin = min(closings) if closings else 0.0
    cmax = max(closings) if closings else 1.0
    if cmax == cmin:
        cmax = cmin + 1.0
    plot_w = w - pad_left - pad_right
    plot_h = h - pad_top - pad_bottom
    n = len(days) or 1
    slot = plot_w / n
    barw = min(30.0, slot * 0.6)
    baseline = pad_top + plot_h
    cols, pts = [], []
    show_label_every = max(1, n // 16)   # avoid crowded axis on long ranges
    for i, d in enumerate(days):
        cx = pad_left + slot * i + slot / 2
        v = max(0.0, float(d['expense']))
        bh = (v / mx) * plot_h
        cols.append({
            'x': round(cx - barw / 2, 1), 'y': round(baseline - bh, 1),
            'w': round(barw, 1), 'h': round(bh, 1), 'cx': round(cx, 1),
            'label': d['date'][8:10], 'value': v, 'axis_y': baseline + 14,
            'show_label': (i % show_label_every == 0),
        })
        cy = baseline - ((float(d['day_closing']) - cmin) / (cmax - cmin)) * plot_h
        pts.append(f"{cx:.1f},{cy:.1f}")
    summary = ('Daily expenses and closing float. Peak daily expense '
               f"R {mx:,.2f}; closing float ranges from R {cmin:,.2f} "
               f"to R {cmax:,.2f}.")
    return {'cols': cols, 'polyline': ' '.join(pts), 'w': w, 'h': h,
            'baseline_y': baseline, 'pad_left': pad_left, 'right_x': w - pad_right,
            'top_y': pad_top, 'exp_max': mx, 'close_max': cmax, 'close_min': cmin,
            'summary': summary}


# ── RM portal (session['cc_user']) ───────────────────────────────────────────

def _rm_email_or_bounce():
    """The logged-in portal person's email if they are an ACTIVE RM, else a
    redirect response to their capability home (or the landing page). The core
    scoping guard: never proceeds for a non-RM."""
    email = session.get('cc_user')
    if not email:
        return None, redirect(url_for('landing'))
    caps = db.rm_capabilities(email)
    if not caps['is_rm']:
        # A cardholder-only person shouldn't reach RM pages; send them home.
        return None, redirect(url_for(core.portal_home_endpoint(email)))
    return email, None


@app.route('/portal')
def portal_hub():
    """Capability hub for a portal person. If they have only one capability we
    skip the hub and send them straight to it."""
    email = session.get('cc_user')
    if not email:
        return redirect(url_for('landing'))
    caps = db.rm_capabilities(email)
    if caps['is_rm'] and not caps['has_cards']:
        return redirect(url_for('rm_dashboard'))
    if caps['has_cards'] and not caps['is_rm']:
        return redirect(url_for('cc_portal'))
    if not caps['is_rm'] and not caps['has_cards']:
        # Neither capability (e.g. deactivated RM who holds no cards) — log out.
        flash('Your access has been removed. Please contact the office.', 'warning')
        return redirect(url_for('cc_portal_logout'))
    return render_template('portal_hub.html',
                           has_rm=caps['is_rm'], has_cards=caps['has_cards'],
                           card_count=caps['card_count'])


def build_cash_dashboard(stores, *, view, sel_param, include_sales,
                         start_iso, end_iso, s, e,
                         dash_endpoint, days_endpoint, dash_extra,
                         owner_name, kicker, page_title):
    """Build the full render context for the cash dashboard over `stores`.

    Shared by the RM portal (scoped to the RM's stores) and the admin overview
    (all stores, or one RM's region). `dash_endpoint`/`days_endpoint` are the
    routes the template links back to, and `dash_extra` is a dict of query args
    (e.g. the admin's selected region) threaded onto every self-link so the
    selection survives navigation. Returns the tpl dict; the caller renders."""
    rows = db.get_recon_overview_range(start_iso, end_iso, stores=stores)
    activity = db.get_recon_activity_summary(stores, start_iso, end_iso)
    as_of = min(e, date.today())
    stale_before = as_of - timedelta(days=3)
    attention_items = []
    for row in rows:
        reasons = []
        store_activity = activity['by_store'].get(row['store'], {})
        latest = store_activity.get('latest_entry_date')
        if not row['entry_count']:
            reasons.append('No entries in this range')
        elif latest and date.fromisoformat(latest) < stale_before:
            reasons.append(f"No entries since {_display_date(latest)}")
        if row['closing'] < 0:
            reasons.append('Negative closing float')
        if row['total_adjust']:
            reasons.append(f"Cash adjustment R {row['total_adjust']:,.2f}")
        if reasons:
            attention_items.append({'store': row['store'], 'reasons': reasons})

    period_days = (e - s).days + 1
    compare_end = s - timedelta(days=1)
    compare_start = compare_end - timedelta(days=period_days - 1)
    previous_rows = db.get_recon_overview_range(
        compare_start.isoformat(), compare_end.isoformat(), stores=stores)
    previous_by_store = {r['store']: r for r in previous_rows}
    previous_summary = db.get_recon_cumulative_range(
        stores, compare_start.isoformat(), compare_end.isoformat())

    # Everything the template may reference — defaults keep Jinja happy in every branch.
    tpl = dict(
        rm_name=owner_name, dash_kicker=kicker, page_title=page_title,
        dash_endpoint=dash_endpoint, days_endpoint=days_endpoint, dash_extra=dash_extra,
        regions=None, sel_region=None,          # admin caller overrides these
        stores=stores, include_sales=include_sales, view=view,
        summary=None, rows=rows, categories=[], cat_chart=None, store_chart=None,
        category_store_breakdowns={},
        trend=None, trend_expense_chart=None, trend_closing_chart=None,
        sel_store=None, srow=None, data=None, day_chart=None, compare=None,
        reported_count=sum(1 for r in rows if r['entry_count']),
        attention_items=attention_items, attention_count=len(attention_items),
        adjustment_count=activity['adjustment_count'],
        entries_current_through=_display_date(activity['latest_entry_date']),
        last_updated=_display_timestamp(activity['latest_created_at']),
        latest_banking_date=_display_date(activity['latest_banking_date']),
        compare_label=(f"{_display_date(compare_start.isoformat())} – "
                       f"{_display_date(compare_end.isoformat())}"),
        **_range_context(s, e, start_iso, end_iso))

    if not stores:
        return tpl

    if view == 'regional':
        tpl['summary'] = db.get_recon_cumulative_range(stores, start_iso, end_iso)
        tpl['compare'] = {
            'expense_pct': _pct_change(tpl['summary']['total_expense'],
                                       previous_summary['total_expense']),
            'closing_delta': round(tpl['summary']['closing'] - previous_summary['closing'], 2),
        }
        cats = db.get_recon_category_breakdown(stores, start_iso, end_iso)
        tpl['categories'] = cats
        store_cats = db.get_recon_category_store_breakdown(
            stores, start_iso, end_iso)[:10]
        breakdowns = {}
        chart_items = []
        for index, category in enumerate(store_cats):
            key = f'category-{index}'
            breakdowns[key] = category
            chart_items.append({
                'label': category['name'], 'value': category['total'],
                'drill_key': key,
            })
        tpl['category_store_breakdowns'] = breakdowns
        tpl['cat_chart'] = _hbar_geometry(
            chart_items, bar_h=28, gap=16, pad_top=10)
        tpl['store_chart'] = _hbar_geometry(
            [{'label': r['store'], 'value': r['total_expense'], 'color': '#823721',
              'url': url_for(dash_endpoint, view='store', store=r['store'],
                             start=start_iso, end=end_iso,
                             **({'sales': 1} if include_sales else {}), **dash_extra)}
             for r in tpl['rows']])

        # 6-month trend for the scope: monthly expenses + end-of-month float.
        trend = []
        for i in range(5, -1, -1):
            yy, mm = e.year, e.month - i
            while mm < 1:
                mm += 12
                yy -= 1
            m_s, m_e = _month_bounds(yy, mm)
            cum = db.get_recon_cumulative_range(stores, m_s.isoformat(), m_e.isoformat())
            trend.append({'label': f"{db.MONTH_FULL[mm][:3]} {yy}",
                          'expense': cum['total_expense'], 'closing': cum['closing']})
        tpl['trend'] = trend
        tpl['trend_expense_chart'] = _hbar_geometry(
            [{'label': t['label'], 'value': t['expense']} for t in trend])
        tpl['trend_closing_chart'] = _hbar_geometry(
            [{'label': t['label'], 'value': t['closing'], 'color': '#2f5a37'} for t in trend])
        return tpl

    # ── per-store (default) ──────────────────────────────────────────────────
    sel = sel_param if sel_param in stores else stores[0]
    tpl['sel_store'] = sel
    tpl['srow'] = next(r for r in rows if r['store'] == sel)
    tpl['latest_banking_date'] = _display_date(
        activity['by_store'].get(sel, {}).get('latest_banking_date'))
    previous_store = previous_by_store.get(sel, {'total_expense': 0, 'closing': 0})
    tpl['compare'] = {
        'expense_pct': _pct_change(tpl['srow']['total_expense'],
                                   previous_store['total_expense']),
        'closing_delta': round(tpl['srow']['closing'] - previous_store['closing'], 2),
    }
    tpl['data'] = db.get_recon_range(sel, start_iso, end_iso)
    cats = db.get_recon_category_breakdown([sel], start_iso, end_iso)
    tpl['categories'] = cats
    exp_cats = sorted([c for c in cats if c['kind'] == 'expense' and c['total'] > 0],
                      key=lambda c: -c['total'])[:10]
    tpl['cat_chart'] = _hbar_geometry([{'label': c['name'], 'value': c['total']} for c in exp_cats])
    tpl['day_chart'] = _column_geometry(tpl['data']['days']) if tpl['data']['days'] else None
    return tpl


@app.route('/portal/regional')
def rm_dashboard():
    """Read-only cash dashboard scoped to the RM's assigned stores.

    Two views: 'store' (the DEFAULT — one store at a time, with charts + its day
    breakdown; this is where store budgets live) and 'regional' (cumulative across
    all the RM's stores). The cash-sales toggle + date range apply to both."""
    email, bounce = _rm_email_or_bounce()
    if bounce:
        return bounce
    my_stores = db.get_rm_stores(email)   # active-RM scope; may be empty
    view = request.args.get('view', 'store')
    if view not in ('store', 'regional'):
        view = 'store'
    start_iso, end_iso, s, e = _range_from_request()
    tpl = build_cash_dashboard(
        my_stores, view=view, sel_param=request.args.get('store'),
        include_sales=_sales_on(), start_iso=start_iso, end_iso=end_iso, s=s, e=e,
        dash_endpoint='rm_dashboard', days_endpoint='rm_store_days', dash_extra={},
        owner_name=(db.get_rm_user(email)['name'] or email),
        kicker='Read-only regional view', page_title='Regional cash dashboard')
    return render_template('rm_dashboard.html', **tpl)


@app.route('/cash/dashboard')
def admin_cash_dashboard():
    """Admin cash dashboard — the SAME rich view an RM gets, but across ALL stores
    ('overall'), with a region picker to scope it to any one RM's stores. Read-only.
    SUPER-ONLY (in SUPER_ONLY_ENDPOINTS); require_login bounces retail/hq admins and
    any store/portal session. See build_cash_dashboard for the shared rendering."""
    if not session.get('admin') and ADMIN_AUTH_ENABLED:
        return redirect(url_for('landing'))
    view = request.args.get('view', 'regional')     # admins land on the overall view
    if view not in ('store', 'regional'):
        view = 'regional'
    start_iso, end_iso, s, e = _range_from_request()

    rm_rows = db.list_rm_users()
    region_by_email = {r['email']: r for r in rm_rows}
    region = (request.args.get('region') or 'all').strip()
    if region != 'all' and region in region_by_email:
        stores = db.get_rm_stores(region)
        owner = f"{region_by_email[region]['name'] or region} · region"
    else:
        region = 'all'
        stores = db.get_stores()                     # overall = every store
        owner = 'All stores'
    dash_extra = {} if region == 'all' else {'region': region}

    tpl = build_cash_dashboard(
        stores, view=view, sel_param=request.args.get('store'),
        include_sales=_sales_on(), start_iso=start_iso, end_iso=end_iso, s=s, e=e,
        dash_endpoint='admin_cash_dashboard', days_endpoint='admin_cash_dashboard_days',
        dash_extra=dash_extra, owner_name=owner,
        kicker='Admin cash overview', page_title='Cash dashboard')
    tpl['regions'] = [{'email': r['email'], 'name': r['name'] or r['email'],
                       'count': r['store_count']} for r in rm_rows]
    tpl['sel_region'] = region
    return render_template('rm_dashboard.html', **tpl)


@app.route('/cash/dashboard/<store>/days')
def admin_cash_dashboard_days(store):
    """Admin per-day breakdown fragment for one store (any store; admin scope)."""
    if not session.get('admin') and ADMIN_AUTH_ENABLED:
        return redirect(url_for('landing'))
    if store not in db.get_stores():
        return ('Not found.', 404)
    start_iso, end_iso, _s, _e = _range_from_request()
    data = db.get_recon_range(store, start_iso, end_iso)
    return render_template('rm_store_days.html', store=store, data=data,
                           start=start_iso, end=end_iso, include_sales=_sales_on())


@app.route('/portal/regional/<store>/days')
def rm_store_days(store):
    """Read-only per-day breakdown for ONE of the RM's stores (inline fragment).
    Verifies the store is in the RM's scope BEFORE returning any data."""
    email, bounce = _rm_email_or_bounce()
    if bounce:
        return bounce
    my_stores = db.get_rm_stores(email)
    if store not in my_stores:
        # Never leak another store's data via the URL.
        return ('Not found.', 404)
    include_sales = _sales_on()
    start_iso, end_iso, _s, _e = _range_from_request()
    data = db.get_recon_range(store, start_iso, end_iso)
    return render_template('rm_store_days.html', store=store, data=data,
                           start=start_iso, end=end_iso,
                           include_sales=include_sales)


# ── Admin: RM management (SUPER-ONLY) ────────────────────────────────────────

def _require_super():
    """Belt-and-braces super gate in-handler (the endpoint is also in
    SUPER_ONLY_ENDPOINTS). Returns a redirect to block, or None to allow."""
    if not ADMIN_AUTH_ENABLED:
        return None  # local dev
    if session.get('admin_role') == 'super':
        return None
    flash('That area is for super administrators only.', 'danger')
    return redirect(url_for('dashboard'))


@app.route('/admin/regional-managers')
def admin_regional_managers():
    blocked = _require_super()
    if blocked:
        return blocked
    rms = db.list_rm_users()
    stores = db.get_stores()
    # store -> RM email currently assigned (None if unassigned).
    assignments = {s: db.get_store_rm(s) for s in stores}
    cards = db.list_cc_cards()
    # One-time credential shown after create/reset (mirrors cc_new_credential).
    new_credential = session.pop('rm_new_credential', None)
    return render_template('admin_regional_managers.html',
                           rms=rms, stores=stores, assignments=assignments,
                           cards=cards,
                           new_credential=new_credential)


@app.route('/admin/regional-managers/create', methods=['POST'])
def admin_rm_create():
    blocked = _require_super()
    if blocked:
        return blocked
    name = (request.form.get('name') or '').strip()
    email = (request.form.get('email') or '').strip().lower()
    if '@' not in email or '.' not in email:
        flash('Enter a valid email address.', 'danger')
        return redirect(url_for('admin_regional_managers'))

    existing_cred = db.get_cc_user(email)
    db.upsert_rm_user(email, name, active=1)

    # Ensure they have a login. If they already have one (e.g. they are a
    # cardholder), keep it — don't silently rotate their password.
    if existing_cred:
        flash(f'{email} is now a Regional Manager. They already had a login — '
              f'use "reset password" if they need a new one.', 'success')
    else:
        typed = (request.form.get('password') or '').strip()
        if typed and len(typed) < security.MIN_PASSWORD_LENGTH:
            flash(f'Password must be at least {security.MIN_PASSWORD_LENGTH} characters.', 'danger')
            return redirect(url_for('admin_regional_managers'))
        password = typed or _generate_password()
        db.set_cc_user_password(email, generate_password_hash(password, method='pbkdf2:sha256'))
        session['rm_new_credential'] = {'email': email, 'password': password}
        flash(f'Regional Manager created for {email}. Copy their password below — '
              f'it is not stored and won\'t be shown again.', 'success')
    # A selected card is assigned immediately. Blank leaves an existing
    # cardholder's access untouched; the profile control below is the explicit
    # place to remove or replace it.
    if request.form.get('card_id'):
        try:
            db.set_rm_card(email, request.form.get('card_id'))
        except ValueError as exc:
            flash(str(exc), 'danger')
    return redirect(url_for('admin_regional_managers'))


@app.route('/admin/regional-managers/<email>/reset', methods=['POST'])
def admin_rm_reset_password(email):
    blocked = _require_super()
    if blocked:
        return blocked
    email = (email or '').strip().lower()
    if not db.get_rm_user(email):
        flash('Unknown Regional Manager.', 'danger')
        return redirect(url_for('admin_regional_managers'))
    typed = (request.form.get('password') or '').strip()
    if typed and len(typed) < security.MIN_PASSWORD_LENGTH:
        flash(f'Password must be at least {security.MIN_PASSWORD_LENGTH} characters.', 'danger')
        return redirect(url_for('admin_regional_managers'))
    password = typed or _generate_password()
    db.set_cc_user_password(email, generate_password_hash(password, method='pbkdf2:sha256'))
    session['rm_new_credential'] = {'email': email, 'password': password}
    flash(f'Password reset for {email}. Copy the new one below — it is not stored '
          f'and won\'t be shown again.', 'success')
    return redirect(url_for('admin_regional_managers'))


@app.route('/admin/regional-managers/<email>/toggle', methods=['POST'])
def admin_rm_toggle(email):
    blocked = _require_super()
    if blocked:
        return blocked
    email = (email or '').strip().lower()
    row = db.get_rm_user(email)
    if not row:
        flash('Unknown Regional Manager.', 'danger')
        return redirect(url_for('admin_regional_managers'))
    new_active = 0 if row['active'] else 1
    db.set_rm_active(email, new_active)
    flash(f'{email} {"activated" if new_active else "deactivated"}.', 'success')
    return redirect(url_for('admin_regional_managers'))


@app.route('/admin/regional-managers/assign', methods=['POST'])
def admin_rm_assign():
    blocked = _require_super()
    if blocked:
        return blocked
    store = (request.form.get('store') or '').strip()
    email = (request.form.get('email') or '').strip().lower()
    if store not in db.get_stores():
        flash('Unknown store.', 'danger')
        return redirect(url_for('admin_regional_managers'))
    if email and not db.get_rm_user(email):
        flash('Choose an existing Regional Manager (or "None").', 'danger')
        return redirect(url_for('admin_regional_managers'))
    db.assign_store_rm(store, email or None)
    if email:
        flash(f'{store} assigned to {email}.', 'success')
    else:
        flash(f'{store} now has no Regional Manager.', 'info')
    return redirect(url_for('admin_regional_managers'))


@app.route('/admin/regional-managers/<email>/card', methods=['POST'])
def admin_rm_assign_card(email):
    """Assign zero or one company credit card to an RM."""
    blocked = _require_super()
    if blocked:
        return blocked
    email = (email or '').strip().lower()
    try:
        card = db.set_rm_card(email, request.form.get('card_id'))
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin_regional_managers'))
    if card:
        label = card['display_name'] or card['card_name']
        flash(f'{label} assigned to {email}. It replaces any previous card.', 'success')
    else:
        flash(f'Credit-card access removed from {email}; their RM login stays active.', 'info')
    return redirect(url_for('admin_regional_managers'))
