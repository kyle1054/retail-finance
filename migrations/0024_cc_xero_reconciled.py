"""Admin "reconciled in Xero" flag on credit-card lines.

Distinct from `status` ('cleared' is auto-derived when a line drops out of a
re-uploaded Xero recon export). This is a MANUAL admin tick: once a transaction has been
reconciled in Xero an admin ticks it here to take it out of the working
view, without waiting for (or depending on) a fresh upload.

Idempotent: only adds the columns if they aren't already there.
"""


def up(conn):
    cols = {r['name'] for r in conn.execute("PRAGMA table_info(cc_lines)")}
    if 'xero_reconciled' not in cols:
        conn.execute("ALTER TABLE cc_lines ADD COLUMN "
                     "xero_reconciled INTEGER NOT NULL DEFAULT 0")
    if 'xero_reconciled_at' not in cols:
        conn.execute("ALTER TABLE cc_lines ADD COLUMN xero_reconciled_at TEXT")
