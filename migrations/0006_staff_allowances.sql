-- Staff allowances (HQ/DC): an annual goods budget per employee, drawn down by
-- purchases. Not a payroll deduction — no installments, no payroll locks.
-- Money lives in integer cents like the *_cents deduction tables; remaining is
-- always computed (allocated - sum of purchases), never stored.

CREATE TABLE IF NOT EXISTS allowances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id TEXT NOT NULL REFERENCES employees(id),
    year INTEGER NOT NULL,
    allocated_cents INTEGER NOT NULL DEFAULT 0 CHECK (allocated_cents >= 0),
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (employee_id, year)
);

CREATE TABLE IF NOT EXISTS allowance_purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id TEXT NOT NULL REFERENCES employees(id),
    year INTEGER NOT NULL,
    purchase_date TEXT,
    sku TEXT,
    description TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    line_total_cents INTEGER NOT NULL CHECK (line_total_cents >= 0),
    location TEXT,
    sale_number TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_allowance_purchases_emp_year
    ON allowance_purchases(employee_id, year);
CREATE INDEX IF NOT EXISTS idx_allowances_year ON allowances(year);
