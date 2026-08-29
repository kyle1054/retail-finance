"""A receipt can belong to MULTIPLE transactions (and a transaction to many
receipts) — e.g. one invoice covers several card charges.

Introduce a join table cc_receipt_lines as the single source of truth for
receipt↔line links, and backfill the existing one-to-one links from
cc_receipts.line_id. The old line_id column is left in place but no longer used
for linking (kept to avoid a destructive rebuild). The "bucket" is now any
receipt with zero rows in this table.
"""


def up(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cc_receipt_lines (
            receipt_id INTEGER NOT NULL REFERENCES cc_receipts(id) ON DELETE CASCADE,
            line_id INTEGER NOT NULL REFERENCES cc_lines(id) ON DELETE CASCADE,
            PRIMARY KEY (receipt_id, line_id)
        )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_receipt_lines_line "
                 "ON cc_receipt_lines(line_id)")
    # Backfill existing single links.
    conn.execute("INSERT OR IGNORE INTO cc_receipt_lines (receipt_id, line_id) "
                 "SELECT id, line_id FROM cc_receipts WHERE line_id IS NOT NULL")
