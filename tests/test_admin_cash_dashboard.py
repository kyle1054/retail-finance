"""Admin cash dashboard — the RM-style overall view + region picker.

Verifies: super admins reach it (all stores + a region picker), scoping to one
RM's region works, and non-super admins are blocked (it's super-only). Reuses the
shared build_cash_dashboard behind the RM dashboard, so this guards the parametrised
template stays working for the admin path.
"""
import time

from northwind.data import database as db

RM_EMAIL = 'zz-admin-dash-rm@test.co'
RM_STORE = 'ZZ Admin Dash Store'


def _retail_client(adm):
    import app as a
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False
    c = a.app.test_client()
    with c.session_transaction() as sess:
        sess['admin'] = True
        sess['admin_role'] = 'retail'
        sess['admin_last_active'] = time.time()
        sess['admin_username'] = adm['username']
        sess['uid'] = adm['id']
        sess['auth_version'] = adm['auth_version']
    return c


def test_super_sees_overall_dashboard_with_region_picker(client, conn):
    conn.execute("INSERT OR IGNORE INTO stores (name) VALUES (?)", (RM_STORE,))
    conn.commit()
    db.invalidate_stores_cache()
    db.upsert_rm_user(RM_EMAIL, 'Dash Test RM', active=1)
    db.assign_store_rm(RM_STORE, RM_EMAIL)
    try:
        r = client.get('/cash/dashboard')
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert 'All stores (overall)' in body           # the region picker rendered
        assert 'Dash Test RM' in body                    # the RM appears as a region option
        assert 'Admin cash overview' in body             # admin kicker, not the RM one

        # Scoping to that RM's region keeps the region on every self-link.
        r2 = client.get(f'/cash/dashboard?region={RM_EMAIL}&view=store')
        assert r2.status_code == 200
        body2 = r2.get_data(as_text=True)
        assert RM_STORE in body2
        assert f'region={RM_EMAIL}'.replace('@', '%40') in body2 or f'region={RM_EMAIL}' in body2
    finally:
        db.assign_store_rm(RM_STORE, None)
        db.delete_rm_user(RM_EMAIL)
        conn.execute("DELETE FROM stores WHERE name=?", (RM_STORE,))
        conn.commit()
        db.invalidate_stores_cache()


def test_admin_day_fragment_renders(client, conn):
    store = conn.execute("SELECT name FROM stores LIMIT 1").fetchone()['name']
    r = client.get(f'/cash/dashboard/{store}/days?start=2026-07-01&end=2026-07-31')
    assert r.status_code == 200


def test_non_super_admin_is_blocked(db_copy):
    from werkzeug.security import generate_password_hash
    db.create_admin_user('cash_retail_test', 'Cash Retail Test',
                         generate_password_hash('plenty-long-pass', method='pbkdf2:sha256'),
                         role='retail')
    adm = db.get_admin_user('cash_retail_test')
    try:
        c = _retail_client(adm)
        r = c.get('/cash/dashboard', follow_redirects=False)
        assert r.status_code in (302, 303)      # super-only — retail admin bounced
    finally:
        db.delete_admin_user(adm['id'])
