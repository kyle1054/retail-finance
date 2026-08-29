"""Create an empty database with the full schema, and one admin login.

    python3 scripts/init_db.py                      # ./db/deductions.db
    NW_DB_PATH=/tmp/scratch.db python3 scripts/init_db.py

The schema is built by importing the app, which runs the same three idempotent
steps every real boot runs (init_db -> migrate_db -> run_migrations). Doing it
that way rather than re-implementing the sequence here means a database made by
this script cannot drift from one made by starting the server.

Idempotent: safe to re-run against an existing database. It adds only what is
missing and never clears or overwrites rows.

The admin password comes from NW_ADMIN_PASSWORD, or is generated and printed
once. There is deliberately no default: an app that ships with a known login is
a worse problem than one that makes you copy a line out of your terminal.
"""
import argparse
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--username', default='admin')
    ap.add_argument('--display-name', default='Administrator')
    args = ap.parse_args()

    from werkzeug.security import generate_password_hash

    # Importing the app runs init_app(): schema, then pending migrations.
    import app  # noqa: F401
    from northwind.data import database as db

    print('database: %s' % db.DB_PATH)
    # Excluding SQLite's own bookkeeping tables, which are not part of the
    # schema and would otherwise inflate the count.
    tables = db.get_db().execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]
    print('schema ready: %d tables' % tables)

    if db.get_admin_user(args.username):
        print('admin %r already exists — leaving it alone' % args.username)
        return 0

    password = os.environ.get('NW_ADMIN_PASSWORD') or secrets.token_urlsafe(12)
    try:
        pw_hash = generate_password_hash(password)
    except AttributeError:
        # Werkzeug defaults to scrypt, which needs a Python linked against an
        # OpenSSL that provides it. The python.org and Homebrew builds do; the
        # macOS system Python does not, and raises AttributeError from hashlib.
        # pbkdf2 is still a sound choice, so fall back rather than refusing to
        # create the login on an otherwise working machine.
        pw_hash = generate_password_hash(password, method='pbkdf2:sha256')
        print('note: scrypt unavailable in this Python; used pbkdf2:sha256')
    db.create_admin_user(args.username, args.display_name,
                         pw_hash, role='super')
    print('\n  admin user: %s' % args.username)
    print('  password:   %s' % password)
    print('\n  Store it now; it is not recoverable from the database.\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
