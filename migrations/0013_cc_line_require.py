"""Let an admin flag a specific transaction as needing its *own* receipt.

Receipts are normally dropped into a monthly bucket, but an admin may require a
particular line to have its own receipt attached (or the AI/ cardholder may
link one). This flag drives a "receipt required" badge on that line in the
cardholder portal. Coverage is otherwise derived from cc_receipts.line_id.
"""


def up(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cc_lines)").fetchall()]
    if 'require_individual' not in cols:
        conn.execute("ALTER TABLE cc_lines ADD COLUMN "
                     "require_individual INTEGER NOT NULL DEFAULT 0")
