"""Phase 2 auth-hardening tests: CSRF, login throttling, password floor."""


def test_lockout_after_max_failures():
    from northwind.services import security
    key = security.make_key('admin', 'bob', '203.0.113.7')
    security.reset(key)
    # Below the threshold: never locked.
    for _ in range(security.MAX_ATTEMPTS):
        assert security.seconds_locked(key) == 0
        security.record_failure(key)
    # At/over the threshold: locked with a positive countdown.
    assert security.seconds_locked(key) > 0
    # A successful login clears it.
    security.reset(key)
    assert security.seconds_locked(key) == 0


def test_csrf_blocks_tokenless_post(db_copy):
    import app as a
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = True
    c = a.app.test_client()
    with c.session_transaction() as sess:
        sess['admin'] = True
    # JSON Accept makes the CSRF error handler return a clean 400.
    r = c.post('/uniform/add', data={'employee_id': 'EMP-0001'},
               headers={'Accept': 'application/json'})
    assert r.status_code == 400
    a.app.config['WTF_CSRF_ENABLED'] = False  # restore for other tests


def test_password_floor_rejects_short(client):
    from northwind.data import database as db
    # CSRF is disabled on the `client` fixture, so this reaches the route logic.
    client.post('/admin/admins/add', data={
        'username': 'shorty', 'display_name': 'Shorty', 'password': 'short123'})  # 8 chars
    assert db.get_admin_user('shorty') is None, "short password should be rejected"


def test_password_floor_accepts_long(client):
    from northwind.data import database as db
    client.post('/admin/admins/add', data={
        'username': 'longuser', 'display_name': 'Long User', 'password': 'plenty-long-pass'})
    assert db.get_admin_user('longuser') is not None


def test_cannot_delete_last_super_admin(client):
    """The final full-access (super) admin must never be deletable — otherwise
    nobody could manage admins, backups or the database."""
    from northwind.data import database as db
    from werkzeug.security import generate_password_hash
    # Isolate: demote every existing super so our test super is the only one,
    # remembering prior roles to restore them afterwards.
    existing = [dict(a) for a in db.get_all_admin_users()]
    for a in existing:
        if (a['role'] or 'super') == 'super':
            db.set_admin_role(a['id'], 'retail')
    db.create_admin_user('solo_super', 'Solo Super',
                         generate_password_hash('plenty-long-pass', method='pbkdf2:sha256'),
                         role='super')
    solo = db.get_admin_user('solo_super')
    try:
        client.post(f"/admin/admins/delete/{solo['id']}")
        assert db.get_admin_user('solo_super') is not None, \
            "the last super admin must not be deletable"
    finally:
        db.delete_admin_user(solo['id'])
        for a in existing:
            db.set_admin_role(a['id'], a['role'] or 'super')


def test_landing_login_roundtrip_and_bad_password(db_copy):
    """The unified '/' login accepts a correct admin credential and rejects a
    wrong one (exercises the restructured, timing-equalised login path)."""
    import app as a
    from northwind.data import database as db
    from werkzeug.security import generate_password_hash
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False
    db.create_admin_user('logintest', 'Login Test',
                         generate_password_hash('plenty-long-pass', method='pbkdf2:sha256'),
                         role='super')
    try:
        c = a.app.test_client()
        # Wrong password → re-renders the login page, no session established.
        r = c.post('/', data={'identifier': 'logintest', 'password': 'nope'})
        assert r.status_code == 200
        with c.session_transaction() as s:
            assert not s.get('admin')
        # Correct password → redirect into the app with an admin session.
        r = c.post('/', data={'identifier': 'logintest', 'password': 'plenty-long-pass'})
        assert r.status_code == 302
        with c.session_transaction() as s:
            assert s.get('admin') is True
            assert s.get('admin_role') == 'super'
    finally:
        db.delete_admin_user(db.get_admin_user('logintest')['id'])


def test_xl_safe_neutralises_formulas():
    """Strings starting with '=' must never reach an export cell unprefixed —
    openpyxl would store them as live formulas (formula-injection)."""
    from northwind.data import database as db
    assert db.xl_safe('=HYPERLINK("http://evil")') == "'=HYPERLINK(\"http://evil\")"
    assert db.xl_safe('Golf Shirt - Navy') == 'Golf Shirt - Navy'   # untouched
    assert db.xl_safe(42.5) == 42.5                                  # non-strings untouched
    assert db.xl_safe(None) is None


def test_security_headers_present(client):
    r = client.get('/admin/login')
    assert r.headers.get('X-Frame-Options') == 'DENY'
    assert r.headers.get('X-Content-Type-Options') == 'nosniff'
    assert 'Content-Security-Policy' in r.headers
    # Cross-origin isolation + feature lockdown (added 2026-07-17).
    assert r.headers.get('Cross-Origin-Opener-Policy') == 'same-origin'
    assert r.headers.get('Cross-Origin-Resource-Policy') == 'same-origin'
    pp = r.headers.get('Permissions-Policy', '')
    assert 'geolocation=()' in pp and 'microphone=()' in pp
    # Camera must NOT be disabled — the CC portal captures receipts via the phone
    # camera; disabling it here would break that flow.
    assert 'camera=()' not in pp
