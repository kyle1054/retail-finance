"""Credit-card AI receipt matching: extraction bookkeeping + match suggestions.

Adds AI-extraction columns to cc_receipts and a suggestions table that keeps
AI-proposed receipt<->line matches SEPARATE from confirmed links
(cc_receipt_lines) — so a suggestion never counts as 'covered' until a human
(or an exact auto-match) confirms it.

All additive and idempotent; safe to re-run.
"""


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def up(conn):
    have = _cols(conn, 'cc_receipts')
    # ai_status: NULL/pending = not yet processed; then processed | failed | unreadable.
    if 'ai_status' not in have:
        conn.execute("ALTER TABLE cc_receipts ADD COLUMN ai_status TEXT")
    if 'ai_processed_at' not in have:
        conn.execute("ALTER TABLE cc_receipts ADD COLUMN ai_processed_at TEXT")
    # download_name: overrides the served filename once a receipt is matched to a
    # transaction, so downloads/zips are self-labelling
    # (e.g. 2026-06-14_Greenfields_R342.10.pdf).
    if 'download_name' not in have:
        conn.execute("ALTER TABLE cc_receipts ADD COLUMN download_name TEXT")

    conn.execute('''
        CREATE TABLE IF NOT EXISTS cc_line_receipt_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_id INTEGER NOT NULL REFERENCES cc_lines(id) ON DELETE CASCADE,
            receipt_id INTEGER NOT NULL REFERENCES cc_receipts(id) ON DELETE CASCADE,
            score REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'suggested',  -- suggested | confirmed | rejected
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE (line_id, receipt_id)
        )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_suggestions_line "
                 "ON cc_line_receipt_suggestions(line_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_suggestions_receipt "
                 "ON cc_line_receipt_suggestions(receipt_id)")
