"""End-to-end tests for the payroll-sync ROUTE, through the Flask test client.

`tests/test_payroll_sync_service.py` covers the service layer thoroughly, but the service
was extracted out of this route — and the extraction left a seam that unit tests cannot
see: the route has to translate ~13 form field names into the service's explicit
selections. Get one name wrong and the sync silently does nothing (or, worse, does the
wrong subset) while every service test still passes.

Before this file, `/payroll/sync` had exactly one test: a GET of the empty form. The POST
preview and POST apply — the code that moves people between stores and terminates them —
had none.

These tests COMMIT (a route owns its transaction), and `db_copy` is session-scoped, so
every one of them cleans up after itself in a `finally`.
"""
import pytest

from northwind.data import database as db


def _unique_active_retail(conn, n=1):
    rows = conn.execute(
        "SELECT id, full_name, current_store, job_title, status FROM employees "
        "WHERE status='active' AND sector='retail' "
        "GROUP BY LOWER(full_name) HAVING COUNT(*)=1 "
        "ORDER BY id LIMIT ?", (n,)).fetchall()
    if len(rows) < n:
        pytest.skip("not enough uniquely-named active retail employees")
    return [dict(r) for r in rows]


def _roster(*employees):
    return "\n".join("{}\t{}\t{}".format(
        e["full_name"], e.get("store") or e.get("current_store") or "Riverbend",
        e.get("job_title") or "Ambassador") for e in employees)


def _restore(conn, emp):
    """Undo a route test's committed writes.

    These tests go through the real route, so they COMMIT, and `db_copy` is
    session-scoped — anything left behind leaks into every later test. Restoring the
    employee columns is not enough: a move also CLOSES the previously-open
    `store_history` row (sets `to_date`) and inserts a new one. Deleting the new row
    without reopening the old one leaves the employee with no current store history,
    which is exactly what broke `test_payroll_sync_service.py` when these two files ran
    in the same session. Reopen it.
    """
    conn.execute(
        "UPDATE employees SET full_name=?, current_store=?, job_title=?, status=?, "
        "terminated_at=NULL WHERE id=?",
        (emp["full_name"], emp["current_store"], emp["job_title"], emp["status"],
         emp["id"]))
    conn.execute("DELETE FROM store_history WHERE employee_id=? AND store=?",
                 (emp["id"], "Route Test Store"))
    # Reopen whichever row is now the latest, so the employee is left with exactly one
    # open period again (the invariant the rest of the suite relies on).
    latest = conn.execute(
        "SELECT id FROM store_history WHERE employee_id=? ORDER BY id DESC LIMIT 1",
        (emp["id"],)).fetchone()
    if latest is not None:
        conn.execute("UPDATE store_history SET to_date=NULL WHERE id=?", (latest["id"],))
    conn.commit()


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
def test_preview_renders_a_store_change(client, conn):
    """The seam: the service returns `store_changes`, the template reads it."""
    emp = _unique_active_retail(conn)[0]
    r = client.post("/payroll/sync/preview", data={
        "pasted": _roster({"full_name": emp["full_name"],
                           "store": "Route Test Store"})})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert emp["full_name"] in body
    assert "Route Test Store" in body


def test_preview_rejects_an_empty_paste(client):
    r = client.post("/payroll/sync/preview", data={"pasted": ""}, follow_redirects=True)
    assert r.status_code == 200
    assert "Upload an Excel file or paste" in r.get_data(as_text=True)


def test_preview_rejects_unparseable_text(client):
    r = client.post("/payroll/sync/preview",
                    data={"pasted": "no commas here\nnor here"},
                    follow_redirects=True)
    assert "No valid rows found" in r.get_data(as_text=True)


def test_preview_writes_nothing(client, conn):
    emp = _unique_active_retail(conn)[0]
    client.post("/payroll/sync/preview", data={
        "pasted": _roster({"full_name": emp["full_name"],
                           "store": "Route Test Store"})})
    after = conn.execute("SELECT current_store FROM employees WHERE id=?",
                         (emp["id"],)).fetchone()["current_store"]
    assert after == emp["current_store"]


# --------------------------------------------------------------------------- #
# Apply — the form-field translation
# --------------------------------------------------------------------------- #
def test_apply_moves_an_employee(client, conn):
    """Exercises `move[]` + `new_store_<id>` reaching the service intact."""
    emp = _unique_active_retail(conn)[0]
    try:
        r = client.post("/payroll/sync/apply", data={
            "move[]": [emp["id"]],
            "new_store_{}".format(emp["id"]): "Route Test Store",
        }, follow_redirects=True)
        assert r.status_code == 200

        row = conn.execute("SELECT current_store FROM employees WHERE id=?",
                           (emp["id"],)).fetchone()
        assert row["current_store"] == "Route Test Store"
        open_rows = conn.execute(
            "SELECT store FROM store_history WHERE employee_id=? AND to_date IS NULL",
            (emp["id"],)).fetchall()
        assert [x["store"] for x in open_rows] == ["Route Test Store"]
    finally:
        _restore(conn, emp)


def test_apply_creates_a_new_employee(client, conn):
    """Exercises `add_new[]` + the indexed `add_new_*_<idx>` fields."""
    created = None
    try:
        client.post("/payroll/sync/apply", data={
            "add_new[]": ["0"],
            "add_new_name_0": "Routetest, Person",
            "add_new_store_0": "Riverbend",
            "add_new_title_0": "Ambassador",
        }, follow_redirects=True)
        created = conn.execute(
            "SELECT id, sector, current_store FROM employees "
            "WHERE full_name='Routetest, Person'").fetchone()
        assert created is not None, "add_new[] did not reach the service"
        assert created["sector"] == "retail"
        assert created["current_store"] == "Riverbend"
    finally:
        if created:
            conn.execute("DELETE FROM store_history WHERE employee_id=?",
                         (created["id"],))
            conn.execute("DELETE FROM employees WHERE id=?", (created["id"],))
            conn.commit()


def test_apply_renames_via_fuzzy_link(client, conn):
    """Exercises `fuzzy_link[]` + `fuzzy_name_/store_/title_<id>`."""
    emp = _unique_active_retail(conn)[0]
    try:
        client.post("/payroll/sync/apply", data={
            "fuzzy_link[]": [emp["id"]],
            "fuzzy_name_{}".format(emp["id"]): "Routetest, Renamed",
            "fuzzy_store_{}".format(emp["id"]): "Route Test Store",
            "fuzzy_title_{}".format(emp["id"]): "Ambassador",
        }, follow_redirects=True)
        row = conn.execute("SELECT full_name, current_store FROM employees WHERE id=?",
                           (emp["id"],)).fetchone()
        assert row["full_name"] == "Routetest, Renamed"
        assert row["current_store"] == "Route Test Store"
    finally:
        _restore(conn, emp)


def test_apply_keeps_an_owing_employee_active(client, conn):
    """The guard that protects real money, exercised through the real form.

    Terminating a leaver who still owes silently abandons the debt, so it must take an
    explicit tick — and the route must surface that it declined."""
    totals = db.get_outstanding_totals()
    owing = next((eid for eid, amount in totals.items() if amount > 0), None)
    if owing is None:
        pytest.skip("no employee with an outstanding balance")
    before = dict(conn.execute(
        "SELECT id, full_name, current_store, job_title, status FROM employees "
        "WHERE id=?", (owing,)).fetchone())
    if before["status"] != "active":
        pytest.skip("the owing employee is not active")

    try:
        r = client.post("/payroll/sync/apply", data={"terminate[]": [owing]},
                        follow_redirects=True)
        body = r.get_data(as_text=True)
        status = conn.execute("SELECT status FROM employees WHERE id=?",
                              (owing,)).fetchone()["status"]
        assert status == "active", "an owing employee must not be terminated"
        assert "still" in body and "owe" in body, \
            "the route must report that it declined, not fail silently"
    finally:
        _restore(conn, before)


def test_apply_force_terminate_overrides_the_guard(client, conn):
    totals = db.get_outstanding_totals()
    owing = next((eid for eid, amount in totals.items() if amount > 0), None)
    if owing is None:
        pytest.skip("no employee with an outstanding balance")
    before = dict(conn.execute(
        "SELECT id, full_name, current_store, job_title, status FROM employees "
        "WHERE id=?", (owing,)).fetchone())
    if before["status"] != "active":
        pytest.skip("the owing employee is not active")

    try:
        client.post("/payroll/sync/apply", data={
            "terminate[]": [owing], "force_terminate[]": [owing],
        }, follow_redirects=True)
        status = conn.execute("SELECT status FROM employees WHERE id=?",
                              (owing,)).fetchone()["status"]
        assert status == "terminated"
    finally:
        _restore(conn, before)


def test_apply_adds_a_store(client, conn):
    try:
        client.post("/payroll/sync/apply", data={"add_store[]": ["Route Test Store"]},
                    follow_redirects=True)
        n = conn.execute("SELECT COUNT(*) c FROM stores WHERE name=?",
                         ("Route Test Store",)).fetchone()["c"]
        assert n == 1
    finally:
        conn.execute("DELETE FROM stores WHERE name=?", ("Route Test Store",))
        conn.commit()
        db.invalidate_stores_cache()


def test_apply_with_no_selections_changes_nothing(client, conn):
    before = conn.execute("SELECT COUNT(*) c FROM employees").fetchone()["c"]
    r = client.post("/payroll/sync/apply", data={}, follow_redirects=True)
    assert r.status_code == 200
    assert "no changes selected" in r.get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) c FROM employees").fetchone()["c"] == before


def test_apply_dates_the_move_to_the_payroll_month(client, conn):
    """period_year/period_month must reach the service's effective_date, or a backdated
    roster writes store history stamped today."""
    emp = _unique_active_retail(conn)[0]
    try:
        client.post("/payroll/sync/apply", data={
            "move[]": [emp["id"]],
            "new_store_{}".format(emp["id"]): "Route Test Store",
            "period_year": "2026", "period_month": "3",
        }, follow_redirects=True)
        row = conn.execute(
            "SELECT from_date FROM store_history "
            "WHERE employee_id=? AND store=? ORDER BY id DESC LIMIT 1",
            (emp["id"], "Route Test Store")).fetchone()
        assert row is not None and row["from_date"].startswith("2026-03-01")
    finally:
        _restore(conn, emp)
