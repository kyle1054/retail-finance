"""Smoke test: every GET route renders without a server error."""
from datetime import datetime


def test_all_get_routes_render(client):
    now = datetime.now()
    urls = [
        '/', '/admin', '/admin/login',
        '/employees', '/uniforms', '/laybys', '/undercharges',
        '/invoice-search', '/invoice-search?q=SO',
        '/monthly', f'/monthly/{now.year}/{now.month}',
        '/stores', '/payroll/sync', '/payroll/sheet', '/payroll/reconcile',
        '/import-center',
        # HQ workspace (lay-by only) — sector-separated from Retail.
        '/hq/employees', '/hq/laybys',
        '/hq/monthly', f'/hq/monthly/{now.year}/{now.month}',
        '/api/payroll/reconcile/stores-summary', '/api/employees/search?q=a',
    ]
    bad = []
    for u in urls:
        r = client.get(u)
        if r.status_code >= 500:
            bad.append((u, r.status_code))
    assert not bad, f"routes returning 5xx: {bad}"


def test_portal_store_renders(staff_client):
    """The staff portal renders for a logged-in store: the staff list, a
    profile of one of its own employees, and /portal/me redirecting to the
    store page (legacy bookmark from the per-employee portal)."""
    c, emp = staff_client
    r = c.get("/portal/store")
    assert r.status_code == 200
    assert emp['full_name'].encode() in r.data
    # Privacy: the shared store page lists names only — no balances. Amounts
    # appear only on a person's own profile page.
    assert b'still owing' not in r.data
    assert b'Store total owing' not in r.data

    r = c.get(f"/portal/store/{emp['id']}")
    assert r.status_code == 200

    r = c.get("/portal/me")
    assert r.status_code == 302
    assert '/portal/store' in r.headers['Location']


def test_portal_store_blocks_other_stores(staff_client):
    """A store session must not open a profile of staff from another store."""
    from northwind.data import database as db
    c, emp = staff_client
    conn = db.get_db()
    other = conn.execute(
        "SELECT id FROM employees WHERE status='active' "
        "AND current_store IS NOT NULL AND current_store != ? LIMIT 1",
        (emp['current_store'],)).fetchone()
    conn.close()
    r = c.get(f"/portal/store/{other['id']}")
    assert r.status_code == 302
    assert '/portal/store' in r.headers['Location']


def test_staff_session_cannot_reach_admin(staff_client):
    """A store session is portal-only — admin pages must redirect away."""
    c, _ = staff_client
    r = c.get("/employees")
    assert r.status_code == 302
