"""Regenerate tests/golden_entered_amounts.json from the current DB state.

The golden file is the "don't silently change entered data" baseline used by
tests/test_entered_amounts.py. It legitimately needs regenerating after plans
are intentionally edited in the app (term/amount/payment adjustments) — at that
point the live data is correct and the golden file is stale.

Run from the project root:  python3 tests/regen_golden.py
Only run this when you have confirmed the current DB values are the ones you
want to lock in as the new baseline. Reads the live DB; never mutates it.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from northwind.data import database as db

GOLDEN = os.path.join(os.path.dirname(__file__), 'golden_entered_amounts.json')


def build():
    conn = db.get_db()
    try:
        snap = {'uniform': {}, 'layby': {}, 'undercharge': {}}

        for r in conn.execute(
            "SELECT id, total_amount, monthly_amount, term_months, payments_made "
            "FROM uniform_deductions").fetchall():
            snap['uniform'][str(r['id'])] = {
                'total': r['total_amount'], 'monthly': r['monthly_amount'],
                'term': r['term_months'], 'paid': r['payments_made'],
            }

        for r in conn.execute(
            "SELECT id, total_amount, monthly_amount, term_months, payments_made, "
            "basket_total, discount_pct FROM layby_deductions").fetchall():
            snap['layby'][str(r['id'])] = {
                'total': r['total_amount'], 'monthly': r['monthly_amount'],
                'term': r['term_months'], 'paid': r['payments_made'],
                'basket': r['basket_total'], 'disc': r['discount_pct'],
            }

        for r in conn.execute(
            "SELECT id, total_amount, split_months, payments_made, recovery_method "
            "FROM undercharges").fetchall():
            snap['undercharge'][str(r['id'])] = {
                'total': r['total_amount'], 'split': r['split_months'],
                'paid': r['payments_made'], 'method': r['recovery_method'],
            }
        return snap
    finally:
        conn.close()


if __name__ == '__main__':
    snap = build()
    json.dump(snap, open(GOLDEN, 'w'), indent=2, sort_keys=True)
    print(f"wrote {GOLDEN}: "
          f"{len(snap['uniform'])} uniform, {len(snap['layby'])} layby, "
          f"{len(snap['undercharge'])} undercharge")
