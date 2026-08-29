"""Fill a database with a full, coherent demo dataset and usable logins.

    python3 scripts/init_db.py                 # schema first
    python3 scripts/seed_demo.py               # then the data
    python3 app.py

Roughly 310 employees across 27 stores, 434 uniform plans and 302 lay-bys at
every stage of repayment, 278 undercharges, fourteen months of already-processed
payroll, six months of store cash reconciliation, and four company cards with
statements, charges and receipts. Enough that every page has something real on it
and the reports mean something.

The data comes from the same generator the test suite uses
(``tests/fixtures``), at its larger ``demo`` scale. Sharing it is deliberate:
one generator that both the tests and the demo exercise cannot drift into
producing data the app cannot actually handle. That is also why this script
lives here and reaches into ``tests/`` rather than keeping a second copy.

Everything is deterministic and dated relative to the current month, so a demo
seeded today looks the same as one seeded next year — the payroll history is
always "the last fourteen months".

Logins: the generator creates accounts with an unusable password, because a
test must never be able to log in as one. This script gives them a real
password so a person can. It is read from NW_DEMO_PASSWORD, or generated and
printed once.

Not idempotent — it inserts. Run it on a database you are willing to throw
away, and use --reset to start over.
"""
import argparse
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(1, os.path.join(ROOT, 'tests'))   # the shared data generator


def _hash(password):
    from werkzeug.security import generate_password_hash
    try:
        return generate_password_hash(password)
    except AttributeError:
        # Werkzeug defaults to scrypt, which the macOS system Python's hashlib
        # does not provide. pbkdf2 is still sound; see scripts/init_db.py.
        return generate_password_hash(password, method='pbkdf2:sha256')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--scale', default='demo', choices=('demo', 'test'),
                    help="'demo' is the larger, presentable dataset (default)")
    ap.add_argument('--store-logins', type=int, default=3, metavar='N',
                    help='give this many stores their own portal password')
    ap.add_argument('--reset', action='store_true',
                    help='delete the database file first and rebuild the schema')
    args = ap.parse_args()

    from northwind.data import database as db

    if args.reset:
        for suffix in ('', '-wal', '-shm'):
            path = db.DB_PATH + suffix
            if os.path.exists(path):
                os.remove(path)
        print('removed %s' % db.DB_PATH)

    # Importing the app builds the schema (init_db -> migrate_db -> migrations).
    import app  # noqa: F401
    import fixtures

    print('database: %s' % db.DB_PATH)
    conn = db.get_db()
    counts = fixtures.seed(conn, scale=args.scale)
    conn.commit()
    print('seeded (%s): %s' % (args.scale, fixtures.summary_of(counts)))

    password = os.environ.get('NW_DEMO_PASSWORD') or secrets.token_urlsafe(9)
    pw_hash = _hash(password)

    # Admin logins: every seeded account that carries a role.
    admins = conn.execute(
        'SELECT u.id, u.login, u.display_name, r.role FROM users u '
        'JOIN user_roles r ON r.user_id = u.id ORDER BY u.id').fetchall()
    for row in admins:
        db.set_user_password(row['id'], pw_hash)

    # Every other portal identity. The generator creates the accounts but no
    # credentials — a test must never be able to log in as one. `users` is the
    # single credential table behind all of them (login = the email), and this
    # is the same call the admin console makes to issue a password.
    stores = [(r['store'], r['email'])
              for r in db.get_all_store_emails()[:max(0, args.store_logins)]]
    rms = [(r['name'], r['email']) for r in conn.execute(
        'SELECT name, email FROM rm_users WHERE active = 1 ORDER BY name')]
    holders = [(r['name'], r['email']) for r in conn.execute(
        'SELECT c.name, c.email FROM cc_card_users c ORDER BY c.card_id')]

    for _, email in stores + rms + holders:
        db.set_cc_user_password(email, pw_hash)

    conn.commit()

    print('\n  Sign in at /  — one form resolves all four kinds of identity.\n')
    groups = [
        ('Admin', [(r['login'], '%s (%s)' % (r['display_name'], r['role']))
                   for r in admins]),
        ('Store manager', [(e, s) for s, e in stores]),
        ('Regional manager', [(e, n) for n, e in rms]),
        ('Cardholder', [(e, n) for n, e in holders]),
    ]
    for title, rows in groups:
        if not rows:
            continue
        print('  %s' % title)
        for identifier, who in rows:
            print('    %-44s %s' % (identifier, who))
        print()
    print('  Password for all of the above: %s\n' % password)
    print('  Stores without their own credential fall back to NW_STAFF_PASSWORD.')
    print('  For the one-click role picker on the sign-in page, run the app with:')
    print('    NW_DEMO_MODE=1 NW_DEMO_PASSWORD=%s python3 app.py\n' % password)
    return 0


if __name__ == '__main__':
    sys.exit(main())
