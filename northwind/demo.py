"""Demo mode: a one-click role picker on the sign-in page.

A portfolio demo has a chicken-and-egg problem — the interesting thing about
this app is that four different kinds of person see four different things, and
nobody is going to discover that by being shown one login box. So when demo
mode is on, the sign-in page also lists the seeded roles and lets a visitor
step straight into any of them.

**This adds no authentication path.** Each button submits the ordinary sign-in
form with a known identifier and the shared demo password, so the request runs
through exactly the same lockout, hashing and role resolution as anyone else's.
Turning demo mode off removes the buttons and changes nothing else; there is no
branch anywhere in the login code that trusts this module.

Off unless BOTH of these are set, so it cannot be switched on by accident:

    NW_DEMO_MODE=1
    NW_DEMO_PASSWORD=<the password scripts/seed_demo.py printed>

Never set them on a deployment holding real people's data. The whole point of
the feature is that it publishes a working credential on the front page.
"""
import os

from northwind.core import app
from northwind.data import database as db

# (kind, label, blurb) — resolved against the database at render time, so a
# role with no seeded account simply does not appear rather than 404ing.
_SPEC = [
    ('admin:super',  'Full access',
     'Everything: every store, payroll, cards, cash, and the admin console.'),
    ('admin:retail', 'Retail workspace',
     'Store-side deductions and payroll. No HQ employees.'),
    ('admin:hq',     'HQ workspace',
     'Head-office staff and their annual goods allowances.'),
    ('store',        'Store manager',
     'The staff portal for one store: what comes off their team next payday.'),
    ('rm',           'Regional manager',
     'Read-only cash reconciliation across an assigned group of stores.'),
    ('cardholder',   'Cardholder',
     'One company card: their own charges and the receipts still owed.'),
]


def password():
    return os.environ.get('NW_DEMO_PASSWORD', '')


def enabled():
    """Demo mode needs an explicit opt-in AND the password to hand out."""
    return os.environ.get('NW_DEMO_MODE') == '1' and bool(password())


def _first(sql, params=()):
    conn = db.get_db()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _resolve(kind):
    """The identifier to sign in with for this role, or None if unseeded.

    Every lookup requires a row in `users` — an account with no credential
    cannot be offered, because the button would just fail the password check.
    """
    if kind.startswith('admin:'):
        row = _first(
            'SELECT u.login AS id, u.display_name AS name FROM users u '
            'JOIN user_roles r ON r.user_id = u.id '
            'WHERE r.role = ? AND u.is_active = 1 ORDER BY u.id LIMIT 1',
            (kind.split(':', 1)[1],))
    elif kind == 'store':
        row = _first(
            'SELECT u.login AS id, s.store AS name FROM users u '
            'JOIN store_emails s ON s.email = u.login '
            'WHERE u.is_active = 1 ORDER BY s.store LIMIT 1')
    elif kind == 'rm':
        row = _first(
            'SELECT u.login AS id, m.name AS name FROM users u '
            'JOIN rm_users m ON m.email = u.login '
            'WHERE m.active = 1 AND u.is_active = 1 ORDER BY m.name LIMIT 1')
    elif kind == 'cardholder':
        row = _first(
            'SELECT u.login AS id, c.name AS name FROM users u '
            'JOIN cc_card_users c ON c.email = u.login '
            'WHERE u.is_active = 1 ORDER BY c.id LIMIT 1')
    else:
        return None
    return dict(row) if row else None


def personas():
    """What the picker should offer, given what is actually in this database."""
    if not enabled():
        return []
    out = []
    for kind, label, blurb in _SPEC:
        try:
            found = _resolve(kind)
        except Exception:
            # A database without the demo schema (or mid-migration) must not
            # take the sign-in page down with it.
            found = None
        if found:
            out.append({'label': label, 'blurb': blurb,
                        'identifier': found['id'], 'who': found['name'] or found['id']})
    return out


@app.context_processor
def _inject():
    """`demo_mode` / `demo_personas` / `demo_password` for the sign-in template."""
    if not enabled():
        return {'demo_mode': False, 'demo_personas': [], 'demo_password': ''}
    return {'demo_mode': True, 'demo_personas': personas(), 'demo_password': password()}


if enabled():
    app.logger.warning(
        'DEMO MODE IS ON: the sign-in page publishes working credentials. '
        'Never enable this on a deployment holding real data.')
