"""Seed (or remove) a self-contained Cash-Recon DEMO.

Creates two demo stores, one demo Regional Manager login, and a realistic
month-and-a-bit of cash-recon entries for those stores — so the RM cash
dashboard and the admin drill-down can be shown to someone without touching a
single row of real data.

Everything it creates is prefixed/keyed so it is trivially and completely
removable:
  • stores        — named with the DEMO_PREFIX ("ZZ DEMO - …")
  • RM login       — the DEMO_RM_EMAIL user (+ rm_users / rm_stores rows)
  • ledger data    — cash_recon_entries / cash_recon_opening for the demo stores

It is IDEMPOTENT: a seed run first purges any prior demo rows, then re-inserts a
known-good state, so re-running never duplicates. `--remove` just runs the purge.

The demo RM is READ-ONLY (RM portal registers no write routes) and is scoped to
ONLY the demo stores, so even on the live DB it physically cannot see or touch
real stores.

Run LOCALLY against a scratch DB:
    NW_DB_PATH=/tmp/demo.db python seed_demo_cash_recon.py

Run it against a scratch database, never one holding real data.

Safe to delete this file once the demo is retired.
"""
import os
import sys

# Make the repo root importable so `import database` / `import money` resolve
# when this script is run directly (e.g. `python scripts/seed_demo_cash_recon.py`).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import secrets

import argparse
from datetime import date

from werkzeug.security import generate_password_hash

from northwind.data import database as db
from northwind.services import money

# ── Demo identity (everything here is keyed off these constants) ──────────────
DEMO_PREFIX = 'ZZ DEMO - '
DEMO_STORES = [DEMO_PREFIX + 'Alpha', DEMO_PREFIX + 'Bravo']
DEMO_RM_EMAIL = 'demo.rm@northwind-apparel.example'
DEMO_RM_NAME = 'Demo Regional Manager'
DEMO_RM_PASSWORD = os.environ.get('NW_DEMO_PASSWORD') or secrets.token_urlsafe(9)

# Per-store Xero tracking so the expenses MJ preview reads cleanly. The POS
# (store_code) is deliberately left unset: the consolidated cash-sales journal
# includes every store that HAS one, so a demo code put fake stores and fake
# money into a real finance export. A demo must never reach a Xero import.
DEMO_STORE_XERO = {
    DEMO_STORES[0]: {'tracking_name': 'ZZ Demo Alpha', 'store_code': None},
    DEMO_STORES[1]: {'tracking_name': 'ZZ Demo Bravo', 'store_code': None},
}

# Two months of data ending on the current month, so opening-float carry-forward
# is visible (prior month's close becomes this month's opening).
_TODAY = date.today()
_CUR = (_TODAY.year, _TODAY.month)
_PREV = (_TODAY.year - 1, 12) if _TODAY.month == 1 else (_TODAY.year, _TODAY.month - 1)

OPENING_FLOAT_RANDS = 2000.0  # explicit opening for the first (prior) demo month


def _month_entries(store_idx, day_shift):
    """A believable set of ledger entries for one store-month.

    Returns [(day_of_month, category_name, amount_rands, note, direction_or_None)].
    `store_idx`/`day_shift` nudge the numbers so the two stores and two months
    don't look identical on the charts. Amounts are chosen to exercise mixed VAT
    and to leave a rounding cent on the expenses MJ (e.g. R649.99 standard)."""
    bump = store_idx * 1.0
    # A per-store receipt-number base so each cash-sale line carries a distinct
    # sale number in its note (the "specify receipt number" the drill-down shows).
    rcpt = 100000 + store_idx * 500
    return [
        # Income — cash sales across the month (hidden on the RM view by default).
        (2 + day_shift, 'Cash Sale (specify receipt number)', 4820.00 + bump * 310, f'Z-report #{rcpt + 31}', None),
        (9 + day_shift, 'Cash Sale (specify receipt number)', 5110.50 + bump * 290, f'Z-report #{rcpt + 48}', None),
        (16 + day_shift, 'Cash Sale (specify receipt number)', 4675.25 + bump * 275, f'Z-report #{rcpt + 63}', None),
        # Expenses — mixed VAT; R649.99 standard leaves a rounding cent for the MJ.
        (3 + day_shift, 'Milk', 45.00 + bump, 'MARKET CO', None),
        (5 + day_shift, 'Coffee', 89.90 + bump, 'Greenfields', None),
        (7 + day_shift, 'Electricity', 649.99 + bump * 4, 'Prepaid meter top-up', None),
        (11 + day_shift, 'Airtime', 120.00, 'Store phone', None),
        (14 + day_shift, 'Printing & Stationery (specify in notes)', 230.00 + bump * 3, 'Till rolls + labels', None),
        (18 + day_shift, 'Fuel (specify in notes)', 500.00 + bump * 10, 'Deposit run', None),
        # Banked — cash deposited to the bank (reduces the float). Kept below net
        # cash-in so the closing float stays healthy and grows gently month on month.
        (10 + day_shift, 'Banked', 7000.00 + bump * 200, 'Cash deposit at THE BANK', None),
        (20 + day_shift, 'Banked', 5000.00 + bump * 150, 'Cash deposit at THE BANK', None),
        # Adjustment — a small till shortfall.
        (15 + day_shift, 'Cash Balancing', 12.50 + bump, 'Till short on count', None),
    ]


def _category_map(conn):
    """name -> (id, kind) for every recon category, so we resolve by NAME (robust
    to id/order drift on the live DB) rather than hard-coding ids."""
    return {r['name']: (r['id'], r['kind'])
            for r in conn.execute("SELECT id, name, kind FROM recon_categories")}


def purge(conn):
    """Remove every trace of the demo. FK-safe order. Never deletes a users row
    that carries an admin role (defence in depth — the demo login shouldn't, but
    we refuse to touch a privileged account under any circumstances)."""
    marks = list(DEMO_STORES)
    q = ','.join('?' for _ in marks)
    n_entries = conn.execute(
        f"DELETE FROM cash_recon_entries WHERE store IN ({q})", marks).rowcount
    conn.execute(f"DELETE FROM cash_recon_opening WHERE store IN ({q})", marks)
    conn.execute(f"DELETE FROM rm_stores WHERE store IN ({q})", marks)
    conn.execute("DELETE FROM rm_stores WHERE email = ?", (DEMO_RM_EMAIL,))
    conn.execute("DELETE FROM rm_users WHERE email = ?", (DEMO_RM_EMAIL,))
    urow = conn.execute("SELECT id FROM users WHERE login = ?", (DEMO_RM_EMAIL,)).fetchone()
    if urow is not None:
        has_role = conn.execute(
            "SELECT 1 FROM user_roles WHERE user_id = ?", (urow['id'],)).fetchone()
        if has_role:
            print(f"  ! refusing to delete users row {DEMO_RM_EMAIL} — it holds an admin role")
        else:
            conn.execute("DELETE FROM users WHERE id = ?", (urow['id'],))
    conn.execute(f"DELETE FROM stores WHERE name IN ({q})", marks)
    return n_entries


def seed(conn):
    cats = _category_map(conn)
    missing = []

    def cat_id(name):
        hit = cats.get(name)
        if hit is None:
            missing.append(name)
            return None
        return hit[0]

    # Fail loudly if the live category set has drifted from what we reference.
    for name in {'Cash Sale (specify receipt number)', 'Banked', 'Cash Balancing',
                 'Milk', 'Coffee', 'Electricity', 'Airtime',
                 'Printing & Stationery (specify in notes)', 'Fuel (specify in notes)'}:
        cat_id(name)
    if missing:
        raise SystemExit(f"Aborting — recon categories not found: {sorted(set(missing))}")

    # 1) Stores + their Xero mapping.
    for name in DEMO_STORES:
        conn.execute("INSERT INTO stores (name) VALUES (?)", (name,))
        x = DEMO_STORE_XERO[name]
        conn.execute("UPDATE stores SET xero_tracking_name = ?, store_code = ? WHERE name = ?",
                     (x['tracking_name'], x['store_code'], name))

    # 2) RM login (unified users table) + rm_users + store assignments.
    conn.execute(
        "INSERT INTO users (login, email, display_name, password_hash, is_active) "
        "VALUES (?,?,?,?,1)",
        (DEMO_RM_EMAIL, DEMO_RM_EMAIL, DEMO_RM_NAME,
         generate_password_hash(DEMO_RM_PASSWORD, method='pbkdf2:sha256')))
    conn.execute(
        "INSERT INTO rm_users (email, name, active) VALUES (?,?,1)",
        (DEMO_RM_EMAIL, DEMO_RM_NAME))
    for name in DEMO_STORES:
        conn.execute("INSERT INTO rm_stores (store, email) VALUES (?,?)", (name, DEMO_RM_EMAIL))

    # 3) Explicit opening float for the first (prior) demo month, then entries
    #    across both months. Current month's opening carries from prior close.
    for name in DEMO_STORES:
        conn.execute(
            "INSERT INTO cash_recon_opening (store, year, month, opening_cents) VALUES (?,?,?,?)",
            (name, _PREV[0], _PREV[1], money.to_cents(OPENING_FLOAT_RANDS)))

    total_entries = 0
    for store_idx, name in enumerate(DEMO_STORES):
        for (yr, mo), shift in ((_PREV, 0), (_CUR, 0)):
            last_day = _last_day(yr, mo)
            for day, cat_name, amount, note, direction in _month_entries(store_idx, shift):
                day = min(day, last_day)
                cid, kind = cats[cat_name]
                dirn = direction or ('in' if kind == 'income' else 'out')
                conn.execute(
                    "INSERT INTO cash_recon_entries "
                    "(store, entry_date, category_id, description, direction, amount_cents, note, created_by) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (name, f"{yr:04d}-{mo:02d}-{day:02d}", cid, cat_name, dirn,
                     money.to_cents(abs(amount)), note, name))
                total_entries += 1
    return total_entries


def _last_day(year, month):
    import calendar
    return calendar.monthrange(year, month)[1]


def main():
    ap = argparse.ArgumentParser(description='Seed or remove the Cash-Recon demo.')
    ap.add_argument('--remove', action='store_true', help='Remove the demo and exit.')
    args = ap.parse_args()

    conn = db.get_db()
    try:
        removed = purge(conn)
        if args.remove:
            conn.commit()
            print(f"Removed demo: {removed} ledger entries + demo stores/RM cleared.")
            return
        entries = seed(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        print("Rolled back — nothing was written.", file=sys.stderr)
        raise
    finally:
        conn.close()

    db.invalidate_stores_cache()
    print("Demo seeded ✓")
    print(f"  Stores   : {', '.join(DEMO_STORES)}")
    print(f"  RM login : {DEMO_RM_EMAIL}")
    print(f"  Password : {DEMO_RM_PASSWORD}")
    print(f"  Entries  : {entries} across {_PREV[1]}/{_PREV[0]} and {_CUR[1]}/{_CUR[0]}")
    print("  Remove later with:  python seed_demo_cash_recon.py --remove")


if __name__ == '__main__':
    main()
