"""Authentication routes — admin + employee login.

Admin routes:
  /             landing choice screen — public
  /admin/login  username + password (accounts are created by admins only —
                see ensure_bootstrap_admin() for how the first one comes to be)
  /admin/logout

Employee (staff) routes:
  /staff/login  two-step: store email → shared store password
  /staff/logout

Admin management routes:
  /admin/staff-logins       view/manage store emails (the staff-login usernames)
  /admin/staff-logins/...   add/delete/bulk-import store emails
  /admin/admins             manage admin user accounts
"""
import hmac
import os
import secrets
import time
from flask import render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from northwind import core
from northwind.data import database as db
from northwind.services import security
from northwind.services import mailer
from northwind.deductions.pagination import paginate
from northwind.core import app, ADMIN_AUTH_ENABLED

# Minimum password length — single source of truth in security.py so every
# password-setting path (admins here, cardholder/RM passwords elsewhere) shares it.
MIN_PASSWORD_LENGTH = security.MIN_PASSWORD_LENGTH

# Shared staff-portal password: every store logs in with its own email plus this
# one password, and sees only its own staff. Deliberately store-level (not
# per-person) for now; override via NW_STAFF_PASSWORD without a code change.
STAFF_PORTAL_PASSWORD = os.environ.get('NW_STAFF_PASSWORD', '')
if not STAFF_PORTAL_PASSWORD and core.IS_PRODUCTION:
    # There is no fallback value to inherit: unset means the shared path is
    # simply closed, and a store can only sign in with its own credential.
    # That is the safe direction to fail, and it is not silent.
    print("WARNING: NW_STAFF_PASSWORD is not set — the shared staff-portal "
          "password is disabled. Stores need their own credentials.")

# Hashed once at import and checked whenever an unknown admin username is
# submitted, so "user exists" and "user doesn't exist" take the same time —
# otherwise response timing would let an attacker enumerate valid usernames.
_DUMMY_HASH = generate_password_hash('timing-equalizer-not-a-real-account',
                                     method='pbkdf2:sha256')


def _safe_next(target, role='super'):
    """Resolve a post-login redirect.

    Only allows local targets (avoids open-redirect), and only honours one the
    admin's role may actually reach — otherwise falls back to the role's home so
    a scoped admin never lands on a page they'll immediately be bounced from.
    """
    home = url_for(core.admin_home_endpoint(role))
    if target and target.startswith('/') and not target.startswith('//'):
        # Resolve the target path to an endpoint so we can role-check it.
        try:
            endpoint = app.url_map.bind('').match(target.split('?', 1)[0])[0]
        except Exception:
            return home
        if core.admin_endpoint_allowed(endpoint, role):
            return target
        return home
    return home


def _fmt_lockout(seconds):
    """Human-friendly remaining-lockout string."""
    minutes = (seconds + 59) // 60
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


# Readable, shareable generated password (NORTHWIND-7K4P-9QXM); no O/0/I/1/L confusion.
# Mirrors the cardholder/RM generators so all one-off credentials look the same.
_PW_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def _generate_password():
    part = lambda: ''.join(secrets.choice(_PW_ALPHABET) for _ in range(4))
    return f'NORTHWIND-{part()}-{part()}'


def _link_portal_if_cardholder(user):
    """If this admin's email is ALSO a cardholder / RM, give them a portal
    session alongside their admin session, so an admin who holds a company card
    (or manages stores as an RM) can still reach their own portal. The gate in
    core.require_login honours session['cc_user'] for portal endpoints even when
    an admin session is present; each portal handler stays scoped to this email."""
    admin_email = ((user['email'] if 'email' in user.keys() else None) or '').strip().lower()
    if not admin_email:
        return
    caps = db.rm_capabilities(admin_email)
    if caps['has_cards'] or caps['is_rm']:
        session['cc_user'] = admin_email
        session['cc_last_active'] = time.time()


def ensure_bootstrap_admin():
    """Create the first admin account from environment variables.

    There is deliberately NO public signup: accounts are only created here or
    by an existing admin at /admin/admins. When the database has no admins
    (fresh install, or a host with an ephemeral filesystem),
    set ADMIN_USERNAME and ADMIN_PASSWORD (optionally ADMIN_DISPLAY_NAME)
    and the account is created on boot. Runs from init_app() after migrations.
    """
    if db.admin_user_count() > 0:
        return
    username = os.environ.get('ADMIN_USERNAME', '').strip()
    password = os.environ.get('ADMIN_PASSWORD', '')
    if not username or not password:
        print("WARNING: no admin accounts exist and ADMIN_USERNAME/ADMIN_PASSWORD "
              "are not set — nobody can log in to the admin area.")
        return
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"WARNING: ADMIN_PASSWORD is shorter than {MIN_PASSWORD_LENGTH} "
              "characters — bootstrap admin not created.")
        return
    display_name = os.environ.get('ADMIN_DISPLAY_NAME', '').strip() or username
    db.create_admin_user(username, display_name,
                         generate_password_hash(password, method='pbkdf2:sha256'))
    print(f"Bootstrap admin account created from environment: {username}")


# ── Landing ───────────────────────────────────────────────────────────────────

@app.route('/', methods=['GET', 'POST'])
def landing():
    """One login screen for everyone. The same identifier+password form serves
    both admins (username) and stores (store email + shared staff password); we
    detect which by what the identifier matches, and land them accordingly."""
    nxt = request.args.get('next') or request.form.get('next')

    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        password = request.form.get('password', '')
        key = security.make_key('login', identifier.lower(), request.remote_addr)
        locked = security.seconds_locked(key)
        if locked:
            flash(f'Too many failed attempts. Try again in {_fmt_lockout(locked)}.', 'danger')
            return render_template('landing.html', identifier=identifier, next=nxt)

        # Track whether we've already spent one real password-hash verification,
        # so the failure path can top up with a dummy hash to a constant cost —
        # a valid identifier and an unknown one must take the same time (no
        # user-enumeration by timing across ANY of the three identity types).
        did_hash = False

        # 1) Admin username?
        user = db.get_admin_user(identifier)
        if user:
            did_hash = True
            if check_password_hash(user['password_hash'], password):
                if not user['is_active']:
                    flash('This account has been disabled. Contact a super administrator.', 'danger')
                    return render_template('landing.html', identifier=identifier, next=nxt)
                security.reset(key)
                session.clear()                       # anti session-fixation
                session.permanent = True
                session['admin'] = True
                session['uid'] = user['id']
                session['admin_username'] = user['username']
                session['admin_display_name'] = user['display_name']
                role = user['role'] if 'role' in user.keys() else 'super'
                session['admin_role'] = role or 'super'
                session['auth_version'] = user['auth_version']
                session['admin_last_active'] = time.time()
                _link_portal_if_cardholder(user)
                return redirect(_safe_next(nxt, role))

        # 2) Store email? Accept the store's OWN password if one has been set
        #    (phase 3), otherwise fall back to the shared staff password. This
        #    lets stores migrate off the shared secret one at a time.
        store_name = db.get_store_by_email(identifier.lower())
        if store_name:
            store_user = db.get_user(identifier.lower())
            matched = False
            if store_user:
                did_hash = True
                matched = (check_password_hash(store_user['password_hash'], password)
                           and bool(store_user['is_active']))
            elif (STAFF_PORTAL_PASSWORD
                  and hmac.compare_digest(password, STAFF_PORTAL_PASSWORD)):
                matched = True
            if matched:
                security.reset(key)
                session.clear()
                # Persistent cookie (see staff_login for why a store MUST have one).
                session.permanent = True
                session['staff_store'] = store_name
                session['staff_login'] = identifier.lower()
                if store_user:
                    session['uid'] = store_user['id']
                    session['auth_version'] = store_user['auth_version']
                else:
                    session['staff_shared'] = True
                session['staff_last_active'] = time.time()
                return redirect(url_for('portal_store'))

        # 3) Portal person (cardholder and/or Regional Manager) email + their own
        #    password? One credential backs both personas; a person is admitted if
        #    they hold cards OR are an ACTIVE RM. Skip this when the identifier is a
        #    store email (handled above) — that keeps a store login to exactly one
        #    hash and never mis-routes a store into the portal.
        email = identifier.lower()
        cc_login = None if store_name else db.get_cc_user(email)
        if cc_login:
            did_hash = True
            if check_password_hash(cc_login['password_hash'], password):
                if not cc_login['is_active']:
                    flash('This account has been disabled. Contact the office.', 'danger')
                    return render_template('landing.html', identifier=identifier, next=nxt)
                caps = db.rm_capabilities(email)
                if caps['has_cards'] or caps['is_rm']:
                    security.reset(key)
                    session.clear()
                    # Persistent cookie, for the same reason a store needs one (see
                    # staff_login): cardholders and RMs photograph and upload receipts
                    # from a phone or tablet, where a browser-session cookie is dropped
                    # the moment the app is closed or the tab evicted — and the upload
                    # in progress dies with it. CC_SESSION_TIMEOUT still caps idle time.
                    session.permanent = True
                    session['cc_user'] = email
                    session['uid'] = cc_login['id']
                    session['auth_version'] = cc_login['auth_version']
                    session['cc_last_active'] = time.time()
                    return redirect(url_for(core.portal_home_endpoint(email)))

        # Failed. Equalise timing: if no identity matched, spend the same one
        # hash a real verification would have cost, then record the failure.
        if not did_hash:
            check_password_hash(_DUMMY_HASH, password)
        security.record_failure(key)
        flash('Incorrect login details. Please check and try again.', 'danger')
        return render_template('landing.html', identifier=identifier, next=nxt)

    # GET — bounce already-authenticated sessions to their home.
    if session.get('admin'):
        return redirect(url_for(core.admin_home_endpoint(session.get('admin_role', 'super'))))
    if session.get('staff_store'):
        return redirect(url_for('portal_store'))
    if session.get('cc_user'):
        return redirect(url_for(core.portal_home_endpoint(session['cc_user'])))
    return render_template('landing.html', next=nxt)


# ── Admin login (individual accounts) ────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if not ADMIN_AUTH_ENABLED:
        return redirect(url_for('dashboard'))

    nxt = request.args.get('next') or request.form.get('next')

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        key = security.make_key('admin', username, request.remote_addr)
        locked = security.seconds_locked(key)
        if locked:
            flash(f'Too many failed attempts. Try again in {_fmt_lockout(locked)}.', 'danger')
        else:
            user = db.get_admin_user(username)
            password_ok = check_password_hash(
                user['password_hash'] if user else _DUMMY_HASH, password)
            if user and password_ok:
                if not user['is_active']:
                    flash('This account has been disabled. Contact a super administrator.', 'danger')
                    return render_template('admin_login.html', next=nxt)
                security.reset(key)
                # Fresh session on privilege change (anti session-fixation).
                session.clear()
                session.permanent = True  # subject to PERMANENT_SESSION_LIFETIME
                session['admin'] = True
                session['uid'] = user['id']
                session['admin_username'] = user['username']
                session['admin_display_name'] = user['display_name']
                # Default to 'super' so any pre-roles account keeps full access.
                role = user['role'] if 'role' in user.keys() else 'super'
                session['admin_role'] = role or 'super'
                session['auth_version'] = user['auth_version']
                session['admin_last_active'] = time.time()
                _link_portal_if_cardholder(user)
                return redirect(_safe_next(nxt, role))
            security.record_failure(key)
            flash('Incorrect username or password.', 'danger')

    return render_template('admin_login.html', next=nxt)


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    session.pop('uid', None)
    session.pop('admin_username', None)
    session.pop('admin_display_name', None)
    session.pop('admin_role', None)
    session.pop('admin_last_active', None)
    # An admin who also held a card/RM portal session — clear that too.
    session.pop('cc_user', None)
    session.pop('cc_last_active', None)
    session.pop('auth_version', None)
    flash('Logged out.', 'info')
    return redirect(url_for('landing'))


# ── Employee (staff) login ────────────────────────────────────────────────────

@app.route('/staff/login', methods=['GET', 'POST'])
def staff_login():
    step = 'email'  # start at step 1
    store_name = None

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'check_email':
            email = request.form.get('email', '').strip().lower()
            store_name = db.get_store_by_email(email)
            if store_name:
                step = 'code'  # move to step 2
            else:
                flash('Email not recognised. Please use your store email address.', 'danger')

        elif action == 'login':
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '').strip()
            store_name = db.get_store_by_email(email)
            key = security.make_key('staff', email, request.remote_addr)
            locked = security.seconds_locked(key)
            if locked:
                flash(f'Too many attempts. Try again in {_fmt_lockout(locked)}.', 'danger')
                step = 'code' if store_name else 'email'
            elif not store_name:
                flash('Email not recognised.', 'danger')
            else:
                # The store's own password (phase 3) if set, else the shared one.
                store_user = db.get_user(email)
                matched = bool(store_user and
                               check_password_hash(store_user['password_hash'], password)
                               and store_user['is_active'])
                if (not store_user and STAFF_PORTAL_PASSWORD
                        and hmac.compare_digest(password, STAFF_PORTAL_PASSWORD)):
                    matched = True
                if matched:
                    security.reset(key)
                    # Fresh session on privilege change (anti session-fixation).
                    session.clear()
                    # A store works on a shop-floor TABLET. Without permanent=True
                    # this is a browser-session cookie, which iPadOS drops whenever
                    # Safari is closed or the tab is evicted — so a half-typed cash
                    # entry died mid-POST on a CSRF failure and the store lost the
                    # lot. Permanent makes it a dated cookie that survives that, and
                    # SESSION_REFRESH_EACH_REQUEST slides the expiry forward on every
                    # request, so the real limit is STAFF_SESSION_TIMEOUT (12h idle).
                    session.permanent = True
                    session['staff_store'] = store_name
                    session['staff_login'] = email
                    if store_user:
                        session['uid'] = store_user['id']
                        session['auth_version'] = store_user['auth_version']
                    else:
                        session['staff_shared'] = True
                    session['staff_last_active'] = time.time()
                    return redirect(url_for('portal_store'))
                else:
                    security.record_failure(key)
                    flash('Incorrect password. Please try again.', 'danger')
                    step = 'code'

    return render_template('staff_login.html', step=step, store_name=store_name,
                           email=request.form.get('email', ''))


@app.route('/staff/logout')
def staff_logout():
    session.pop('staff_store', None)
    session.pop('staff_last_active', None)
    session.pop('staff_login', None)
    session.pop('staff_shared', None)
    session.pop('uid', None)
    session.pop('auth_version', None)
    # Legacy per-employee session keys from the old login flow.
    session.pop('employee_id', None)
    session.pop('employee_store', None)
    session.pop('employee_name', None)
    session.pop('employee_last_active', None)
    flash('Logged out.', 'info')
    return redirect(url_for('landing'))


# ── Admin management: staff logins & store emails ─────────────────────────────

@app.route('/admin/staff-logins')
def admin_staff_logins():
    store_emails = db.get_all_store_emails()
    stores = db.get_stores()
    return render_template('admin_staff_logins.html',
                           store_emails=store_emails, stores=stores,
                           own_pw=db.store_password_logins(),
                           new_credential=session.pop('store_new_credential', None))


@app.route('/admin/staff-logins/set-password', methods=['POST'])
def admin_set_store_password():
    """Give a store its OWN login password (phase 3 — off the shared secret).
    Generated unless one is typed; shown once in a copyable panel."""
    email = request.form.get('email', '').strip().lower()
    store = db.get_store_by_email(email)
    if not store:
        flash('Unknown store email.', 'danger')
        return redirect(url_for('admin_staff_logins'))
    typed = (request.form.get('password') or '').strip()
    if typed and len(typed) < MIN_PASSWORD_LENGTH:
        flash(f'Password must be at least {MIN_PASSWORD_LENGTH} characters.', 'danger')
        return redirect(url_for('admin_staff_logins'))
    password = typed or _generate_password()
    db.set_cc_user_password(email, generate_password_hash(password, method='pbkdf2:sha256'))
    session['store_new_credential'] = {'store': store, 'email': email, 'password': password}
    flash(f'{store} now has its own password. Copy it below — it is not stored '
          f"and won't be shown again.", 'success')
    return redirect(url_for('admin_staff_logins'))


@app.route('/admin/staff-logins/clear-password', methods=['POST'])
def admin_clear_store_password():
    """Remove a store's own password — it reverts to the shared staff password."""
    email = request.form.get('email', '').strip().lower()
    store = db.get_store_by_email(email)
    db.clear_store_password(email)
    flash(f"{store or email} reverted to the shared staff password.", 'info')
    return redirect(url_for('admin_staff_logins'))


@app.route('/admin/staff-logins/add-store-email', methods=['POST'])
def admin_add_store_email():
    store = request.form.get('store', '').strip()
    email = request.form.get('email', '').strip().lower()
    if store and email:
        db.upsert_store_email(store, email)
        flash(f'Store email set: {store} → {email}', 'success')
    else:
        flash('Store and email are required.', 'danger')
    return redirect(url_for('admin_staff_logins'))


@app.route('/admin/staff-logins/delete-store-email/<int:email_id>', methods=['POST'])
def admin_delete_store_email(email_id):
    db.delete_store_email(email_id)
    flash('Store email removed.', 'success')
    return redirect(url_for('admin_staff_logins'))


@app.route('/admin/staff-logins/import-store-emails', methods=['POST'])
def admin_import_store_emails():
    """Bulk import store emails. Accepts lines of: Store Name,email@example.com"""
    raw = request.form.get('bulk_emails', '').strip()
    if not raw:
        flash('No data provided.', 'danger')
        return redirect(url_for('admin_staff_logins'))

    data = []
    errors = []
    for i, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = [p.strip() for p in line.replace('\t', ',').split(',')]
        if len(parts) >= 2:
            data.append((parts[0], parts[1]))
        else:
            errors.append(f'Line {i}: could not parse "{line}"')

    if data:
        count = db.bulk_import_store_emails(data)
        flash(f'Imported {count} store email(s).', 'success')
    if errors:
        flash(f'{len(errors)} line(s) skipped: {"; ".join(errors[:5])}', 'warning')
    return redirect(url_for('admin_staff_logins'))


# ── People & Access: unified admin console ────────────────────────────────────
# One super-only page manages admin accounts (create / change role / reset
# password / enable-disable / delete) and shows a read-only roster of EVERY
# login — admins, cardholders and Regional Managers — with their capabilities,
# so there is a single place to see who can access what. Card and store scope
# is still assigned on the card pages / RM page (cross-linked from here), because
# those live with the cards/stores they grant. Self-service password change is a
# separate, all-admin page (admin_change_password) linked from the account menu.

@app.route('/admin/admins')
def admin_manage_admins():
    # Each roster row now carries an access panel with a checkbox per store, so an
    # unpaged roster builds a DOM that grows with logins × stores (46 × 31 was
    # 643 KB of markup). Same window the deduction lists use, "show all" included.
    roster, pager = paginate(db.list_all_users(), noun='logins', per_page=25)
    return render_template('admin_manage_admins.html',
                           admins=db.get_all_admin_users(),
                           roster=roster, pager=pager,
                           me_id=session.get('uid'),
                           new_credential=session.pop('admin_new_credential', None))


@app.route('/admin/admins/add', methods=['POST'])
def admin_add_admin():
    username = request.form.get('username', '').strip()
    display_name = request.form.get('display_name', '').strip()
    password = request.form.get('password', '')
    role = request.form.get('role', 'super')

    if not username or not display_name or not password:
        flash('All fields are required.', 'danger')
    elif role not in db.ADMIN_ROLES:
        flash('Please choose a valid access level.', 'danger')
    elif len(password) < MIN_PASSWORD_LENGTH:
        flash(f'Password must be at least {MIN_PASSWORD_LENGTH} characters.', 'danger')
    elif db.get_user(username):
        flash('That username / email is already taken.', 'danger')
    else:
        db.create_admin_user(username, display_name,
                             generate_password_hash(password, method='pbkdf2:sha256'),
                             role=role)
        flash(f'Admin "{display_name}" created.', 'success')
    return redirect(url_for('admin_manage_admins'))


def _admin_target(user_id):
    """The admin row for user_id (or None), plus the current admin list."""
    admins = db.get_all_admin_users()
    return next((a for a in admins if a['id'] == user_id), None), admins


@app.route('/admin/admins/<int:user_id>/role', methods=['POST'])
def admin_edit_admin_role(user_id):
    role = request.form.get('role', '')
    target, admins = _admin_target(user_id)
    supers = [a for a in admins if (a['role'] or 'super') == 'super']
    if role not in db.ADMIN_ROLES:
        flash('Please choose a valid access level.', 'danger')
    elif not target:
        flash('Unknown admin.', 'danger')
    elif (target['role'] or 'super') == 'super' and role != 'super' and len(supers) <= 1:
        flash('Cannot lower the last full-access (super) admin.', 'danger')
    else:
        db.set_admin_role(user_id, role)
        flash(f"Access level updated for {target['display_name']}.", 'success')
    return redirect(url_for('admin_manage_admins'))


@app.route('/admin/admins/<int:user_id>/reset', methods=['POST'])
def admin_reset_admin_password(user_id):
    target, _ = _admin_target(user_id)
    if not target:
        flash('Unknown admin.', 'danger')
        return redirect(url_for('admin_manage_admins'))
    typed = (request.form.get('password') or '').strip()
    if typed and len(typed) < MIN_PASSWORD_LENGTH:
        flash(f'Password must be at least {MIN_PASSWORD_LENGTH} characters.', 'danger')
        return redirect(url_for('admin_manage_admins'))
    password = typed or _generate_password()
    db.set_user_password(user_id, generate_password_hash(password, method='pbkdf2:sha256'))
    # Show once in a copyable panel (mirrors the cardholder/RM credential flow).
    session['admin_new_credential'] = {'username': target['username'], 'password': password}
    flash(f"Password reset for {target['display_name']}. Copy it below — "
          f"it is not stored and won't be shown again.", 'success')
    return redirect(url_for('admin_manage_admins'))


@app.route('/admin/admins/<int:user_id>/active', methods=['POST'])
def admin_set_admin_active(user_id):
    target, _ = _admin_target(user_id)
    if not target:
        flash('Unknown admin.', 'danger')
        return redirect(url_for('admin_manage_admins'))
    activate = request.form.get('active') == '1'
    current_user = db.get_admin_user(session.get('admin_username', ''))
    if not activate:
        if current_user and current_user['id'] == user_id:
            flash('You cannot disable your own account.', 'danger')
            return redirect(url_for('admin_manage_admins'))
        if (target['role'] or 'super') == 'super' and db.active_super_count() <= 1:
            flash('Cannot disable the last active full-access (super) admin.', 'danger')
            return redirect(url_for('admin_manage_admins'))
    db.set_user_active(user_id, activate)
    flash(f"{target['display_name']} {'enabled' if activate else 'disabled'}.", 'success')
    return redirect(url_for('admin_manage_admins'))


@app.route('/admin/admins/delete/<int:user_id>', methods=['POST'])
def admin_delete_admin(user_id):
    target, admins = _admin_target(user_id)
    current_user = db.get_admin_user(session.get('admin_username', ''))
    supers = [a for a in admins if (a['role'] or 'super') == 'super']
    if current_user and current_user['id'] == user_id:
        flash('You cannot delete your own account.', 'danger')
    elif db.admin_user_count() <= 1:
        flash('Cannot delete the last admin account.', 'danger')
    elif target and (target['role'] or 'super') == 'super' and len(supers) <= 1:
        # Never strand the system without a full-access admin — only a super can
        # manage admins, backups and the database.
        flash('Cannot delete the last full-access (super) admin.', 'danger')
    else:
        db.delete_admin_user(user_id)
        flash('Admin account deleted.', 'success')
    return redirect(url_for('admin_manage_admins'))




# ── People & Access: edit one person's access in place ────────────────────────
# Every capability a login can hold is granted somewhere else in the app (admin
# roles here, cards on a card's page, RM scope on the Regional Managers page).
# That meant "make Lethabo an RM as well" required creating them again on another
# page. These two routes let the roster edit an EXISTING login instead: same
# primitives, same guards, one screen.

def _person(user_id):
    """The roster row for user_id (or None). One batched read, so the handler sees
    the person's cards, RM scope and store login as well as their admin roles."""
    return next((u for u in db.list_all_users() if u['id'] == user_id), None)


def _is_me(user_id):
    me = db.get_admin_user(session.get('admin_username', ''))
    return bool(me and me['id'] == user_id)


@app.route('/admin/people/<int:user_id>/access', methods=['POST'])
def admin_person_access(user_id):
    """Apply the access panel: name, admin role, RM + their stores, login status."""
    person = _person(user_id)
    if not person:
        flash('Unknown login.', 'danger')
        return redirect(url_for('admin_manage_admins'))

    email = (person['email'] or person['login'] or '').strip().lower()
    changes, refusals = [], []

    # ── Name ──────────────────────────────────────────────────────────────────
    name = (request.form.get('display_name') or '').strip()
    if name != (person['display_name'] or ''):
        db.set_user_display_name(user_id, name)
        changes.append('renamed to %s' % (name or '(no name)'))

    # ── Admin access ──────────────────────────────────────────────────────────
    wanted_role = (request.form.get('admin_role') or '').strip()
    current_role = person['role']
    if wanted_role != current_role:
        if wanted_role and wanted_role not in db.ADMIN_ROLES:
            refusals.append('that is not a valid admin access level')
        elif _is_me(user_id):
            # Any change to your OWN level locks you out of this page — dropping
            # from full access to retail does it just as effectively as removing
            # the role. Another full-access admin has to make the change.
            refusals.append('you cannot change your own admin access — '
                            'another full-access admin must do it')
        elif not wanted_role:
            # Removing admin rights, not the person: delete_admin_user keeps the
            # login when it is also a cardholder / RM / store login.
            if current_role == 'super' and db.active_super_count() <= 1:
                refusals.append('this is the last full-access admin')
            else:
                db.delete_admin_user(user_id)
                changes.append('admin access removed')
        else:
            if current_role == 'super' and wanted_role != 'super' \
                    and db.active_super_count() <= 1:
                refusals.append('this is the last full-access admin')
            else:
                db.set_admin_role(user_id, wanted_role)
                changes.append('admin access set to %s'
                               % {'super': 'full', 'retail': 'retail only',
                                  'hq': 'HQ only'}[wanted_role])

    # ── Regional Manager ──────────────────────────────────────────────────────
    wants_rm = request.form.get('is_rm') == '1'
    if not email and wants_rm:
        refusals.append('an RM is identified by email, and this login has none')
        wants_rm = False
    if wants_rm and not person['is_rm']:
        if person['rm_known']:
            db.set_rm_active(email, 1)
        else:
            db.upsert_rm_user(email, name or person['display_name'] or email, active=1)
        changes.append('now a Regional Manager')
    elif not wants_rm and person['is_rm']:
        db.set_rm_active(email, 0)
        changes.append('no longer a Regional Manager')

    # Store scope. Assigning a store moves it off whoever held it (rm_stores is
    # keyed by store), which is the same behaviour as the Regional Managers page.
    if email:
        chosen = set(request.form.getlist('rm_store')) if wants_rm else set()
        held = set(person['rm_stores'])
        all_stores = set(db.get_stores())
        for store in sorted((chosen | held) & all_stores):
            if store in chosen and store not in held:
                db.assign_store_rm(store, email)
            elif store not in chosen and store in held:
                # Releasing rather than leaving stores with someone who is no
                # longer an RM: a store that looks covered but isn't is worse
                # than an obviously unassigned one.
                db.assign_store_rm(store, None)
        added, removed = len(chosen - held), len(held - chosen)
        if added:
            changes.append('%d store%s assigned' % (added, '' if added == 1 else 's'))
        if removed:
            changes.append('%d store%s released' % (removed, '' if removed == 1 else 's'))

    # ── Login status ──────────────────────────────────────────────────────────
    activate = request.form.get('login_active') == '1'
    if activate != bool(person['is_active']):
        if not activate and _is_me(user_id):
            refusals.append('you cannot disable your own login')
        elif not activate and person['role'] == 'super' and db.active_super_count() <= 1:
            refusals.append('this is the last active full-access admin')
        else:
            db.set_user_active(user_id, activate)
            changes.append('login %s' % ('enabled' if activate else 'disabled'))

    who = name or person['display_name'] or person['login']
    if changes:
        flash('%s: %s.' % (who, ' · '.join(changes)), 'success')
    if refusals:
        flash('Not changed — %s.' % '; '.join(refusals), 'danger')
    if not changes and not refusals:
        flash('Nothing to change for %s.' % who, 'info')
    return redirect(url_for('admin_manage_admins'))


@app.route('/admin/people/<int:user_id>/password', methods=['POST'])
def admin_person_password(user_id):
    """Reset any login's password — cardholders and RMs included, not just admins."""
    person = _person(user_id)
    if not person:
        flash('Unknown login.', 'danger')
        return redirect(url_for('admin_manage_admins'))
    typed = (request.form.get('password') or '').strip()
    if typed and len(typed) < MIN_PASSWORD_LENGTH:
        flash(f'Password must be at least {MIN_PASSWORD_LENGTH} characters.', 'danger')
        return redirect(url_for('admin_manage_admins'))
    password = typed or _generate_password()
    db.set_user_password(user_id, generate_password_hash(password, method='pbkdf2:sha256'))
    session['admin_new_credential'] = {'username': person['login'], 'password': password}
    flash('Password reset for %s. Copy it below — it is not stored and will not be '
          'shown again.' % (person['display_name'] or person['login']), 'success')
    return redirect(url_for('admin_manage_admins'))


@app.route('/admin/change-password', methods=['GET', 'POST'])
def admin_change_password():
    """Self-service password change for the logged-in admin (any role)."""
    me = db.get_admin_user(session.get('admin_username', ''))
    home = core.admin_home_endpoint(session.get('admin_role', 'super'))
    if not me:
        flash('Could not load your account.', 'danger')
        return redirect(url_for(home))
    if request.method == 'POST':
        current = request.form.get('current_password', '')
        new = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        if not check_password_hash(me['password_hash'], current):
            flash('Your current password is incorrect.', 'danger')
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash(f'New password must be at least {MIN_PASSWORD_LENGTH} characters.', 'danger')
        elif new != confirm:
            flash('The new passwords do not match.', 'danger')
        else:
            db.set_user_password(me['id'], generate_password_hash(new, method='pbkdf2:sha256'))
            # Keep this verified session alive while every other session carrying
            # the previous version is revoked on its next request.
            session['auth_version'] = db.get_session_user(me['id'])['auth_version']
            flash('Your password has been changed.', 'success')
            return redirect(url_for(home))
    return render_template('admin_change_password.html')


# ── Email ─────────────────────────────────────────────────────────────────────
# Setup + a manual test send. There is deliberately no automatic sending wired
# up: the mailer is proven by hand here before cardholder reminders are sent.

@app.route('/admin/email')
def admin_email():
    return render_template('admin_email.html', mail=mailer.status(),
                           sent=session.pop('mail_test_sent', None))


@app.route('/admin/email/test', methods=['POST'])
def admin_email_test():
    """Send one email, to an address the admin types. Never sends on its own."""
    to = (request.form.get('to') or '').strip()
    if not to:
        flash('Enter an address to send the test to.', 'warning')
        return redirect(url_for('admin_email'))
    try:
        result = mailer.send(
            to,
            'NORTHWIND app — test email',
            '<p>This is a test from the Northwind Deductions app.</p>'
            '<p>It was composed as <strong>%s</strong>. This build has no mail '
            'provider, so the message went to the application log rather than '
            'to an inbox.</p>' % mailer.SENDER)
    except mailer.MailError as exc:
        flash('Could not send: %s' % exc, 'danger')
        return redirect(url_for('admin_email'))
    session['mail_test_sent'] = result['to'][0]
    flash('Test email sent to %s.' % result['to'][0]
          + (' (dry run — nothing actually left the building.)'
             if result['dry_run'] else ''), 'success')
    return redirect(url_for('admin_email'))
