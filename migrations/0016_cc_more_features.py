"""Three small QoL additions:

- cc_lines.personal       — cardholder flags a charge as personal / to repay.
- cc_statements.submitted_at / submitted_by — "submit month" locks the period.
- cc_receipts.content_hash — sha256 of the file bytes, for duplicate detection.
"""


def _add(conn, table, col, decl):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def up(conn):
    _add(conn, 'cc_lines', 'personal', 'INTEGER NOT NULL DEFAULT 0')
    _add(conn, 'cc_statements', 'submitted_at', 'TEXT')
    _add(conn, 'cc_statements', 'submitted_by', 'TEXT')
    _add(conn, 'cc_receipts', 'content_hash', 'TEXT')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_receipts_hash "
                 "ON cc_receipts(card_id, content_hash)")
