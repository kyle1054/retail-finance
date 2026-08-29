"""Store layby_deductions money as integer cents (same pattern as 0001).

Money columns -> *_cents INTEGER on base table layby_deductions_cents; a view
layby_deductions aliases them back to Rands. discount_pct is a percentage, not
money, so it stays REAL.
"""

VIEW_SQL = """
    SELECT id, employee_id, description,
           total_amount_cents / 100.0   AS total_amount,
           monthly_amount_cents / 100.0 AS monthly_amount,
           term_months, start_month, start_year, payments_made, status,
           created_at, notes, sale_number,
           basket_total_cents / 100.0   AS basket_total,
           discount_pct,
           balance_remaining_cents / 100.0 AS balance_remaining
    FROM layby_deductions_cents
"""


def up(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()}

    if 'layby_deductions_cents' not in names:
        conn.execute("""
            CREATE TABLE layby_deductions_cents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT NOT NULL,
                description TEXT,
                total_amount_cents INTEGER,
                monthly_amount_cents INTEGER NOT NULL,
                term_months INTEGER NOT NULL,
                start_month INTEGER NOT NULL,
                start_year INTEGER NOT NULL,
                payments_made INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT (datetime('now')),
                notes TEXT,
                sale_number TEXT DEFAULT '',
                basket_total_cents INTEGER DEFAULT 0,
                discount_pct REAL DEFAULT 40,
                balance_remaining_cents INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            INSERT INTO layby_deductions_cents
                (id, employee_id, description, total_amount_cents, monthly_amount_cents,
                 term_months, start_month, start_year, payments_made, status, created_at,
                 notes, sale_number, basket_total_cents, discount_pct, balance_remaining_cents)
            SELECT id, employee_id, description,
                   CAST(ROUND(total_amount   * 100) AS INTEGER),
                   CAST(ROUND(monthly_amount * 100) AS INTEGER),
                   term_months, start_month, start_year, payments_made, status, created_at,
                   notes, sale_number,
                   CAST(ROUND(COALESCE(basket_total, 0) * 100) AS INTEGER),
                   discount_pct,
                   CAST(ROUND(COALESCE(balance_remaining, 0) * 100) AS INTEGER)
            FROM layby_deductions
        """)

    row = conn.execute("SELECT type FROM sqlite_master WHERE name='layby_deductions'").fetchone()
    if row and row[0] == 'table':
        conn.execute("DROP TABLE layby_deductions")

    conn.execute("DROP VIEW IF EXISTS layby_deductions")
    conn.execute("CREATE VIEW layby_deductions AS " + VIEW_SQL)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_layby_cents_employee_id ON layby_deductions_cents(employee_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_layby_cents_status ON layby_deductions_cents(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_layby_cents_month_range ON layby_deductions_cents(start_year * 12 + start_month)")
