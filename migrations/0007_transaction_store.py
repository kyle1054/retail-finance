"""P2 — record the employee's store at the moment a deduction is ticked.

Adds a `store` column to deduction_transactions_cents plus an AFTER INSERT
trigger that stamps it from employees.current_store. This preserves where a
deduction was actually taken even after the employee later moves stores, so
historical/locked-month reporting can be made store-accurate without rewriting
every tick site (the trigger fills it in automatically). The rands view is
recreated to expose `store`, and existing rows are backfilled with the
employee's current store (the best approximation available retroactively).
"""

VIEW_SQL = """
    SELECT id, plan_type, plan_id, employee_id, amount_cents / 100.0 AS amount,
           year, month, created_at, voided, store
    FROM deduction_transactions_cents"""


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(deduction_transactions_cents)")}
    if 'store' not in cols:
        conn.execute("ALTER TABLE deduction_transactions_cents ADD COLUMN store TEXT")

    # Recreate the rands view so `store` rides along with the money fields.
    conn.execute("DROP VIEW IF EXISTS deduction_transactions")
    conn.execute("CREATE VIEW deduction_transactions AS " + VIEW_SQL)

    # Auto-stamp the store from the employee's current store at insert time,
    # only when the caller didn't set one. Keeps all existing INSERTs untouched.
    conn.execute("DROP TRIGGER IF EXISTS trg_dt_cents_store")
    conn.execute("""
        CREATE TRIGGER trg_dt_cents_store
        AFTER INSERT ON deduction_transactions_cents
        FOR EACH ROW WHEN NEW.store IS NULL
        BEGIN
            UPDATE deduction_transactions_cents
               SET store = (SELECT current_store FROM employees WHERE id = NEW.employee_id)
             WHERE id = NEW.id;
        END""")

    # Backfill historical rows with the employee's current store.
    conn.execute("""
        UPDATE deduction_transactions_cents
           SET store = (SELECT current_store FROM employees
                         WHERE id = deduction_transactions_cents.employee_id)
         WHERE store IS NULL""")
