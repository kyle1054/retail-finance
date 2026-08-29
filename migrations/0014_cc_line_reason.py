"""Cardholders must give a reason / description per transaction.

an admin needs this to code the expense in Xero and to put a reason on the sync.
A transaction is only "complete" once it has both a receipt and a reason.
"""


def up(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cc_lines)").fetchall()]
    if 'reason' not in cols:
        conn.execute("ALTER TABLE cc_lines ADD COLUMN reason TEXT")
