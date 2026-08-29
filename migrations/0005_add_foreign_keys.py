"""Add foreign-key and CHECK constraints to the three deduction *_cents tables.

SQLite cannot add constraints with ALTER TABLE, so each table is rebuilt via
CREATE … _new / copy / DROP / RENAME.

Critical fix: the rands-view (e.g. uniform_deductions) references the base
cents table.  Python's sqlite3 implicitly commits DDL, so if we DROP the cents
table while the view still exists, SQLite will error with "error in view …: no
such table" on any subsequent statement that triggers schema validation.
The solution is to DROP the view FIRST, rebuild the table, then RECREATE the
view — so the view is never left pointing at a non-existent table.

The view definitions are duplicated from 0001/0002/0003 and must be kept in
sync (they rarely change).
"""

# ── View SQL (must match 0001/0002/0003) ─────────────────────────────────────

UNIFORM_VIEW_SQL = """\
    SELECT id, employee_id, sku, description, sale_number,
           total_amount_cents / 100.0      AS total_amount,
           monthly_amount_cents / 100.0    AS monthly_amount,
           term_months, start_month, start_year, payments_made, status,
           created_at, notes, end_date,
           CASE WHEN balance_remaining_cents IS NULL THEN NULL
                ELSE balance_remaining_cents / 100.0 END AS balance_remaining
    FROM uniform_deductions_cents
"""

LAYBY_VIEW_SQL = """\
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

UNDERCHARGES_VIEW_SQL = """\
    SELECT id, employee_id, sale_number,
           total_amount_cents / 100.0 AS total_amount,
           reason, incident_month, incident_year, recovery_method, split_months,
           payments_made, status, created_at, notes, type,
           reimburse_month, reimburse_year, start_month, start_year
    FROM undercharges_cents
"""

# ── Table rebuild specs ──────────────────────────────────────────────────────

TABLES = [
    {
        'base': 'uniform_deductions_cents',
        'view_name': 'uniform_deductions',
        'view_sql': UNIFORM_VIEW_SQL,
        'create_new': """\
            CREATE TABLE uniform_deductions_cents_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT NOT NULL,
                sku TEXT,
                description TEXT,
                sale_number TEXT,
                total_amount_cents INTEGER,
                monthly_amount_cents INTEGER NOT NULL,
                term_months INTEGER NOT NULL CHECK(term_months > 0),
                start_month INTEGER NOT NULL,
                start_year INTEGER NOT NULL,
                payments_made INTEGER DEFAULT 0 CHECK(payments_made >= 0),
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT (datetime('now')),
                notes TEXT,
                end_date TEXT,
                balance_remaining_cents INTEGER,
                FOREIGN KEY (employee_id) REFERENCES employees(id)
            )""",
        'columns': (
            'id, employee_id, sku, description, sale_number, total_amount_cents, '
            'monthly_amount_cents, term_months, start_month, start_year, payments_made, '
            'status, created_at, notes, end_date, balance_remaining_cents'
        ),
        'indexes': [
            "CREATE INDEX IF NOT EXISTS idx_uniform_cents_employee_id ON uniform_deductions_cents(employee_id)",
            "CREATE INDEX IF NOT EXISTS idx_uniform_cents_status ON uniform_deductions_cents(status)",
            "CREATE INDEX IF NOT EXISTS idx_uniform_cents_month_range ON uniform_deductions_cents(start_year * 12 + start_month)",
        ],
    },
    {
        'base': 'layby_deductions_cents',
        'view_name': 'layby_deductions',
        'view_sql': LAYBY_VIEW_SQL,
        'create_new': """\
            CREATE TABLE layby_deductions_cents_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT NOT NULL,
                description TEXT,
                total_amount_cents INTEGER,
                monthly_amount_cents INTEGER NOT NULL,
                term_months INTEGER NOT NULL CHECK(term_months > 0),
                start_month INTEGER NOT NULL,
                start_year INTEGER NOT NULL,
                payments_made INTEGER DEFAULT 0 CHECK(payments_made >= 0),
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT (datetime('now')),
                notes TEXT,
                sale_number TEXT DEFAULT '',
                basket_total_cents INTEGER DEFAULT 0,
                discount_pct REAL DEFAULT 40,
                balance_remaining_cents INTEGER DEFAULT 0,
                FOREIGN KEY (employee_id) REFERENCES employees(id)
            )""",
        'columns': (
            'id, employee_id, description, total_amount_cents, monthly_amount_cents, '
            'term_months, start_month, start_year, payments_made, status, created_at, '
            'notes, sale_number, basket_total_cents, discount_pct, balance_remaining_cents'
        ),
        'indexes': [
            "CREATE INDEX IF NOT EXISTS idx_layby_cents_employee_id ON layby_deductions_cents(employee_id)",
            "CREATE INDEX IF NOT EXISTS idx_layby_cents_status ON layby_deductions_cents(status)",
            "CREATE INDEX IF NOT EXISTS idx_layby_cents_month_range ON layby_deductions_cents(start_year * 12 + start_month)",
        ],
    },
    {
        'base': 'undercharges_cents',
        'view_name': 'undercharges',
        'view_sql': UNDERCHARGES_VIEW_SQL,
        'create_new': """\
            CREATE TABLE undercharges_cents_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT NOT NULL,
                sale_number TEXT,
                total_amount_cents INTEGER NOT NULL,
                reason TEXT,
                incident_month INTEGER,
                incident_year INTEGER,
                recovery_method TEXT DEFAULT 'full',
                split_months INTEGER DEFAULT 1 CHECK(split_months > 0),
                payments_made INTEGER DEFAULT 0 CHECK(payments_made >= 0),
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                notes TEXT,
                type TEXT DEFAULT 'undercharge',
                reimburse_month INTEGER,
                reimburse_year INTEGER,
                start_month INTEGER,
                start_year INTEGER,
                FOREIGN KEY (employee_id) REFERENCES employees(id)
            )""",
        'columns': (
            'id, employee_id, sale_number, total_amount_cents, reason, incident_month, '
            'incident_year, recovery_method, split_months, payments_made, status, '
            'created_at, notes, type, reimburse_month, reimburse_year, start_month, start_year'
        ),
        'indexes': [
            "CREATE INDEX IF NOT EXISTS idx_uc_cents_employee_id ON undercharges_cents(employee_id)",
            "CREATE INDEX IF NOT EXISTS idx_uc_cents_status_type ON undercharges_cents(status, type)",
            "CREATE INDEX IF NOT EXISTS idx_uc_cents_month_range ON undercharges_cents(incident_year * 12 + incident_month)",
        ],
    },
]


def up(conn):
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()}

    for spec in TABLES:
        base = spec['base']
        new = base + '_new'
        view = spec['view_name']

        # 1. Drop the view FIRST — prevents "error in view" during table rebuild.
        if view in existing:
            conn.execute(f"DROP VIEW IF EXISTS {view}")

        # 2. Drop any leftover _new table from a partial previous run.
        conn.execute(f"DROP TABLE IF EXISTS {new}")

        # 3. If the base table doesn't exist (shouldn't happen, but be safe),
        #    skip the rebuild — just recreate the view.
        if base not in existing:
            conn.execute(f"CREATE VIEW {view} AS {spec['view_sql']}")
            continue

        # 4. Check if constraints already exist (idempotent re-run guard).
        #    If the table already has a FK, skip the rebuild.
        fk_rows = conn.execute(f"PRAGMA foreign_key_list({base})").fetchall()
        if fk_rows:
            # Already has foreign keys — just recreate the view and move on.
            conn.execute(f"CREATE VIEW {view} AS {spec['view_sql']}")
            for idx in spec['indexes']:
                conn.execute(idx)
            continue

        # 5. Create the new table with constraints.
        conn.execute(spec['create_new'])

        # 6. Copy data. FK enforcement is ON during migrations (get_db sets
        #    PRAGMA foreign_keys=ON), so this FK-checked INSERT raises
        #    IntegrityError if any row references a missing employee — which
        #    would abort app boot when restoring an older backup that carries
        #    orphaned rows (e.g. left behind by a historical merge/delete).
        #    Drop such orphans first: they point at a non-existent employee and
        #    are already unreachable in the app. Only reachable on a pre-0005
        #    DB (the FK-exists guard at step 4 short-circuits once applied), so
        #    this never touches a healthy live DB.
        cols = spec['columns']
        orphans = conn.execute(
            f"SELECT COUNT(*) FROM {base} WHERE employee_id NOT IN (SELECT id FROM employees)"
        ).fetchone()[0]
        if orphans:
            print(f"[migration 0005] Dropping {orphans} orphaned row(s) from {base} "
                  f"(employee_id not in employees) before adding the foreign key.")
            conn.execute(f"DELETE FROM {base} WHERE employee_id NOT IN (SELECT id FROM employees)")
        conn.execute(f"INSERT INTO {new} ({cols}) SELECT {cols} FROM {base}")

        # 7. Drop old table.
        conn.execute(f"DROP TABLE {base}")

        # 8. Rename new → old.
        conn.execute(f"ALTER TABLE {new} RENAME TO {base}")

        # 9. Recreate the view.
        conn.execute(f"CREATE VIEW {view} AS {spec['view_sql']}")

        # 10. Recreate indexes.
        for idx in spec['indexes']:
            conn.execute(idx)
