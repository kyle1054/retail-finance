"""Stores, store logins, employees and regional managers."""
import re

from . import names
from .calendar_math import iso_day, shift

# Two stores are named by migration 0046's seeded Shopify aliases but are not in
# the store list any migration creates, so a fresh database has a mapping that
# resolves to nothing. Seeding them keeps that migration's own guard test
# meaningful instead of vacuous.
EXTRA_STORES = ('Baymouth', 'Woodhaven')

HQ_LOCATIONS = ('HQ', 'DC')

EMAIL_DOMAIN = 'northwind-apparel.example'


def _slug(store):
    return re.sub(r'[^a-z0-9]+', '-', store.lower()).strip('-')


def store_login(store):
    return 'store.%s@%s' % (_slug(store), EMAIL_DOMAIN)


def seed_stores(conn):
    """Ensure the store list is complete and every store carries finance metadata.

    Returns the ordered list of store names.
    """
    for name in EXTRA_STORES:
        conn.execute("INSERT OR IGNORE INTO stores (name) VALUES (?)", (name,))
    stores = [r[0] for r in conn.execute("SELECT name FROM stores ORDER BY name")]
    for index, store in enumerate(stores, 1):
        conn.execute(
            "UPDATE stores SET store_code=COALESCE(store_code, ?), "
            "xero_tracking_name=COALESCE(xero_tracking_name, ?) WHERE name=?",
            ('S%03d' % index, store, store))
    return stores


def seed_store_logins(conn, stores):
    """One shared shop-floor login per store.

    Deliberately written to ``store_emails`` only, with no matching ``users``
    row: that is the "store with an address but no password yet" state, and the
    staff-portal fixture looks for exactly it.
    """
    for store in stores:
        conn.execute(
            "INSERT OR IGNORE INTO store_emails (store, email) VALUES (?, ?)",
            (store, store_login(store)))
    return {store: store_login(store) for store in stores}


def seed_employees(conn, profile, stores):
    """Create the roster.

    Returns ``{'retail': [...], 'hq': [...], 'terminated': [...]}`` where each
    entry is ``{'id', 'full_name', 'store', 'sector'}``, in creation order.
    """
    retail_n = profile['retail_employees']
    hq_n = profile['hq_employees']
    left_n = profile['terminated_employees']
    total = retail_n + hq_n + left_n

    seen, roster = set(), []
    for index in range(total):
        full_name = names.person(index)
        assert full_name not in seen, 'duplicate synthetic name %r' % full_name
        seen.add(full_name)
        roster.append(full_name)

    people = {'retail': [], 'hq': [], 'terminated': []}
    emp_index = 0

    def _insert(full_name, store, sector, job_title, status, joined_offset,
                terminated_offset=None):
        nonlocal emp_index
        emp_index += 1
        emp_id = 'EMP-%04d' % emp_index
        conn.execute(
            "INSERT INTO employees (id, full_name, current_store, job_title, "
            "status, sector, created_at, terminated_at, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (emp_id, full_name, store, job_title, status, sector,
             iso_day(joined_offset),
             iso_day(terminated_offset) if terminated_offset is not None else None,
             None))
        conn.execute(
            "INSERT INTO store_history (employee_id, store, from_date, to_date) "
            "VALUES (?,?,?,?)",
            (emp_id, store, iso_day(joined_offset),
             iso_day(terminated_offset) if terminated_offset is not None else None))
        return {'id': emp_id, 'full_name': full_name, 'store': store,
                'sector': sector, 'status': status}

    cursor = 0
    for i in range(retail_n):
        store = stores[i % len(stores)]
        title = names.JOB_TITLES[i % len(names.JOB_TITLES)]
        people['retail'].append(_insert(
            roster[cursor], store, 'retail', title, 'active',
            joined_offset=-(900 - i * 7)))
        cursor += 1

    for i in range(hq_n):
        # Enough of both so the HQ/DC split filter has something on each side.
        location = HQ_LOCATIONS[0] if i % 3 != 2 else HQ_LOCATIONS[1]
        title = names.HQ_JOB_TITLES[i % len(names.HQ_JOB_TITLES)]
        people['hq'].append(_insert(
            roster[cursor], location, 'hq', title, 'active',
            joined_offset=-(800 - i * 11)))
        cursor += 1

    for i in range(left_n):
        store = stores[(i * 5) % len(stores)]
        title = names.JOB_TITLES[i % len(names.JOB_TITLES)]
        people['terminated'].append(_insert(
            roster[cursor], store, 'retail', title, 'terminated',
            joined_offset=-(1200 - i * 30), terminated_offset=-(120 + i * 13)))
        cursor += 1

    return people


def seed_employee_logins(conn, people, every=6):
    """Personal login codes for a slice of the roster."""
    made = 0
    for index, emp in enumerate(people['retail']):
        if index % every:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO employee_logins (employee_id, login_code) "
            "VALUES (?, ?)", (emp['id'], 'NW%s' % emp['id'][4:]))
        made += 1
    return made


def seed_regional_managers(conn, stores, per_manager=4):
    """Regional managers, each scoped to a handful of stores."""
    scoped = []
    for index, (email, name) in enumerate(names.REGIONAL_MANAGERS):
        conn.execute("INSERT OR IGNORE INTO rm_users (email, name, active) "
                     "VALUES (?, ?, 1)", (email, name))
        mine = stores[index * per_manager:(index + 1) * per_manager]
        for store in mine:
            conn.execute("INSERT OR IGNORE INTO rm_stores (store, email) "
                         "VALUES (?, ?)", (store, email))
        scoped.append({'email': email, 'name': name, 'stores': mine})
    return scoped


def seed_transfer_history(conn, people, every=9):
    """A closed store_history row for staff who have moved once before.

    The portal and payroll sync both read history rather than only
    ``current_store``, so at least some staff must have a past store.
    """
    moved = 0
    for index, emp in enumerate(people['retail']):
        if index % every or index == 0:
            continue
        previous = people['retail'][(index + 3) % len(people['retail'])]['store']
        if previous == emp['store']:
            continue
        conn.execute(
            "INSERT INTO store_history (employee_id, store, from_date, to_date) "
            "VALUES (?,?,?,?)",
            (emp['id'], previous, iso_day(-1100), iso_day(-500)))
        moved += 1
    return moved


def month_label(offset):
    """(year, month) `offset` whole months from the anchor month."""
    return shift(offset)


# ── Admin logins ─────────────────────────────────────────────────────────────
# Roles come from northwind's own vocabulary: 'super' is full access, 'retail'
# and 'hq' are scoped consoles. More than one 'super' on purpose — several guards
# ("you cannot demote the last full-access admin", "you cannot lock yourself
# out") only have something to bite on when a second one exists.

ADMINS = [
    ('finance.lead', 'Finance Lead', 'super'),
    ('retail.ops', 'Retail Operations', 'retail'),
    ('hq.people', 'HQ People Team', 'hq'),
]

# A single throwaway hash, reused for every seeded login. Nothing signs in as
# these accounts, and hashing once keeps the seed fast; the password itself is
# the obviously-unusable string below.
SEED_PASSWORD = 'seeded-account-no-login'


def seed_admins(conn):
    """Admin logins in the unified ``users`` table, with their roles."""
    from werkzeug.security import generate_password_hash
    pw_hash = generate_password_hash(SEED_PASSWORD, method='pbkdf2:sha256')
    made = 0
    for login, display_name, role in ADMINS:
        email = '%s@%s' % (login, EMAIL_DOMAIN)
        conn.execute(
            "INSERT OR IGNORE INTO users (login, email, display_name, "
            "password_hash, is_active) VALUES (?,?,?,?,1)",
            (login, email, display_name, pw_hash))
        row = conn.execute("SELECT id FROM users WHERE login=?", (login,)).fetchone()
        conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role) VALUES (?,?)",
                     (row['id'], role))
        made += 1
    return made
