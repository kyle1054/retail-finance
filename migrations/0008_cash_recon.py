"""Phase 1 — store-level cash reconciliation ledger.

Mimics the per-store "Cash Recon" sheet but with guard rails: categories are a
fixed picklist (each mapped to its Xero account code, from the workbook's
Variables tab), the running balance is always computed (never hand-typed), and
the opening balance carries forward. Receipts + OCR + Xero export come later;
the receipt_id column is reserved now so the schema is stable.

All money is stored as integer cents, consistent with the rest of the app.
"""

# (name, kind, xero_code, requires_receipt)
#   kind: 'income' | 'expense' | 'transfer' | 'adjustment'
#   xero_code from the configured category mapping; None where it isn't a coded expense.
SEED_CATEGORIES = [
    ('Cash Sale (specify receipt number)', 'income',   None,  False),
    ('Cash Float Top Up',                  'income',   None,  False),
    ('Cash Top Up',                        'income',   None,  False),
    ('Banked',                             'transfer', None,  False),
    ('Cash Balancing',                     'adjustment', None, False),
    ('Airtime',                            'expense',  '6310', True),
    ('Coffee',                             'expense',  '6330', True),
    ('Electricity',                        'expense',  '6030', True),
    ('Milk',                               'expense',  '6330', True),
    ('Parking (specify in notes)',         'expense',  '6330', True),
    ('Fuel (specify in notes)',          'expense',  '6330', True),
    ('Printing & Stationery (specify in notes)', 'expense', '6240', True),
    ('Hygiene Supplies',                          'expense',  '6180', True),
    ('Staff Transport',                    'expense',  '6330', True),
    ('Staff Welfare',                      'expense',  '6330', True),
    ('Cleaning Services (specify in notes)', 'expense', '6180', True),
    ('Store Consumables (specify in notes)', 'expense', '6180', True),
    ('Store Displays',                      'expense',  '6180', True),
    ('Sugar',                              'expense',  '6330', True),
    ('Team Refreshments',                  'expense',  '6330', True),
    ('Water',                              'expense',  '6330', True),
    ('Store Expense Other (specify)',      'expense',  None,  True),
]


def up(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS recon_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL DEFAULT 'expense',
            xero_code TEXT,
            requires_receipt INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        )''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS cash_recon_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            entry_date TEXT NOT NULL,                 -- ISO yyyy-mm-dd
            category_id INTEGER REFERENCES recon_categories(id),
            description TEXT,                          -- category name snapshot
            direction TEXT NOT NULL DEFAULT 'out',     -- 'in' raises the float, 'out' lowers it
            amount_cents INTEGER NOT NULL DEFAULT 0,   -- always positive magnitude
            note TEXT,
            receipt_id INTEGER,                        -- reserved for Phase 2
            status TEXT NOT NULL DEFAULT 'submitted',  -- draft | submitted | approved
            created_by TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recon_entries_store_date "
                 "ON cash_recon_entries(store, entry_date)")

    # Opening float per store per month (editable; defaults to prior month's close).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cash_recon_opening (
            store TEXT NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            opening_cents INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (store, year, month)
        )''')

    # Seed the picklist once (idempotent on name).
    for i, (name, kind, code, rcpt) in enumerate(SEED_CATEGORIES):
        conn.execute(
            "INSERT OR IGNORE INTO recon_categories "
            "(name, kind, xero_code, requires_receipt, sort_order) VALUES (?,?,?,?,?)",
            (name, kind, code, 1 if rcpt else 0, i))
