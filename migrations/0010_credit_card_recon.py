"""Credit Card Reconciliation — schema.

A new top-level section alongside Deductions and Cash Recon. Cardholders are
auto-provisioned by uploading their Xero credit-card reconciliation export
(see credit_card_parser.py): each file self-identifies its card, and we store
the card's *unreconciled* statement lines, classified into:

  - 'spend'    — negative merchant charge → cardholder owes a receipt.
  - 'transfer' — money in / funding → hidden from the cardholder.
  - 'fee'      — bank/system line (#DECLINED AUTH FEE, …) → N/A.

Re-uploading the same card/period is an idempotent MERGE: lines are matched on
(fingerprint, occurrence) so genuine repeats are preserved, duplicates are
never created, and receipts already attached are never wiped. A line that has
since been reconciled in Xero is marked 'cleared' (its receipt kept for audit).

All money is integer cents, consistent with the rest of the app.
"""


def up(conn):
    # ── Cards (identity = the name on the Xero report, e.g. "Primary Credit Card")
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cc_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_name TEXT NOT NULL UNIQUE,        -- from the report; the identity
            display_name TEXT,                     -- "Terrence"
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )''')

    # ── Who may log in / has access to a card (email-based, like store_emails).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cc_card_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL REFERENCES cc_cards(id) ON DELETE CASCADE,
            email TEXT NOT NULL,
            name TEXT,
            access_note TEXT,                      -- "Primary holder", "Assistant", ...
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE (card_id, email)
        )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_card_users_email "
                 "ON cc_card_users(email)")

    # ── A statement period per card. Keyed by (card_id, year, month) so repeated
    #    uploads of the same month merge instead of duplicating (year/month are
    #    derived from the report's 'as at' / period-end date).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cc_statements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL REFERENCES cc_cards(id) ON DELETE CASCADE,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            period_start TEXT,
            period_end TEXT,
            as_at TEXT,
            source_filename TEXT,
            imported_at TEXT DEFAULT (datetime('now')),
            duplicates_removed_by_xero INTEGER NOT NULL DEFAULT 0,
            UNIQUE (card_id, year, month)
        )''')

    # ── The classified statement lines. (statement_id, fingerprint, occurrence)
    #    is the idempotent merge key; receipt_id survives re-imports.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cc_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            statement_id INTEGER NOT NULL REFERENCES cc_statements(id) ON DELETE CASCADE,
            card_id INTEGER NOT NULL REFERENCES cc_cards(id) ON DELETE CASCADE,
            line_date TEXT,
            reference TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,         -- signed; negative = spend
            category TEXT NOT NULL,                -- 'spend' | 'transfer' | 'fee'
            reconciled INTEGER NOT NULL DEFAULT 0, -- from the latest import
            needs_receipt INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'outstanding',  -- 'outstanding' | 'cleared'
            fingerprint TEXT NOT NULL,
            occurrence INTEGER NOT NULL DEFAULT 0,
            receipt_id INTEGER REFERENCES cc_receipts(id) ON DELETE SET NULL,
            first_seen_at TEXT DEFAULT (datetime('now')),
            last_seen_at TEXT DEFAULT (datetime('now')),
            UNIQUE (statement_id, fingerprint, occurrence)
        )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_lines_card "
                 "ON cc_lines(card_id, status, needs_receipt)")

    # ── Uploaded receipt images + AI-extracted fields. File bytes live on disk;
    #    we store the path only. AI columns populated by the Claude-vision pass.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cc_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_id INTEGER REFERENCES cc_lines(id) ON DELETE CASCADE,
            card_id INTEGER NOT NULL REFERENCES cc_cards(id) ON DELETE CASCADE,
            file_path TEXT NOT NULL,
            original_filename TEXT,
            content_type TEXT,
            uploaded_by TEXT,
            uploaded_at TEXT DEFAULT (datetime('now')),
            ai_vendor TEXT,
            ai_date TEXT,
            ai_total_cents INTEGER,
            ai_raw_json TEXT,
            status TEXT NOT NULL DEFAULT 'uploaded'  -- 'uploaded' | 'verified'
        )''')
