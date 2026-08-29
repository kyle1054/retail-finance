"""Credit-card receipts are dropped into a monthly *bucket*, not matched 1:1.

A cardholder uploads all their receipts/invoices for a month; each file is
attached to the statement (the month), with line_id left NULL until it's matched
to a specific transaction (by an admin or the Claude-vision pass later). So add
statement_id to cc_receipts and index it for fast per-month bucket lookups.
"""


def up(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cc_receipts)").fetchall()]
    if 'statement_id' not in cols:
        # ALTER ADD COLUMN can't carry an inline FK in SQLite; the relationship
        # is enforced in code (receipts are always written with a real statement).
        conn.execute("ALTER TABLE cc_receipts ADD COLUMN statement_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_receipts_statement "
                 "ON cc_receipts(statement_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_receipts_card "
                 "ON cc_receipts(card_id)")
