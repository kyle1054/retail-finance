"""Store uniform_deductions money as integer cents.

Strategy (see money.py / project notes): the physical table becomes
`uniform_deductions_cents` with *_cents INTEGER columns, and a view
`uniform_deductions` exposes the money back as Rands under the original column
names. So every READ (including SELECT * and SUM aggregates) is unchanged;
only WRITE statements target the base table with x100 conversion.
"""

COLUMNS_VIEW = """
    SELECT id, employee_id, sku, description, sale_number,
           total_amount_cents / 100.0      AS total_amount,
           monthly_amount_cents / 100.0    AS monthly_amount,
           term_months, start_month, start_year, payments_made, status,
           created_at, notes, end_date,
           CASE WHEN balance_remaining_cents IS NULL THEN NULL
                ELSE balance_remaining_cents / 100.0 END AS balance_remaining
    FROM uniform_deductions_cents
"""


def up(conn):
    # If a previous partial run left the base table, reuse it; else build it.
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()}

    if 'uniform_deductions_cents' not in existing:
        conn.execute("""
            CREATE TABLE uniform_deductions_cents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT NOT NULL,
                sku TEXT,
                description TEXT,
                sale_number TEXT,
                total_amount_cents INTEGER,
                monthly_amount_cents INTEGER NOT NULL,
                term_months INTEGER NOT NULL,
                start_month INTEGER NOT NULL,
                start_year INTEGER NOT NULL,
                payments_made INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT (datetime('now')),
                notes TEXT,
                end_date TEXT,
                balance_remaining_cents INTEGER
            )
        """)
        conn.execute("""
            INSERT INTO uniform_deductions_cents
                (id, employee_id, sku, description, sale_number,
                 total_amount_cents, monthly_amount_cents, term_months,
                 start_month, start_year, payments_made, status, created_at,
                 notes, end_date, balance_remaining_cents)
            SELECT id, employee_id, sku, description, sale_number,
                   CAST(ROUND(total_amount   * 100) AS INTEGER),
                   CAST(ROUND(monthly_amount * 100) AS INTEGER),
                   term_months, start_month, start_year, payments_made, status,
                   created_at, notes, end_date,
                   CASE WHEN balance_remaining IS NULL THEN NULL
                        ELSE CAST(ROUND(balance_remaining * 100) AS INTEGER) END
            FROM uniform_deductions
        """)

    # Drop the old physical table (only if it is a real table, not the view).
    row = conn.execute("SELECT type FROM sqlite_master WHERE name='uniform_deductions'").fetchone()
    if row and row[0] == 'table':
        conn.execute("DROP TABLE uniform_deductions")

    conn.execute("DROP VIEW IF EXISTS uniform_deductions")
    conn.execute("CREATE VIEW uniform_deductions AS " + COLUMNS_VIEW)

    # Recreate the indexes (they live on the base table now).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uniform_cents_employee_id ON uniform_deductions_cents(employee_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uniform_cents_status ON uniform_deductions_cents(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uniform_cents_month_range ON uniform_deductions_cents(start_year * 12 + start_month)")
