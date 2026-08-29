"""Tests for the Cash-Recon demo seed/teardown (seed_demo_cash_recon.py).

Proves the demo is (a) scoped to ONLY the demo stores, (b) fully removable, and
(c) idempotent — the three properties that make it safe to run on the live DB.
"""
from datetime import date

from werkzeug.security import check_password_hash

from northwind.data import database as db
from scripts import seed_demo_cash_recon as demo


def _current_range():
    t = date.today()
    import calendar
    last = calendar.monthrange(t.year, t.month)[1]
    return f"{t.year:04d}-{t.month:02d}-01", f"{t.year:04d}-{t.month:02d}-{last:02d}"


def test_seed_creates_read_only_scoped_demo(conn):
    demo.purge(conn)
    demo.seed(conn)
    conn.commit()
    db.invalidate_stores_cache()
    try:
        # Stores + RM identity exist.
        for name in demo.DEMO_STORES:
            assert conn.execute("SELECT 1 FROM stores WHERE name=?", (name,)).fetchone()

        rm = db.get_rm_user(demo.DEMO_RM_EMAIL)
        assert rm is not None and rm['active'] == 1

        login = db.get_cc_user(demo.DEMO_RM_EMAIL)
        assert login['is_active'] == 1
        assert check_password_hash(login['password_hash'], demo.DEMO_RM_PASSWORD)

        # RM scope is EXACTLY the demo stores — no real store leaks in.
        assert sorted(db.get_rm_stores(demo.DEMO_RM_EMAIL)) == sorted(demo.DEMO_STORES)

        # Data present and floats healthy (positive) this month.
        start, end = _current_range()
        rows = db.get_recon_overview_range(start, end, stores=demo.DEMO_STORES)
        assert len(rows) == 2
        for r in rows:
            assert r['entry_count'] > 0
            assert r['closing'] > 0
    finally:
        demo.purge(conn)
        conn.commit()


def test_purge_removes_everything_but_spares_real_stores(conn):
    real_before = conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
    demo.seed(conn)
    conn.commit()
    demo.purge(conn)
    conn.commit()

    q = ','.join('?' for _ in demo.DEMO_STORES)
    assert conn.execute(f"SELECT COUNT(*) FROM stores WHERE name IN ({q})", demo.DEMO_STORES).fetchone()[0] == 0
    assert conn.execute(f"SELECT COUNT(*) FROM cash_recon_entries WHERE store IN ({q})", demo.DEMO_STORES).fetchone()[0] == 0
    assert conn.execute(f"SELECT COUNT(*) FROM cash_recon_opening WHERE store IN ({q})", demo.DEMO_STORES).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM rm_users WHERE email=?", (demo.DEMO_RM_EMAIL,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM rm_stores WHERE email=?", (demo.DEMO_RM_EMAIL,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM users WHERE login=?", (demo.DEMO_RM_EMAIL,)).fetchone()[0] == 0
    # Real stores are untouched.
    assert conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == real_before


def test_seed_is_idempotent(conn):
    demo.purge(conn)
    demo.seed(conn)
    conn.commit()
    n1 = conn.execute("SELECT COUNT(*) FROM cash_recon_entries WHERE store LIKE 'ZZ DEMO%'").fetchone()[0]
    # Re-seeding purges first, so counts stay identical (no duplicates).
    demo.purge(conn)
    demo.seed(conn)
    conn.commit()
    n2 = conn.execute("SELECT COUNT(*) FROM cash_recon_entries WHERE store LIKE 'ZZ DEMO%'").fetchone()[0]
    stores = conn.execute("SELECT COUNT(*) FROM stores WHERE name LIKE 'ZZ DEMO%'").fetchone()[0]
    assert n1 == n2
    assert stores == 2
    demo.purge(conn)
    conn.commit()
