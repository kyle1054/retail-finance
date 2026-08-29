"""Per-transaction "submitted to finance" marker on credit-card lines.

The cardholder portal already had a whole-month submit (a soft `submitted_at`
marker on `cc_statements`). This adds the same idea at the LINE level so a
cardholder can submit individual transactions — or a bulk selection — as they
finish them, rather than only in one month-wide action.

It is a SOFT marker, not a lock (mirrors the 2026-07 removal of the month-submit
edit lock): a submitted line still shows up and stays editable; `submitted_at`
just records that finance has been told about it. `submitted_by` records who.

Idempotent: only adds the columns if they aren't already there.
"""


def up(conn):
    cols = {r['name'] for r in conn.execute("PRAGMA table_info(cc_lines)")}
    if 'submitted_at' not in cols:
        conn.execute("ALTER TABLE cc_lines ADD COLUMN submitted_at TEXT")
    if 'submitted_by' not in cols:
        conn.execute("ALTER TABLE cc_lines ADD COLUMN submitted_by TEXT")
