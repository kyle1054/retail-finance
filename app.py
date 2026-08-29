"""Northwind Deductions Manager — application entrypoint.

Routes live in routes_*.py modules; importing them registers their handlers
on the shared `app` object defined in core.py.
"""
import os
import glob
import sqlite3
import time
from datetime import datetime

from northwind.core import app, IS_PRODUCTION, BACKUP_DIR
from northwind.data import database as db

BACKUP_PREFIX = 'auto_deductions_'      # distinguishes auto backups from manual ones
BACKUP_RETENTION_DAYS = 14


def backup_database():
    """Take a consistent pre-migration snapshot of the live DB on startup.

    Uses SQLite's online backup API so the copy is consistent even with the
    WAL journal in play. Only auto-prefixed backups are auto-pruned, so any
    manually-named safety backups in backups/ are left untouched.
    """
    if not os.path.exists(db.DB_PATH):
        return  # fresh install, nothing to back up yet
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    dest_path = os.path.join(BACKUP_DIR, f'{BACKUP_PREFIX}{stamp}.db')
    try:
        src = sqlite3.connect(db.DB_PATH)
        dst = sqlite3.connect(dest_path)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        print(f"Pre-migration backup written: {os.path.relpath(dest_path)}")
    except Exception as e:
        # A backup failure must never block startup, but make it loud.
        print(f"WARNING: startup backup failed ({e}); continuing without it.")
        return
    _prune_old_backups()


def _prune_old_backups():
    """Delete auto backups older than BACKUP_RETENTION_DAYS. Manual backups
    (without the auto prefix) are never touched."""
    cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
    for path in glob.glob(os.path.join(BACKUP_DIR, f'{BACKUP_PREFIX}*.db')):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                print(f"Pruned old backup: {os.path.basename(path)}")
        except OSError:
            pass

from northwind.auth import routes as routes_auth
from northwind import demo  # noqa: F401  (sign-in page role picker)
from northwind.deductions import routes_dashboard
from northwind.deductions import routes_employees
from northwind.deductions import routes_uniforms
from northwind.deductions import routes_laybys
from northwind.deductions import routes_undercharges
from northwind.deductions import routes_monthly
from northwind.deductions import routes_stores
from northwind.deductions import routes_payroll
from northwind.deductions import routes_imports
from northwind.deductions import routes_portal
from northwind.deductions import routes_requests
from northwind.deductions import routes_hq
from northwind.deductions import routes_allowances
from northwind.deductions import routes_search
from northwind.deductions import routes_activity
from northwind.cash import routes as routes_cash_recon
from northwind.cards import routes as routes_credit_card
from northwind.regional import routes as routes_regional


def init_app():
    """Ensure the schema exists and all migrations are applied.

    Runs at import time so the app is correctly initialised under ANY WSGI
    server (gunicorn/waitress via `app:app`), not only `python app.py`.
    Every step is idempotent and safe to re-run on each boot.
    """
    db.init_db()
    db.migrate_db()
    import migrations
    applied = migrations.run_migrations()
    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    # First admin comes from ADMIN_USERNAME/ADMIN_PASSWORD env vars — there is
    # no public signup (essential on hosts where the DB is wiped
    # on every boot and the admin table starts empty).
    routes_auth.ensure_bootstrap_admin()


# Initialise on import (covers gunicorn/waitress). In production (NW_ENV=
# production — i.e. real hosted boots under gunicorn) a pre-migration backup is
# taken first; dev/test imports skip it so tests never write to backups/. The
# dev-server entrypoint below takes its own snapshot.
if IS_PRODUCTION:
    backup_database()
init_app()


if __name__ == '__main__':
    # The Werkzeug reloader re-executes this file in a child process; only the
    # parent (where WERKZEUG_RUN_MAIN is unset) should take the startup backup so
    # we don't write two snapshots per launch.
    # (Production boots already snapshot above, so skip the duplicate here.)
    if not IS_PRODUCTION and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        backup_database()

    import socket
    def get_local_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"
            
    local_ip = get_local_ip()
    print("Starting Northwind Deductions Manager...")
    print("Open your browser locally at: http://localhost:5050")
    print(f"Share with others on the same Wi-Fi at: http://{local_ip}:5050")
    # Debug (incl. the interactive code-execution debugger) is dev-only; never
    # enabled when NW_ENV=production.
    app.run(debug=not IS_PRODUCTION, host='0.0.0.0', port=5050)
