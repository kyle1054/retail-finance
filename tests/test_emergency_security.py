"""Regression coverage for the 2026-07-22 emergency security hardening."""

import time

from werkzeug.security import generate_password_hash

from northwind.data import database as db


def _admin_client(login, role, user):
    import app as a
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False
    client = a.app.test_client()
    with client.session_transaction() as sess:
        sess['admin'] = True
        sess['admin_role'] = role
        sess['admin_username'] = login
        sess['uid'] = user['id']
        sess['auth_version'] = user['auth_version']
        sess['admin_last_active'] = time.time()
    return client


def test_role_change_revokes_existing_admin_session(db_copy):
    db.create_admin_user(
        'security-role-revoke', 'Security Role Revoke',
        generate_password_hash('plenty-long-pass', method='pbkdf2:sha256'),
        role='retail')
    user = db.get_admin_user('security-role-revoke')
    client = _admin_client('security-role-revoke', 'retail', user)
    try:
        db.set_admin_role(user['id'], 'hq')
        response = client.get('/employees', follow_redirects=False)
        assert response.status_code == 302
        with client.session_transaction() as sess:
            assert not sess.get('admin')
    finally:
        db.delete_admin_user(user['id'])


def test_shared_store_session_revoked_when_own_login_is_created(db_copy):
    import app as a
    rows = db.get_all_store_emails()
    email = next((r['email'] for r in rows if db.get_user(r['email']) is None), None)
    if not email:
        return
    store = db.get_store_by_email(email)
    client = a.app.test_client()
    with client.session_transaction() as sess:
        sess['staff_store'] = store
        sess['staff_login'] = email
        sess['staff_shared'] = True
        sess['staff_last_active'] = time.time()
    db.set_cc_user_password(
        email, generate_password_hash('store-own-pass-123', method='pbkdf2:sha256'))
    try:
        response = client.get('/portal/store', follow_redirects=False)
        assert response.status_code == 302
        with client.session_transaction() as sess:
            assert not sess.get('staff_store')
    finally:
        db.clear_store_password(email)



def test_transfer_search_uses_dom_text_not_untrusted_inner_html():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'templates' / 'employee.html').read_text()
    assert "resultsEl.innerHTML = list.map" not in source
    assert "name.textContent = e.full_name" in source
    assert "row.addEventListener('click'" in source
