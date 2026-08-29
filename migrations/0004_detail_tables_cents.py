"""Store remaining money tables as integer cents: deduction_transactions,
plan_adjustments, layby_items, overpayments. Same view+base-table pattern.
"""

SPECS = {
    'deduction_transactions': {
        'create': """
            CREATE TABLE deduction_transactions_cents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_type TEXT NOT NULL, plan_id INTEGER NOT NULL,
                employee_id TEXT NOT NULL, amount_cents INTEGER NOT NULL,
                year INTEGER NOT NULL, month INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now')), voided INTEGER DEFAULT 0
            )""",
        'copy': """
            INSERT INTO deduction_transactions_cents
                (id, plan_type, plan_id, employee_id, amount_cents, year, month, created_at, voided)
            SELECT id, plan_type, plan_id, employee_id,
                   CAST(ROUND(amount * 100) AS INTEGER), year, month, created_at, voided
            FROM deduction_transactions""",
        'view': """
            SELECT id, plan_type, plan_id, employee_id, amount_cents / 100.0 AS amount,
                   year, month, created_at, voided
            FROM deduction_transactions_cents""",
        'indexes': [
            "CREATE INDEX IF NOT EXISTS idx_dt_cents_plan ON deduction_transactions_cents(plan_type, plan_id)",
            "CREATE INDEX IF NOT EXISTS idx_dt_cents_date ON deduction_transactions_cents(year, month)",
            # Preserve the partial UNIQUE constraint that prevents duplicate live payments.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_dt_cents_dup ON deduction_transactions_cents(plan_type, plan_id, year, month) WHERE voided = 0",
        ],
    },
    'plan_adjustments': {
        'create': """
            CREATE TABLE plan_adjustments_cents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_type TEXT NOT NULL, plan_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL, note TEXT,
                new_monthly_cents INTEGER, created_at TEXT DEFAULT (datetime('now'))
            )""",
        'copy': """
            INSERT INTO plan_adjustments_cents
                (id, plan_type, plan_id, amount_cents, note, new_monthly_cents, created_at)
            SELECT id, plan_type, plan_id, CAST(ROUND(amount * 100) AS INTEGER), note,
                   CASE WHEN new_monthly IS NULL THEN NULL ELSE CAST(ROUND(new_monthly * 100) AS INTEGER) END,
                   created_at
            FROM plan_adjustments""",
        'view': """
            SELECT id, plan_type, plan_id, amount_cents / 100.0 AS amount, note,
                   CASE WHEN new_monthly_cents IS NULL THEN NULL ELSE new_monthly_cents / 100.0 END AS new_monthly,
                   created_at
            FROM plan_adjustments_cents""",
        'indexes': [],
    },
    'layby_items': {
        'create': """
            CREATE TABLE layby_items_cents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                layby_id INTEGER NOT NULL, description TEXT NOT NULL,
                unit_price_cents INTEGER NOT NULL, quantity INTEGER DEFAULT 1,
                line_total_cents INTEGER NOT NULL
            )""",
        'copy': """
            INSERT INTO layby_items_cents
                (id, layby_id, description, unit_price_cents, quantity, line_total_cents)
            SELECT id, layby_id, description,
                   CAST(ROUND(unit_price * 100) AS INTEGER), quantity,
                   CAST(ROUND(line_total * 100) AS INTEGER)
            FROM layby_items""",
        'view': """
            SELECT id, layby_id, description, unit_price_cents / 100.0 AS unit_price,
                   quantity, line_total_cents / 100.0 AS line_total
            FROM layby_items_cents""",
        'indexes': [
            "CREATE INDEX IF NOT EXISTS idx_li_cents_layby ON layby_items_cents(layby_id)",
        ],
    },
    'overpayments': {
        'create': """
            CREATE TABLE overpayments_cents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT, store TEXT, individual_name TEXT, sale_number TEXT,
                total_amount_cents INTEGER NOT NULL, reason TEXT,
                incident_month INTEGER, incident_year INTEGER, status TEXT DEFAULT 'pending',
                balance_remaining_cents INTEGER DEFAULT 0, corrected_on TEXT, notes TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )""",
        'copy': """
            INSERT INTO overpayments_cents
                (id, employee_id, store, individual_name, sale_number, total_amount_cents,
                 reason, incident_month, incident_year, status, balance_remaining_cents,
                 corrected_on, notes, created_at)
            SELECT id, employee_id, store, individual_name, sale_number,
                   CAST(ROUND(total_amount * 100) AS INTEGER), reason, incident_month, incident_year,
                   status, CAST(ROUND(COALESCE(balance_remaining,0) * 100) AS INTEGER),
                   corrected_on, notes, created_at
            FROM overpayments""",
        'view': """
            SELECT id, employee_id, store, individual_name, sale_number,
                   total_amount_cents / 100.0 AS total_amount, reason, incident_month, incident_year,
                   status, balance_remaining_cents / 100.0 AS balance_remaining, corrected_on, notes, created_at
            FROM overpayments_cents""",
        'indexes': [],
    },
}


def up(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()}
    for table, spec in SPECS.items():
        base = table + '_cents'
        if base not in names:
            conn.execute(spec['create'])
            conn.execute(spec['copy'])
        row = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchone()
        if row and row[0] == 'table':
            conn.execute(f"DROP TABLE {table}")
        conn.execute(f"DROP VIEW IF EXISTS {table}")
        conn.execute(f"CREATE VIEW {table} AS " + spec['view'])
        for idx in spec['indexes']:
            conn.execute(idx)
