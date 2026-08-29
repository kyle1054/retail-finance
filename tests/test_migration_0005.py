"""Regression test for migration 0005's orphan handling.

Restoring an older backup that predates 0005 and carries a deduction row whose
employee_id is absent from `employees` used to crash app boot: the FK-checked
copy raised IntegrityError (FK enforcement is ON during migrations). 0005 now
drops such orphans before adding the constraint. This test reconstructs that
exact pre-0005 shape and asserts the migration succeeds, drops the orphan, and
keeps the valid row.
"""
import importlib.util
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_migration():
    path = os.path.join(ROOT, 'migrations', '0005_add_foreign_keys.py')
    spec = importlib.util.spec_from_file_location('mig0005', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_0005_drops_orphans_instead_of_crashing():
    mod = _load_migration()
    uniform_spec = mod.TABLES[0]
    assert uniform_spec['base'] == 'uniform_deductions_cents'

    conn = sqlite3.connect(':memory:')
    conn.execute("PRAGMA foreign_keys = ON")  # mirrors get_db() during migrations

    # employees (parent) + one valid employee.
    conn.execute("CREATE TABLE employees (id TEXT PRIMARY KEY, full_name TEXT)")
    conn.execute("INSERT INTO employees (id, full_name) VALUES ('E1', 'Real Person')")

    # Pre-0005 uniform table: same columns as the spec, but NO foreign key.
    conn.execute("""
        CREATE TABLE uniform_deductions_cents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT NOT NULL,
            sku TEXT, description TEXT, sale_number TEXT,
            total_amount_cents INTEGER, monthly_amount_cents INTEGER,
            term_months INTEGER, start_month INTEGER, start_year INTEGER,
            payments_made INTEGER DEFAULT 0, status TEXT DEFAULT 'active',
            created_at TEXT, notes TEXT, end_date TEXT, balance_remaining_cents INTEGER
        )""")
    conn.execute(f"CREATE VIEW uniform_deductions AS {uniform_spec['view_sql']}")

    # One valid row (E1 exists) and one orphan (GHOST does not).
    conn.execute("INSERT INTO uniform_deductions_cents (employee_id, monthly_amount_cents, term_months, start_month, start_year) "
                 "VALUES ('E1', 10000, 3, 1, 2025)")
    conn.execute("INSERT INTO uniform_deductions_cents (employee_id, monthly_amount_cents, term_months, start_month, start_year) "
                 "VALUES ('GHOST', 10000, 3, 1, 2025)")

    # Run only the uniform table through the migration.
    original_tables = mod.TABLES
    mod.TABLES = [uniform_spec]
    try:
        mod.up(conn)  # must NOT raise IntegrityError
    finally:
        mod.TABLES = original_tables

    # Orphan dropped, valid row kept.
    rows = conn.execute("SELECT employee_id FROM uniform_deductions_cents").fetchall()
    assert [r[0] for r in rows] == ['E1'], rows
    # The foreign key was actually added.
    assert conn.execute("PRAGMA foreign_key_list(uniform_deductions_cents)").fetchall()
    conn.close()
