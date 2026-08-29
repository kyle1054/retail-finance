"""Credit-card Xero account coding: AI account suggestions + merchant memory.

Adds coding columns to cc_lines — the AI's suggested Xero account (code/name/
confidence/rationale/review flag/source) plus the admin's confirmed override —
and a GLOBAL merchant->account memory so a confirmed coding is reused across all
cards and months. `coding_dirty` marks lines that still need a suggestion (set on
import and whenever the cardholder's reason changes). Additive + idempotent.
"""


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def up(conn):
    have = _cols(conn, 'cc_lines')
    additions = [
        ('ai_account_code', "ALTER TABLE cc_lines ADD COLUMN ai_account_code TEXT"),
        ('ai_account_name', "ALTER TABLE cc_lines ADD COLUMN ai_account_name TEXT"),
        ('ai_confidence', "ALTER TABLE cc_lines ADD COLUMN ai_confidence TEXT"),
        ('ai_rationale', "ALTER TABLE cc_lines ADD COLUMN ai_rationale TEXT"),
        ('ai_needs_review', "ALTER TABLE cc_lines ADD COLUMN ai_needs_review INTEGER NOT NULL DEFAULT 0"),
        ('ai_source', "ALTER TABLE cc_lines ADD COLUMN ai_source TEXT"),        # memory | ai
        ('ai_coded_at', "ALTER TABLE cc_lines ADD COLUMN ai_coded_at TEXT"),
        ('coding_dirty', "ALTER TABLE cc_lines ADD COLUMN coding_dirty INTEGER NOT NULL DEFAULT 1"),
        ('xero_account_code', "ALTER TABLE cc_lines ADD COLUMN xero_account_code TEXT"),
        ('xero_account_name', "ALTER TABLE cc_lines ADD COLUMN xero_account_name TEXT"),
    ]
    for col, sql in additions:
        if col not in have:
            conn.execute(sql)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_lines_coding_dirty "
                 "ON cc_lines(coding_dirty, category)")

    # Global merchant -> account memory (keyed on the normalised merchant only, so
    # one confirmation applies across every card and month).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cc_merchant_map (
            merchant_key TEXT PRIMARY KEY,
            account_code TEXT NOT NULL,
            account_name TEXT,
            hits INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT DEFAULT (datetime('now'))
        )''')
