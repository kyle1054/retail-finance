"""Phase 3: per-store staff passwords in the unified users store, with the shared
staff password retained as a transition fallback."""
from werkzeug.security import generate_password_hash

from northwind.data import database as db
from northwind.services import security

OWN = 'store-own-pass-123'
PW = generate_password_hash(OWN, method='pbkdf2:sha256')


def _a_store_email():
    rows = db.get_all_store_emails()
    return rows[0]['email'] if rows else None


def _client():
    import app as a
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False
    return a.app.test_client()


def _reset_throttle(email):
    security.reset(security.make_key('login', email, '127.0.0.1'))


def test_capability_reports_staff_store(db_copy):
    email = _a_store_email()
    if not email:
        return
    db.set_cc_user_password(email, PW)
    try:
        caps = db.user_capabilities(email)
        assert caps['staff_store'] == db.get_store_by_email(email)
        assert email in db.store_password_logins()
    finally:
        db.clear_store_password(email)
    assert email not in db.store_password_logins()


def test_own_password_disables_shared_fallback(db_copy):
    from northwind.auth import routes as routes_auth
    email = _a_store_email()
    if not email:
        return
    store = db.get_store_by_email(email)
    db.set_cc_user_password(email, PW)
    try:
        _reset_throttle(email)
        c = _client()
        r = c.post('/', data={'identifier': email, 'password': OWN})
        assert r.status_code == 302
        with c.session_transaction() as s:
            assert s.get('staff_store') == store

        # Once an own password exists, the shared password is no longer a
        # master-password fallback for this store.
        c2 = _client()
        r2 = c2.post('/', data={'identifier': email, 'password': routes_auth.STAFF_PORTAL_PASSWORD})
        assert r2.status_code == 200
        with c2.session_transaction() as s:
            assert not s.get('staff_store')

        # A wrong password (neither own nor shared) is refused.
        c3 = _client()
        r3 = c3.post('/', data={'identifier': email, 'password': 'totally-wrong'})
        assert r3.status_code == 200
        with c3.session_transaction() as s:
            assert not s.get('staff_store')
    finally:
        db.clear_store_password(email)


def test_disabled_store_user_cannot_use_shared_password(db_copy):
    from northwind.auth import routes as routes_auth
    email = _a_store_email()
    if not email:
        return
    db.set_cc_user_password(email, PW)
    user = db.get_user(email)
    db.set_user_active(user['id'], False)
    try:
        _reset_throttle(email)
        c = _client()
        r = c.post('/', data={
            'identifier': email,
            'password': routes_auth.STAFF_PORTAL_PASSWORD,
        })
        assert r.status_code == 200
        with c.session_transaction() as s:
            assert not s.get('staff_store')
    finally:
        db.clear_store_password(email)


def test_clear_reverts_to_shared_password(db_copy):
    from northwind.auth import routes as routes_auth
    email = _a_store_email()
    if not email:
        return
    store = db.get_store_by_email(email)
    db.set_cc_user_password(email, PW)
    db.clear_store_password(email)
    _reset_throttle(email)
    # Old own password no longer works...
    c = _client()
    r = c.post('/', data={'identifier': email, 'password': OWN})
    assert r.status_code == 200
    with c.session_transaction() as s:
        assert not s.get('staff_store')
    # ...but the shared password does.
    c2 = _client()
    r2 = c2.post('/', data={'identifier': email, 'password': routes_auth.STAFF_PORTAL_PASSWORD})
    assert r2.status_code == 302
    with c2.session_transaction() as s:
        assert s.get('staff_store') == store


def test_set_and_clear_password_routes(client):
    rows = db.get_all_store_emails()
    if not rows:
        return
    email = rows[0]['email']
    try:
        client.post('/admin/staff-logins/set-password', data={'email': email})
        assert email in db.store_password_logins()
        client.post('/admin/staff-logins/clear-password', data={'email': email})
        assert email not in db.store_password_logins()
    finally:
        db.clear_store_password(email)
