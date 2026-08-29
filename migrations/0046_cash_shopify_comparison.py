"""Persist monthly Shopify cash exports, store aliases, and variance reasons."""


MAPPINGS = (
    ('NORTHWIND Riverbend', 'Riverbend'), ('NORTHWIND Fairhaven', 'Stonebridge - Fairhaven'),
    ('NORTHWIND Westgate', 'Westgate'), ('NORTHWIND Kingsway', 'Kingsway'),
    ('NORTHWIND Ashford Square', 'Ashford'), ('NORTHWIND Brookfield', 'Brookfield'),
    ('NORTHWIND Fairview', 'Fairview'), ('NORTHWIND Camden', 'Mill Street'),
    ('NORTHWIND Crossroads Mall', 'Crossroads'), ('NORTHWIND Lakeside', 'Lakeside'),
    ('NORTHWIND Northgate', 'Northgate'), ('NORTHWIND Elmwood Central', 'Elmwood'),
    ('NORTHWIND Sunfield Mall', 'Sunfield'), ('NORTHWIND Ravine Street', 'Ravine'),
    ('NORTHWIND Vineyard', 'Vineyard'), ('NORTHWIND Highland Mall', 'Highland Mall'),
    ('NORTHWIND Parkview', 'Parkview Mall'), ('NORTHWIND Grand Central Mall', 'Grand Central Mall'),
    ('NORTHWIND Riverside', 'Riverside'), ('NORTHWIND Eastvale', 'Eastvale'),
    ('NORTHWIND The Atrium', 'The Atrium'), ('NORTHWIND Rosewood', 'Rosewood'),
    ('NORTHWIND Summit City', 'Summit'), ('NORTHWIND Somerton', 'Somerton Mall'),
    ('NORTHWIND Oakvale', 'Oakvale'), ('NORTHWIND Harbour Point', 'Harbour Point'),
    ('NORTHWIND Wellstone Park', 'Wellstone'), ('NORTHWIND Baymouth', 'Baymouth'),
    ('NORTHWIND Woodhaven', 'Woodhaven'),
)

JOURNAL_LABELS = {
    'Stonebridge - Fairhaven': 'Fairhaven', 'Sunfield': 'Sunfield Mall',
    'Grand Central Mall': 'GCM', 'Parkview Mall': 'Parkview Park',
}


def up(conn):
    columns = {r[1] for r in conn.execute("PRAGMA table_info(stores)")}
    if 'cash_sales_label' not in columns:
        conn.execute("ALTER TABLE stores ADD COLUMN cash_sales_label TEXT")
    statements = (
        '''CREATE TABLE IF NOT EXISTS cash_shopify_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL CHECK(year BETWEEN 2000 AND 2100),
            month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
            source_filename TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            uploaded_by TEXT,
            uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(year, month)
        )''',
        '''CREATE TABLE IF NOT EXISTS cash_shopify_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            upload_id INTEGER NOT NULL REFERENCES cash_shopify_uploads(id) ON DELETE CASCADE,
            source_row INTEGER NOT NULL,
            pos_location_name TEXT NOT NULL,
            payment_gateway TEXT NOT NULL,
            order_name TEXT,
            transactions INTEGER NOT NULL,
            gross_cents INTEGER NOT NULL,
            refunded_cents INTEGER NOT NULL,
            net_cents INTEGER NOT NULL
        )''',
        '''CREATE INDEX IF NOT EXISTS idx_cash_shopify_rows_upload
            ON cash_shopify_rows(upload_id)''',
        '''CREATE TABLE IF NOT EXISTS cash_shopify_store_mappings (
            shopify_location TEXT PRIMARY KEY COLLATE NOCASE,
            store TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )''',
        '''CREATE TABLE IF NOT EXISTS cash_sales_variance_reasons (
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            store TEXT NOT NULL,
            reason TEXT NOT NULL,
            updated_by TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY(year, month, store)
        )''',
    )
    for statement in statements:
        conn.execute(statement)
    conn.executemany(
        "INSERT OR IGNORE INTO cash_shopify_store_mappings "
        "(shopify_location, store) VALUES (?, ?)", MAPPINGS)
    conn.executemany(
        "UPDATE stores SET cash_sales_label=? WHERE name=? AND cash_sales_label IS NULL",
        [(label, store) for store, label in JOURNAL_LABELS.items()])
