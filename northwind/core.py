from flask import (Flask, render_template, request, redirect, url_for, flash,
                   jsonify, session)
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import safe_join
from datetime import datetime, timedelta
import hashlib
import os
import time
from northwind.data import database as db

# core.py now lives at northwind/core.py. Anchor Flask's resource root (templates/,
# static/) and the default backup dir to the REPO ROOT so the package move
# changes no paths. Plain Flask(__name__) would resolve resources under northwind/.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__, root_path=_REPO_ROOT)

# Strip the newline after a block tag and the indentation before one. Every
# authenticated page is Cache-Control: no-store (for authenticated responses), so the FULL body is
# re-sent on every navigation — the indentation left behind by thousands of
# `{% for %}` / `{% if %}` lines is therefore paid per page view, not once.
# Measured: -5% to -10% of the bytes on most pages, -26% on /monthly (77 KB).
#
# This CAN change rendered output, so it was checked rather than assumed: every
# template was lexed with the flags off and on to find each removed whitespace
# run. All of them sit between block elements, inside a flex/grid container,
# between elements with their own margins, or between `;`-terminated CSS
# declarations — none is visible. tests/test_asset_delivery.py re-runs that
# check per template so a future template can't quietly introduce a real one.
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True

# ── Environment & security configuration ─────────────────────────────────────
# Set NW_ENV=production on any public/hosted deployment. In production the app
# refuses to boot without a real FLASK_SECRET_KEY and serves HTTPS-only cookies.
IS_PRODUCTION = os.environ.get('NW_ENV', 'development').lower() == 'production'

# Behind a single reverse proxy, trust exactly one hop of X-Forwarded-For so
# request.remote_addr is the real client IP. Without this the
# login throttle (keyed on IP + identifier) sees one constant proxy IP for every
# client, collapsing to identifier-only — which lets anyone who knows an admin
# username / store email fire 5 bad passwords and lock that account out for
# everyone. Prod only (there is no proxy in front of the local dev server).
if IS_PRODUCTION:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# Where DB backups are written (startup snapshots, pre-restore safety copies).
# Defaults to db/backups/ in the repo; set NW_BACKUP_DIR to a volume path (e.g.
# a persistent volume path) on hosts with ephemeral filesystems.
BACKUP_DIR = os.environ.get('NW_BACKUP_DIR') or os.path.join(_REPO_ROOT, 'db', 'backups')

_secret = os.environ.get('FLASK_SECRET_KEY')
if not _secret:
    if IS_PRODUCTION:
        raise RuntimeError(
            "FLASK_SECRET_KEY must be set in production. Generate one with:\n"
            "  python -c \"import secrets; print(secrets.token_hex(32))\"")
    # Development only: a stable but clearly-insecure key so local sessions work.
    _secret = 'dev-insecure-key-not-for-production'
app.secret_key = _secret

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,              # JS can't read the session cookie
    SESSION_COOKIE_SAMESITE='Lax',             # blocks cross-site cookie sends (CSRF defence-in-depth)
    SESSION_COOKIE_SECURE=IS_PRODUCTION,       # HTTPS-only cookie once hosted
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),  # cookie lifetime for every
                                               # logged-in session (admin, store,
                                               # cardholder). Refreshed on each
                                               # request, so it is a rolling 12h —
                                               # the binding limit is the per-role
                                               # idle timeout enforced below.
    MAX_CONTENT_LENGTH=150 * 1024 * 1024,      # 150 MB cap on a single request/upload batch
                                               # (per-file cap stays 15 MB in routes_credit_card;
                                               # peak RAM is driven by per-file size, not the batch total)
    WTF_CSRF_TIME_LIMIT=None,                  # CSRF token lives as long as the session (no surprise 400s)
)

# CSRF protection on every state-changing request (POST/PUT/PATCH/DELETE).
# Templates expose the token via csrf_token(); forms carry it as a hidden field
# (auto-injected by base.html JS) and AJAX sends it in the X-CSRFToken header.
csrf = CSRFProtect(app)


@app.errorhandler(404)
def handle_404(e):
    return render_template('error.html', code=404), 404


@app.errorhandler(500)
def handle_500(e):
    return render_template('error.html', code=500), 500


@app.errorhandler(413)
def handle_too_large(e):
    """Upload exceeded MAX_CONTENT_LENGTH — send them back with a clear message
    instead of the bare 'Request Entity Too Large' page. (Referer is a header, so
    it's still readable even though the oversized body was rejected unread.)"""
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify(error='too_large',
                       message='That upload is too large — the limit is 150 MB per batch.'), 413
    flash('That upload was too large — the limit is 150 MB per batch. '
          'Please select fewer files and try again.', 'warning')
    return redirect(request.referrer or url_for('landing')), 303


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    """Friendly handling when a CSRF token is missing/expired (e.g. stale tab)."""
    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify(error='csrf', message='Your session expired. Please refresh and try again.'), 400
    flash('Your session expired or the form was stale. Please try again.', 'warning')
    return redirect(request.referrer or url_for('landing')), 303

app.teardown_appcontext(db.close_request_conns)


@app.template_filter('rands')
def _rands_filter(value):
    """Format a money amount with thousands separators (value only — templates
    keep their own 'R ' prefix): 128473.5 -> '128,473.50'. Non-numbers pass
    through unchanged so it's safe to apply broadly."""
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return value


@app.template_filter('shortdate')
def _shortdate_filter(value):
    """'2026-07-01' -> '01 Jul'. Cardholders scan one month of their own
    charges, so the year is already established by the section heading and the
    ISO form just reads as machine data. Anything unparseable passes through."""
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').strftime('%d %b')
    except (TypeError, ValueError):
        return value

JOB_TITLES =['Part-time Associate', 'Full-time Associate', 'Store Manager', 'Regional Manager']

# HQ locations are fixed (HQ + DC) and intentionally kept out of the retail
# `stores` table so the two sectors stay completely separate.
HQ_STORES = ['HQ', 'DC']
HQ_JOB_TITLES = ['Head Office Staff', 'DC Staff', 'Manager', 'Executive']

# Workspaces shown in the sidebar brand switcher. To add a new area of the app
# later, register it here (and point `endpoint` at its landing route) — the
# switcher menu picks it up automatically. `prefixes` are endpoint-name
# fragments used to highlight the active workspace. Each workspace also carries
# the endpoint names and feature set the shared templates resolve against, so
# Retail and HQ can reuse the same pages while staying separate sections.
# Each workspace carries a 'group' — the top-level nav bucket it belongs to.
# Retail + HQ share the "Deductions" group (rendered as one tab with a small
# Retail/HQ secondary switch); Credit Cards and Cash Recon are their own groups.
WORKSPACES = [
    {'key': 'retail', 'name': 'NORTHWIND', 'sub': 'Retail Deductions', 'tab': 'Retail',
     'group': 'Deductions',
     'icon': 'bi-layers-fill', 'endpoint': 'dashboard', 'prefixes': [],
     'features': {'uniforms', 'laybys', 'undercharges', 'transfer', 'dashboard', 'stores'},
     'employees_ep': 'employees', 'employee_detail_ep': 'employee_detail',
     'add_employee_ep': 'add_employee', 'laybys_ep': 'laybys_list',
     'monthly_ep': 'monthly_current', 'monthly_view_ep': 'monthly_view',
     'pay_layby_ep': 'pay_layby_monthly', 'pay_all_ep': 'pay_all_monthly',
     'pay_selected_ep': 'pay_selected_monthly',
     'lock_ep': 'lock_period', 'unlock_ep': 'unlock_period',
     'monthly_export_ep': 'export_monthly', 'laybys_export_ep': 'export_laybys'},
    {'key': 'hq', 'name': 'NORTHWIND', 'sub': 'HQ Deductions', 'tab': 'HQ',
     'group': 'Deductions',
     'icon': 'bi-building', 'endpoint': 'hq_employees', 'prefixes': ['hq_'],
     'features': {'laybys', 'allowances'},
     'employees_ep': 'hq_employees', 'employee_detail_ep': 'hq_employee_detail',
     'add_employee_ep': 'hq_add_employee', 'laybys_ep': 'hq_laybys_list',
     'monthly_ep': 'hq_monthly_current', 'monthly_view_ep': 'hq_monthly_view',
     'pay_layby_ep': 'hq_pay_layby_monthly', 'pay_all_ep': 'hq_pay_all_monthly',
     'pay_selected_ep': 'hq_pay_selected_monthly',
     'lock_ep': 'hq_lock_period', 'unlock_ep': 'hq_unlock_period',
     'monthly_export_ep': 'hq_export_monthly', 'laybys_export_ep': 'hq_export_laybys'},
    # Credit Card Reconciliation — its own section. Cardholders are provisioned
    # by uploading their Xero recon export; see routes_credit_card. Visible to
    # super admins (and dev-mode); scoped retail/hq admins keep their own area.
    {'key': 'creditcard', 'name': 'NORTHWIND', 'sub': 'Credit Cards', 'tab': 'Cards',
     'group': 'Credit Cards',
     'icon': 'bi-credit-card-2-front-fill', 'endpoint': 'cc_home', 'prefixes': ['cc_'],
     'features': set()},
    # Cash Reconciliation — store float ledger + admin drill-down. Retail-
    # flavoured: super + retail admins reach it, HQ cannot (cash_* endpoints
    # are non-'hq_'). See routes_cash_recon.
    {'key': 'cashrecon', 'name': 'NORTHWIND', 'sub': 'Cash Recon', 'tab': 'Cash',
     'group': 'Cash Recon',
     'icon': 'bi-cash-stack', 'endpoint': 'cash_home', 'prefixes': ['cash_'],
     'features': set()},
]


def employee_detail_url(emp_id, sector=None):
    """URL of an employee's profile in the correct section (Retail or HQ)."""
    if sector is None:
        sector = db.get_employee_sector(emp_id)
    ep = 'hq_employee_detail' if sector == 'hq' else 'employee_detail'
    return url_for(ep, emp_id=emp_id)


def employees_list_url(sector):
    """URL of the employee list for a sector."""
    return url_for('hq_employees' if sector == 'hq' else 'employees')

# Admin auth is always enabled. Disable with ADMIN_AUTH=0 for local dev only.
ADMIN_AUTH_ENABLED = os.environ.get('ADMIN_AUTH', '1') != '0'

# Super-admin session idle timeout — 90 minutes of inactivity, kept strict (a
# super admin can do anything, so a session left open on a shared machine must
# not stay usable all day). On top of the 12h absolute PERMANENT_SESSION_LIFETIME.
ADMIN_SESSION_TIMEOUT = 5400  # seconds

# EVERYONE ELSE (retail/hq admins, staff, cardholders, RMs) gets a 12-hour idle
# timeout, per request — long enough to work through a day without re-logging in.
NONSUPER_SESSION_TIMEOUT = 43200  # 12 hours, in seconds

# Staff- and cardholder-portal timeouts follow the non-super 12h rule.
STAFF_SESSION_TIMEOUT = NONSUPER_SESSION_TIMEOUT
CC_SESSION_TIMEOUT = NONSUPER_SESSION_TIMEOUT

# Endpoints reachable without any session at all.
PUBLIC_ENDPOINTS = {'landing', 'admin_login', 'admin_logout', 'staff_login',
                    'staff_logout', 'static'}

# Cash recon needs no admin allowlist: every cash_* endpoint is non-'hq_', so
# admin_endpoint_allowed already gives it to super + retail and denies HQ. There
# used to be a CASH_ENDPOINTS set here that nothing read — it looked load-bearing
# and invited "fixing" access control by editing a set with no effect. Don't
# reintroduce it; add the endpoint to SUPER_ONLY_ENDPOINTS if it needs narrowing.

# The endpoints a logged-in STORE session may reach (scoped to its own store in
# the handlers). Admin-only views (opening float, summary, day drill-down, edit)
# are deliberately excluded — a store must not reach them.
CASH_STORE_ENDPOINTS = {'cash_home', 'cash_store', 'cash_ledger',
                        'cash_add_entry', 'cash_delete_entry'}

# Endpoints reachable by a logged-in store (the staff portal + their cash recon).
STAFF_ENDPOINTS = {'portal_store', 'portal_store_employee', 'portal_me',
                   # Staff requests: asking for a uniform / lay-by, and following
                   # it up. Each handler re-checks the store owns the employee.
                   'portal_request_new', 'portal_request_detail',
                   'portal_request_comment', 'portal_request_cancel',
                   } | CASH_STORE_ENDPOINTS

# Cardholder portal — a logged-in card user (session['cc_user'] = their email)
# reaches only these, each scoped in-handler to cards their email may access.
CC_PORTAL_ENDPOINTS = {'cc_portal', 'cc_portal_card', 'cc_portal_upload',
                       'cc_portal_upload_to_line',
                       'cc_portal_delete_receipt', 'cc_portal_file', 'cc_portal_logout',
                       'cc_portal_link', 'cc_portal_unlink', 'cc_portal_reason',
                       'cc_portal_location', 'cc_portal_personal', 'cc_portal_submit',
                       'cc_portal_submit_lines', 'cc_portal_confirm_suggestion',
                       'cc_portal_reject_suggestion',
                       'cc_portal_inbox_upload', 'cc_portal_inbox_assign'}

# Regional-Manager portal — the SAME portal person (session['cc_user'] = email)
# may also be an RM. These read-only endpoints are reachable in addition to any
# card endpoints they hold; each is scoped in-handler to the RM's assigned stores.
RM_PORTAL_ENDPOINTS = {'rm_dashboard', 'rm_store_days', 'portal_hub'}

# Endpoints that stream an uploaded file (image/PDF) meant to be VIEWED inline in
# the browser. They get a viewer-friendly cache policy (see set_security_headers)
# instead of the blanket 'no-store', which breaks the built-in PDF viewer.
INLINE_FILE_ENDPOINTS = {'cc_portal_file'}

# ── Admin access tiers ───────────────────────────────────────────────────────
# Admins carry a role: 'super' (everything), 'retail' (retail deductions only)
# or 'hq' (HQ deductions only). HQ pages are identified by their 'hq_' endpoint
# prefix; retail is everything else. The two sets below are the exceptions to
# that rule. Enforced in require_login(); see also routes_auth login.

# Admin-management and whole-database operations — super admins only. Scoped
# admins must never create accounts, manage staff logins, or move the DB.
SUPER_ONLY_ENDPOINTS = {
    'admin_manage_admins', 'admin_add_admin', 'admin_delete_admin',
    'admin_edit_admin_role', 'admin_reset_admin_password', 'admin_set_admin_active',
    # Editing one person's access from the roster (admin role, RM + stores,
    # login status, password) — same super-only footing as the rest of this page.
    'admin_person_access', 'admin_person_password',
    'admin_staff_logins', 'admin_add_store_email', 'admin_delete_store_email',
    'admin_import_store_emails', 'admin_set_store_password', 'admin_clear_store_password',
    'download_backup', 'restore_backup',
    # Regional-Manager management (roster + per-store assignment). Non-'hq_'
    # endpoints, so this gate is what keeps them super-only (like admin mgmt).
    'admin_regional_managers', 'admin_rm_create', 'admin_rm_reset_password',
    'admin_rm_toggle', 'admin_rm_assign', 'admin_rm_assign_card',
    # Admin cash dashboard (the RM-style overall view + region picker). Super-only
    # by request; non-'hq_' so this gate is what restricts it.
    'admin_cash_dashboard', 'admin_cash_dashboard_days',
    # Outbound email setup + test send. Holds the company sending identity, so
    # it sits with the other whole-system settings.
    'admin_email', 'admin_email_test',
}

# Cross-sector pages any admin role may open; the view filters their *content*
# to the admin's own sector (super sees both). See routes_search/routes_activity.
# admin_change_password is here so EVERY admin role (incl. HQ, whose access is
# otherwise limited to hq_ endpoints) can change their own password.
SHARED_SECTOR_ENDPOINTS = {'invoice_search', 'global_search', 'recent_activity',
                           'admin_change_password'}


def admin_endpoint_allowed(endpoint, role):
    """Whether an admin with `role` may reach `endpoint`."""
    if role == 'super':
        return True
    if endpoint in SUPER_ONLY_ENDPOINTS:
        return False
    # Credit Card Reconciliation is a super-only section (the switcher hides it
    # from scoped admins). Block every 'cc_' endpoint for non-super roles so a
    # retail admin can't reach /cards by typing the URL directly.
    if endpoint.startswith('cc_'):
        return False
    if endpoint in SHARED_SECTOR_ENDPOINTS:
        return True
    is_hq = endpoint.startswith('hq_')
    if role == 'hq':
        return is_hq
    if role == 'retail':
        return not is_hq
    return False  # unknown role -> no access


def admin_home_endpoint(role):
    """Where an admin of `role` lands after login / when bounced from a page."""
    return 'hq_employees' if role == 'hq' else 'dashboard'


def portal_home_endpoint(email):
    """Capability-aware home for a portal person (session['cc_user'] = email).

    A person may be an RM, a cardholder, or both (one shared login):
      both  -> 'portal_hub'    (choose which area)
      RM    -> 'rm_dashboard'
      cards -> 'cc_portal'     (the existing default)
    Used by the login redirect AND by require_login's bounce, so an RM-only
    person is never dumped on the empty card portal."""
    caps = db.rm_capabilities(email)
    if caps['is_rm'] and caps['has_cards']:
        return 'portal_hub'
    if caps['is_rm']:
        return 'rm_dashboard'
    return 'cc_portal'


def current_admin_sector():
    """Sector an admin's views should be scoped to, or None for full visibility.

    Returns 'retail'/'hq' for scoped admins; None for super admins and for the
    staff portal (which has its own store-level scoping)."""
    role = session.get('admin_role')
    return role if role in ('retail', 'hq') else None


@app.before_request
def require_login():
    if not ADMIN_AUTH_ENABLED:
        return  # login disabled — everything open (local dev)

    ep = request.endpoint
    if ep is None:
        return  # unmatched route -> let Flask 404

    # Public pages — anyone
    if ep in PUBLIC_ENDPOINTS:
        return

    # ── Admin session check (admins keep their access even if they also
    #    opened the staff portal in the same browser) ──────────────────────
    if session.get('admin'):
        # Fail closed: an authenticated admin session MUST carry an explicit role
        # (login always sets one — 'super' only as the DB-level default for legacy
        # accounts). A session with `admin` but no `admin_role` can only arise from
        # a malformed/partial session, and must never silently be treated as super.
        role = session.get('admin_role')
        identity = db.get_session_user(session.get('uid'))
        if (not role or not identity or not identity['is_active']
                or identity['role'] != role
                or identity['auth_version'] != session.get('auth_version')):
            session.clear()
            flash('Your access changed. Please log in again.', 'info')
            return redirect(url_for('landing', next=request.path))
        # Idle timeout. A session that predates this feature (or a fresh login)
        # has no timestamp yet — grandfather it by stamping now rather than
        # expiring it, so a deploy doesn't log every active admin out at once.
        last_active = session.get('admin_last_active')
        # Super admins keep the strict 90-min idle timeout; every other role gets 12h.
        admin_timeout = ADMIN_SESSION_TIMEOUT if role == 'super' else NONSUPER_SESSION_TIMEOUT
        if last_active is not None and time.time() - last_active > admin_timeout:
            session.clear()
            flash('Session expired — please log in again.', 'info')
            return redirect(url_for('landing', next=request.path))
        session['admin_last_active'] = time.time()
        if admin_endpoint_allowed(ep, role):
            return
        # An admin who is ALSO a cardholder / RM (same email) may use their own
        # portal — login stashes session['cc_user'] for them. Each portal handler
        # still scopes to that email's own cards/stores.
        if session.get('cc_user') and ep in (CC_PORTAL_ENDPOINTS | RM_PORTAL_ENDPOINTS):
            return
        # Wrong sector / not permitted: bounce to the admin's own home so a
        # retail admin can't reach HQ pages (and vice versa), no silent pass.
        flash('You do not have access to that area.', 'danger')
        return redirect(url_for(admin_home_endpoint(role)))

    # ── Staff (store) session check ───────────────────────────────────────
    if session.get('staff_store'):
        staff_login = session.get('staff_login')
        if session.get('staff_shared'):
            valid_identity = (staff_login
                              and db.get_store_by_email(staff_login) == session['staff_store']
                              and db.get_user(staff_login) is None)
        else:
            identity = db.get_session_user(session.get('uid'))
            valid_identity = bool(
                identity and identity['is_active']
                and identity['auth_version'] == session.get('auth_version')
                and identity['login'].lower() == (staff_login or '').lower()
                and db.get_store_by_email(identity['login']) == session['staff_store'])
        if not valid_identity:
            session.clear()
            flash('Your access changed. Please log in again.', 'info')
            return redirect(url_for('landing'))
        # Check inactivity timeout
        last_active = session.get('staff_last_active', 0)
        if time.time() - last_active > STAFF_SESSION_TIMEOUT:
            session.pop('staff_store', None)
            session.pop('staff_last_active', None)
            flash('Session expired — please log in again.', 'info')
            return redirect(url_for('staff_login'))
        # Refresh activity timestamp
        session['staff_last_active'] = time.time()
        # A store session can only access the staff portal
        if ep in STAFF_ENDPOINTS:
            return
        # Block everything else
        flash('You do not have access to this area.', 'danger')
        return redirect(url_for('portal_store'))

    # ── Cardholder (credit-card portal) session check ─────────────────────
    if session.get('cc_user'):
        identity = db.get_session_user(session.get('uid'))
        if (not identity or not identity['is_active']
                or identity['auth_version'] != session.get('auth_version')
                or identity['login'].lower() != session['cc_user'].lower()):
            session.clear()
            flash('Your access changed. Please log in again.', 'info')
            return redirect(url_for('landing'))
        last_active = session.get('cc_last_active', 0)
        if time.time() - last_active > CC_SESSION_TIMEOUT:
            session.pop('cc_user', None)
            session.pop('cc_last_active', None)
            flash('Session expired — please log in again.', 'info')
            return redirect(url_for('landing'))
        session['cc_last_active'] = time.time()
        if ep in (CC_PORTAL_ENDPOINTS | RM_PORTAL_ENDPOINTS):
            return
        flash('You do not have access to this area.', 'danger')
        return redirect(url_for(portal_home_endpoint(session['cc_user'])))

    # ── Everyone else: the single login at / (carry the intended path) ────
    return redirect(url_for('landing', next=request.path))


# ── Content-addressed static URLs ────────────────────────────────────────────
# Static assets used to be served `Cache-Control: no-cache`, so the browser
# revalidated all ten-odd CSS/JS files on EVERY navigation — and because authed
# pages are no-store, every navigation is a real round trip: ten conditional
# requests before the page can paint, on a store tablet's connection. They can be
# cached for a year instead, but only if a changed file gets a NEW URL: otherwise a
# deploy would serve last month's stylesheet out of everyone's browser cache
# with no way to bust it (a redeploy cannot reach into a browser).
#
# So `url_for('static', ...)` grows a `?v=<content hash>` here, centrally —
# NOT in the templates. Two reasons it's done this way:
#   * 76 url_for('static') calls across templates/ would each need editing, and
#     a missed one would silently pin a stale asset for a year;
#   * several templates already append their own hand-maintained `?v=3.1`
#     AFTER the url_for() call. That literal ends up inside our value
#     (`?v=<hash>?v=3.1`) — still a single, valid query string, still unique per
#     content change, and those hand-rolled versions become redundant (they can
#     be deleted whenever someone is in those templates anyway).
# The hash is over the file's BYTES, so identical content across deploys keeps
# the same URL and a one-byte edit changes it. Memoised per process, keyed on
# (mtime, size) so editing a file in dev re-hashes without a restart.
_STATIC_FINGERPRINTS = {}


def static_fingerprint(filename):
    """Short content hash of `filename` under static/, or None if unreadable.

    None means "don't version this URL" — a typo'd filename then behaves exactly
    as it did before (404, not cached), instead of raising during rendering.
    """
    path = safe_join(app.static_folder or '', filename)
    if path is None:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _STATIC_FINGERPRINTS.get(filename)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b''):
                digest.update(chunk)
    except OSError:
        return None
    # 10 hex chars = 40 bits. This is a cache key, not a signature; the only
    # cost of an (astronomically unlikely) collision is one stale asset.
    fingerprint = digest.hexdigest()[:10]
    _STATIC_FINGERPRINTS[filename] = (stamp, fingerprint)
    return fingerprint


@app.url_defaults
def add_static_fingerprint(endpoint, values):
    """Append the content hash to every generated /static/... URL."""
    if endpoint != 'static' or 'v' in values:
        return
    filename = values.get('filename')
    if not filename:
        return
    fingerprint = static_fingerprint(filename)
    if fingerprint:
        values['v'] = fingerprint


def _static_cache_control():
    """Cache policy for a /static/... response.

    A year + `immutable` is only safe for a URL that actually carries THIS
    file's current fingerprint. Anything else — a hand-typed /static/style.css,
    or a URL still holding last deploy's hash — falls back to the previous
    revalidate-every-time policy, so a mistake costs a round trip rather than
    pinning a stale asset in someone's browser for a year.
    """
    filename = (request.view_args or {}).get('filename')
    fingerprint = static_fingerprint(filename) if filename else None
    # startswith, not ==: a template that appends its own legacy `?v=3.1` after
    # url_for() makes the value '<hash>?v=3.1'.
    if fingerprint and request.args.get('v', '').startswith(fingerprint):
        return 'public, max-age=31536000, immutable'
    return 'no-cache'


@app.after_request
def set_security_headers(resp):
    """Defence-in-depth headers on every response.

    The CSP still allows 'unsafe-inline' for script/style while the staged migration
    in CSP_HARDENING_CHECKLIST.md removes the verified baseline of 116 event-handler
    attributes, 32 executable script blocks, 14 style blocks, and 1,913 style
    attributes. tools/csp_inventory.py and tests/test_csp_inventory.py prevent that
    debt from increasing during the migration. Meanwhile everything else is locked
    to 'self' (all JS/CSS vendored), Jinja autoescapes and there is no
    render_template_string / |safe, so the practical XSS surface is low. The
    directives below + the extra headers are the current hardening.
    """
    resp.headers.setdefault('Content-Security-Policy',
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; object-src 'none'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    # Feature lockdown: switch off powerful browser APIs the app never uses.
    # Camera is deliberately LEFT UNLISTED (defaults to same-origin) so the credit-
    # card portal's phone-camera receipt capture keeps working — only genuinely
    # unused features are disabled here.
    resp.headers.setdefault('Permissions-Policy',
        'geolocation=(), microphone=(), payment=(), usb=(), '
        'magnetometer=(), gyroscope=(), accelerometer=()')
    # Cross-origin isolation. The app is fully self-contained (all assets 'self',
    # no cross-origin popups, embeds or OAuth flows), so these are safe and stop
    # other origins opening/embedding our pages or reading our resources.
    resp.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
    resp.headers.setdefault('Cross-Origin-Resource-Policy', 'same-origin')
    if IS_PRODUCTION:
        resp.headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
    # No authed page may land in a shared/proxy cache or survive in the browser
    # after logout / an account switch. Tag EVERY non-static response 'no-store'
    # (login page included) — otherwise a cached portal page (e.g. a cardholder's
    # profile) can be re-served from cache to a different user in the same browser
    # without ever hitting the server. Only static assets stay cacheable.
    # Inline receipt/attachment files must stay viewable in the browser's built-in
    # PDF viewer, which BREAKS on 'no-store' (its range / second-fetch requests
    # can't be served from a no-store response → a blank page or "failed to load
    # PDF"). Give just those file responses a private-but-revalidated policy: the
    # browser may cache locally, but MUST re-request before reuse — and that
    # re-request re-runs the per-file access check (_require_card_access), so a
    # different user still can't be served another user's receipt from cache after
    # an account switch. Also relax object-src to 'self' for these responses so the
    # PDF plugin can instantiate (the strict CSP's object-src 'none' can block it);
    # the file is a validated image/PDF served with a fixed content-type + nosniff,
    # so this can't introduce script execution.
    if request.endpoint in INLINE_FILE_ENDPOINTS:
        resp.headers['Cache-Control'] = 'private, no-cache, max-age=0, must-revalidate'
        # Receipts are VIEWED in the same-origin slide-out preview iframe (see the
        # cc-rcpt-preview panel). The global 'X-Frame-Options: DENY' +
        # 'frame-ancestors none' blank that iframe — so the PDF neither previews nor
        # (because the click is intercepted) opens in a tab. Allow SAME-ORIGIN
        # framing here (still no cross-site embedding), and object-src 'self' so the
        # PDF plugin can instantiate. Served bytes are a validated image/PDF with a
        # fixed content-type + nosniff, so no script-execution risk.
        resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
        resp.headers['Content-Security-Policy'] = (
            "default-src 'none'; img-src 'self'; object-src 'self'; "
            "style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'self'")
    elif request.endpoint == 'static':
        # Fingerprinted assets are cacheable for a year; see _static_cache_control.
        resp.headers['Cache-Control'] = _static_cache_control()
    else:
        resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.context_processor
def inject_globals():
    now = datetime.now()
    ep = request.endpoint or ''
    # Only offer the workspaces this admin's role may enter. Gated by the same
    # rule the request guard uses, so the switcher never shows a tab the admin
    # would be bounced from: super sees every group; retail sees Deductions
    # (Retail) + Cash Recon; HQ sees Deductions (HQ) only; Credit Cards is
    # super-only. Non-admins (dev-mode / staff base) see all.
    if session.get('admin'):
        # No 'super' fallback: require_login guarantees an admin session carries a
        # role, so a missing one here means "show nothing", never "show everything".
        role = session.get('admin_role')
        visible = [ws for ws in WORKSPACES if admin_endpoint_allowed(ws['endpoint'], role)]
    else:
        visible = list(WORKSPACES)
    current = visible[0] if visible else WORKSPACES[0]
    for ws in visible:
        if any(p in ep for p in ws['prefixes']):
            current = ws
            break
    # One portal identity may hold several capabilities (RM cash view, one or
    # more cards, and optionally an admin role). Expose a single permission-aware
    # model to both cash_base and portal_base so deep pages keep the same switcher.
    portal_caps = {
        'is_rm': False, 'stores': [], 'has_cards': False, 'card_count': 0,
        'card_task_count': 0, 'rm_name': None,
    }
    portal_identity = None
    portal_email = (session.get('cc_user') or '').strip().lower()
    if portal_email:
        portal_caps = db.rm_capabilities(portal_email)
        user = db.get_user(portal_email)
        portal_identity = {
            'email': portal_email,
            'name': ((user['display_name'] if user else None)
                     or portal_caps.get('rm_name') or portal_email),
        }
    # Unclaimed staff requests, for the sidebar badge. One indexed COUNT, and only
    # for admin roles that can reach /requests — the portal shells never pay for it.
    requests_pending = 0
    # `not ADMIN_AUTH_ENABLED` is the local dev-server case (ADMIN_AUTH=0), where
    # there is no admin session at all but every page is open.
    if ((session.get('admin') or not ADMIN_AUTH_ENABLED)
            and session.get('admin_role') in (None, 'super', 'retail')):
        try:
            # Imported here, not at module scope: northwind.deductions imports core.
            from northwind.deductions import requests as staff_requests
            conn = db.get_db()
            try:
                requests_pending = staff_requests.pending_count(conn, current_admin_sector())
            finally:
                conn.close()
        except Exception:
            requests_pending = 0   # pre-migration boot must never break the shell

    return {'all_stores': db.get_stores(), 'hq_stores': HQ_STORES, 'now': now,
            'requests_pending': requests_pending,
            'page_year': now.year, 'page_month': now.month,
            'workspaces': visible, 'current_workspace': current,
            'admin_auth_enabled': ADMIN_AUTH_ENABLED,
            'portal_caps': portal_caps, 'portal_identity': portal_identity}


# ── Dashboard ──────────────────────────────────────────────────────────────
