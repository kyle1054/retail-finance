"""Test fixtures.

The suite runs against a database built from scratch in a temp file: the schema
is created exactly the way a real boot creates it (importing ``app`` runs
``init_db`` -> ``migrate_db`` -> ``run_migrations``), then ``tests/fixtures``
seeds synthetic data into it. Nothing is copied from anywhere, so a fresh clone
can run the whole suite, and no test can read or write real data.

``database.DB_PATH`` is redirected at the temp file *before* the app is
imported, because importing the app initialises the schema — on whatever path
DB_PATH points at at that moment.
"""
import os
import sys
import time
import tempfile
import pytest

# Set before any app import: northwind/auth/routes.py reads this at
# module scope, and with it unset the shared-password path is closed
# (which is the right default, but three tests are about that path).
os.environ.setdefault('NW_STAFF_PASSWORD', 'shared-staff-password-for-tests')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture(scope='session')
def db_copy():
    """A throwaway synthetic database; database.DB_PATH points at it.

    Session-scoped: building the schema and seeding it costs a couple of
    seconds, and the data is shaped so tests can share it (each one works in a
    far-future period, its own store, or its own newly-created row).

    The name is kept from when this used a database-shaped fixture, because
    every test in the suite asks for it by that name.
    """
    from northwind.data import database as db
    fd, path = tempfile.mkstemp(suffix='.db', prefix='nw_test_')
    os.close(fd)
    original = db.DB_PATH
    db.DB_PATH = path
    db.invalidate_stores_cache()

    # Importing the app builds the schema on DB_PATH (init_db, migrate_db, then
    # every pending migration) — the same three steps a real boot runs.
    import app  # noqa: F401
    import fixtures

    conn = db.get_db()
    try:
        fixtures.seed(conn)
    finally:
        conn.close()
    db.invalidate_stores_cache()

    yield path

    db.DB_PATH = original
    db.invalidate_stores_cache()
    for suffix in ('', '-wal', '-shm'):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


@pytest.fixture
def conn(db_copy):
    from northwind.data import database as db
    c = db.get_db()
    yield c
    c.close()


@pytest.fixture
def client(db_copy):
    """Test client authenticated as an admin.

    Admin auth stays fully enabled (we don't disable it); the fixture simply
    establishes a valid admin session the way a real login would, so admin-only
    pages actually render and mutation routes actually run instead of being
    redirected to /admin/login.
    """
    import app as a
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False  # tokens are exercised separately
    c = a.app.test_client()
    from northwind.data import database as db
    from werkzeug.security import generate_password_hash
    identity = db.get_admin_user('pytest')
    if identity is None:
        db.create_admin_user(
            'pytest', 'Pytest Admin',
            generate_password_hash('pytest-session-password', method='pbkdf2:sha256'),
            role='super')
        identity = db.get_admin_user('pytest')
    with c.session_transaction() as sess:
        sess['admin'] = True
        sess['admin_role'] = 'super'   # a real login always sets a role (fail-closed gate)
        sess['admin_last_active'] = time.time()   # mirror a fresh login (idle-timeout)
        sess['admin_username'] = 'pytest'
        sess['admin_display_name'] = 'Pytest Admin'
        sess['uid'] = identity['id']
        sess['auth_version'] = identity['auth_version']
    return c


@pytest.fixture
def staff_client(db_copy):
    """Test client logged in to the staff portal as a store (store-level session).

    Yields (client, employee_row) so tests can hit both the store list and the
    detail page of a staff member known to belong to that store.
    """
    import time
    import app as a
    from northwind.data import database as db
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False
    c = a.app.test_client()
    conn = db.get_db()
    login = conn.execute(
        "SELECT s.email, s.store FROM store_emails s "
        "LEFT JOIN users u ON u.login=s.email "
        "WHERE u.id IS NULL AND EXISTS (SELECT 1 FROM employees e "
        "WHERE e.current_store=s.store AND e.status='active') LIMIT 1").fetchone()
    emp = conn.execute(
        "SELECT id, full_name, current_store FROM employees "
        "WHERE status='active' AND current_store=? LIMIT 1", (login['store'],)).fetchone()
    conn.close()
    with c.session_transaction() as sess:
        sess['staff_store'] = emp['current_store']
        sess['staff_login'] = login['email']
        sess['staff_shared'] = True
        sess['staff_last_active'] = time.time()
    return c, emp
