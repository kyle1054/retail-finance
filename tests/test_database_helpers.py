"""Tests for database.py helper functions.

Covers the summary/total helpers, sector-aware period locking, the sector
lookup, and the employee-id generator. All run against the throwaway db_copy
fixture (see conftest.py) so the live database is never touched.
"""
from northwind.data import database as db


# --- get_outstanding_summary ----------------------------------------------

def test_outstanding_summary_shape_and_total(conn):
    emp = conn.execute(
        "SELECT id FROM employees WHERE status='active' LIMIT 1").fetchone()
    s = db.get_outstanding_summary(emp['id'])
    assert set(s.keys()) == {'uniform', 'layby', 'undercharges', 'total'}
    # Total is the sum of the three categories (within rounding).
    assert abs(s['total'] - (s['uniform'] + s['layby'] + s['undercharges'])) <= 0.01
    # No category should be negative.
    assert all(s[k] >= 0 for k in ('uniform', 'layby', 'undercharges', 'total'))


def test_outstanding_summary_unknown_employee_is_zero(conn):
    s = db.get_outstanding_summary('EMP-DOES-NOT-EXIST')
    assert s == {'uniform': 0, 'layby': 0, 'undercharges': 0, 'total': 0}


# --- get_category_totals ---------------------------------------------------

def test_category_totals_shape(conn):
    emp = conn.execute(
        "SELECT id FROM employees WHERE status='active' LIMIT 1").fetchone()
    cats = db.get_category_totals(emp['id'])
    for section in ('uniform', 'layby', 'undercharges'):
        assert section in cats, f"missing section {section}"
        assert set(cats[section].keys()) >= {'charged', 'paid', 'remaining'}


def test_category_totals_paid_plus_remaining_within_charged(conn):
    # For any employee, remaining should never exceed charged in a section.
    emp = conn.execute(
        "SELECT id FROM employees WHERE status='active' LIMIT 1").fetchone()
    cats = db.get_category_totals(emp['id'])
    for section in ('uniform', 'layby', 'undercharges'):
        c = cats[section]
        assert c['remaining'] <= c['charged'] + 0.01


# --- is_period_locked: sector independence ---------------------------------

def test_period_lock_is_sector_independent(conn):
    # Use a far-future period that cannot collide with real payroll data.
    year, month = 2099, 7
    conn.execute("DELETE FROM locked_periods WHERE year=? AND month=?", (year, month))
    conn.execute(
        "INSERT INTO locked_periods (sector, year, month) VALUES ('retail', ?, ?)",
        (year, month))
    conn.commit()
    try:
        assert db.is_period_locked(year, month, 'retail') is True
        # HQ must remain unlocked even though Retail locked the same month.
        assert db.is_period_locked(year, month, 'hq') is False
    finally:
        conn.execute("DELETE FROM locked_periods WHERE year=? AND month=?", (year, month))
        conn.commit()


def test_period_lock_absent_is_false(conn):
    assert db.is_period_locked(1990, 1, 'retail') is False
    assert db.is_period_locked(1990, 1, 'hq') is False


# --- get_employee_sector ---------------------------------------------------

def test_employee_sector_defaults_to_retail_for_unknown(conn):
    assert db.get_employee_sector('EMP-DOES-NOT-EXIST') == 'retail'


def test_employee_sector_returns_stored_value(conn):
    emp = conn.execute("SELECT id, sector FROM employees LIMIT 1").fetchone()
    expected = emp['sector'] if emp['sector'] else 'retail'
    assert db.get_employee_sector(emp['id']) == expected


def test_employee_sector_accepts_shared_connection(conn):
    emp = conn.execute("SELECT id FROM employees LIMIT 1").fetchone()
    # Passing an existing connection must not close it.
    sector = db.get_employee_sector(emp['id'], conn=conn)
    assert sector in ('retail', 'hq')
    # conn still usable afterwards.
    assert conn.execute("SELECT 1").fetchone()[0] == 1


# --- next_employee_id ------------------------------------------------------

def test_next_employee_id_format_and_uniqueness(conn):
    new_id = db.next_employee_id(conn)
    assert new_id.startswith('EMP-')
    assert len(new_id) == len('EMP-0000')
    assert new_id[4:].isdigit()
    # It must not already exist.
    exists = conn.execute("SELECT 1 FROM employees WHERE id=?", (new_id,)).fetchone()
    assert exists is None


def test_next_employee_id_is_greater_than_current_max(conn):
    row = conn.execute(
        "SELECT COALESCE(MAX(CAST(SUBSTR(id, 5) AS INTEGER)), 0) AS m "
        "FROM employees WHERE id LIKE 'EMP-%'").fetchone()
    new_n = int(db.next_employee_id(conn)[4:])
    assert new_n == row['m'] + 1
