"""Store undercharges money as integer cents (same view+base pattern).

Only total_amount is money. Base table undercharges_cents holds total_amount_cents
INTEGER; view undercharges aliases it back to Rands. All other columns unchanged.
"""

VIEW_SQL = """
    SELECT id, employee_id, sale_number,
           total_amount_cents / 100.0 AS total_amount,
           reason, incident_month, incident_year, recovery_method, split_months,
           payments_made, status, created_at, notes, type,
           reimburse_month, reimburse_year, start_month, start_year
    FROM undercharges_cents
"""


def up(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()}

    if 'undercharges_cents' not in names:
        conn.execute("""
            CREATE TABLE undercharges_cents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT NOT NULL,
                sale_number TEXT,
                total_amount_cents INTEGER NOT NULL,
                reason TEXT,
                incident_month INTEGER,
                incident_year INTEGER,
                recovery_method TEXT DEFAULT 'full',
                split_months INTEGER DEFAULT 1,
                payments_made INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                notes TEXT,
                type TEXT DEFAULT 'undercharge',
                reimburse_month INTEGER,
                reimburse_year INTEGER,
                start_month INTEGER,
                start_year INTEGER
            )
        """)
        conn.execute("""
            INSERT INTO undercharges_cents
                (id, employee_id, sale_number, total_amount_cents, reason, incident_month,
                 incident_year, recovery_method, split_months, payments_made, status,
                 created_at, notes, type, reimburse_month, reimburse_year, start_month, start_year)
            SELECT id, employee_id, sale_number,
                   CAST(ROUND(total_amount * 100) AS INTEGER),
                   reason, incident_month, incident_year, recovery_method, split_months,
                   payments_made, status, created_at, notes, type,
                   reimburse_month, reimburse_year, start_month, start_year
            FROM undercharges
        """)

    row = conn.execute("SELECT type FROM sqlite_master WHERE name='undercharges'").fetchone()
    if row and row[0] == 'table':
        conn.execute("DROP TABLE undercharges")

    conn.execute("DROP VIEW IF EXISTS undercharges")
    conn.execute("CREATE VIEW undercharges AS " + VIEW_SQL)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_uc_cents_employee_id ON undercharges_cents(employee_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uc_cents_status_type ON undercharges_cents(status, type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uc_cents_month_range ON undercharges_cents(incident_year * 12 + incident_month)")
