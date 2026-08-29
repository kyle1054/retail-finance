"""Staff requests — staff ask for a uniform or a lay-by; admins approve into a plan.

Three tables:
  staff_requests        one row per ask, with its workflow status and (once
                        approved) a pointer to the deduction plan it became
  staff_request_items   what they asked for; unit_price_cents is NULLABLE because
                        staff frequently don't know the price
  staff_request_events  the audit/comment thread — every status change and message

Money is in cents, matching every other money column. `estimated_total_cents` is
deliberately an ESTIMATE: the authoritative amount is written by
northwind/deductions/plans.py when the request is converted, so this table can never
restate a balance.
"""

STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS staff_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref TEXT UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('uniform', 'layby')),
        employee_id TEXT NOT NULL REFERENCES employees(id),
        store TEXT,
        sector TEXT NOT NULL DEFAULT 'retail',
        status TEXT NOT NULL DEFAULT 'submitted' CHECK (status IN (
            'submitted', 'in_progress', 'info_needed', 'approved',
            'declined', 'cancelled', 'fulfilled')),
        requested_term_months INTEGER,
        estimated_total_cents INTEGER CHECK (estimated_total_cents IS NULL
                                             OR estimated_total_cents >= 0),
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        created_by TEXT,
        created_via TEXT,
        updated_at TEXT,
        claimed_by TEXT,
        claimed_at TEXT,
        decided_by TEXT,
        decided_at TEXT,
        decline_reason TEXT,
        plan_type TEXT,
        plan_id INTEGER
    )''',
    "CREATE INDEX IF NOT EXISTS idx_staff_requests_status ON staff_requests(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_staff_requests_employee ON staff_requests(employee_id)",
    "CREATE INDEX IF NOT EXISTS idx_staff_requests_store ON staff_requests(store)",

    '''CREATE TABLE IF NOT EXISTS staff_request_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL REFERENCES staff_requests(id) ON DELETE CASCADE,
        description TEXT NOT NULL,
        sku TEXT,
        size TEXT,
        quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
        unit_price_cents INTEGER CHECK (unit_price_cents IS NULL OR unit_price_cents >= 0),
        sort_order INTEGER NOT NULL DEFAULT 0
    )''',
    "CREATE INDEX IF NOT EXISTS idx_staff_request_items_request "
    "ON staff_request_items(request_id, sort_order)",

    '''CREATE TABLE IF NOT EXISTS staff_request_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL REFERENCES staff_requests(id) ON DELETE CASCADE,
        at TEXT NOT NULL DEFAULT (datetime('now')),
        actor TEXT,
        actor_role TEXT,
        event TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        message TEXT
    )''',
    "CREATE INDEX IF NOT EXISTS idx_staff_request_events_request "
    "ON staff_request_events(request_id, id)",
)


def up(conn):
    for sql in STATEMENTS:
        conn.execute(sql)
