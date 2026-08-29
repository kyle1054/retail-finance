"""Tests for the shared payroll-sync service.

This logic used to live inside ``routes_payroll.payroll_sync_preview`` / ``_apply`` and
was therefore only reachable through a Flask form post, which is why it had almost no
coverage despite being the most consequential write in the app — it terminates people
and moves them between stores. Extracting it so BOTH the route and the MCP connector
call one implementation also made it directly testable, and these are the properties
that must hold whichever front door drives it.

The two that matter most:

* an employee who still owes money is NOT terminated without an explicit override, and
* an ambiguous (duplicated) name is never auto-matched.

Both protect a real person from a silent, wrong write.
"""
import pytest

from northwind.data import database as db
from northwind.deductions import payroll_sync as service


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_parse_text_reads_tab_separated_rows():
    roster, rows, skipped = service.parse_text(
        "Smith, John\tRiverbend\tAmbassador\n"
        "Bennett, Nadia\tNorthgate\tStore Manager\n")
    assert rows == 2 and skipped == 0
    assert roster["smith, john"] == {
        "full_name": "Smith, John", "store": "Riverbend", "job_title": "Ambassador"}


def test_parse_text_falls_back_to_multiple_spaces():
    """Pasting out of some spreadsheets gives runs of spaces, not tabs."""
    roster, rows, _ = service.parse_text("Smith, John   Riverbend   Ambassador")
    assert rows == 1
    assert roster["smith, john"]["store"] == "Riverbend"


def test_parse_text_skips_headers_and_malformed_names():
    roster, rows, skipped = service.parse_text(
        "Employee Name\tStore\tTitle\n"          # header: no comma
        "Smith, John\tRiverbend\tAmbassador\n"
        "NoComma Here\tNorthgate\tX\n"             # malformed
        ", Missing\tNorthgate\tX\n"                # empty surname
        "Trailing,\tNorthgate\tX\n")               # empty firstname
    assert rows == 1 and skipped == 4
    assert list(roster) == ["smith, john"]


def test_parse_text_ignores_blank_lines_without_counting_them():
    _, rows, skipped = service.parse_text(
        "Smith, John\tRiverbend\tX\n\n\nBennett, Nadia\tNorthgate\tY\n")
    assert rows == 2 and skipped == 0


@pytest.mark.parametrize("name,ok", [
    ("Smith, John", True), ("Smith,John", True),
    ("Smith", False), ("", False), (None, False),
    ("Smith, John, Jr", False),      # two commas is not the expected shape
    (" , John", False), ("Smith, ", False),
])
def test_valid_payroll_name(name, ok):
    assert service.valid_payroll_name(name) is ok


def test_parse_text_handles_missing_store_and_title():
    roster, rows, _ = service.parse_text("Smith, John")
    assert rows == 1
    assert roster["smith, john"] == {"full_name": "Smith, John", "store": "",
                                     "job_title": ""}


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def _active_retail(conn, n=3):
    rows = conn.execute(
        "SELECT id, full_name, current_store FROM employees "
        "WHERE status='active' AND sector='retail' "
        "GROUP BY LOWER(full_name) HAVING COUNT(*)=1 "
        "ORDER BY id LIMIT ?", (n,)).fetchall()
    if len(rows) < n:
        pytest.skip("not enough uniquely-named active retail employees")
    return [dict(r) for r in rows]


def _roster_line(emp, store=None, title="Ambassador"):
    return "{}\t{}\t{}".format(emp["full_name"], store or emp["current_store"] or "X",
                               title)


def test_resolve_detects_a_store_change(conn, db_copy):
    emp = _active_retail(conn, 1)[0]
    roster, _, _ = service.parse_text(_roster_line(emp, store="A Different Store"))
    out = service.resolve(roster)

    changed = [c for c in out["store_changes"] if c["id"] == emp["id"]]
    assert changed, "a moved employee must appear in store_changes"
    assert changed[0]["new_store"] == "A Different Store"
    assert changed[0]["old_store"] == emp["current_store"]


def test_resolve_reports_absent_staff_as_probable_leavers(conn, db_copy):
    emps = _active_retail(conn, 2)
    # Roster contains only the FIRST employee, so the second is absent.
    roster, _, _ = service.parse_text(_roster_line(emps[0]))
    out = service.resolve(roster)

    absent_ids = {c["id"] for c in out["not_in_payroll"]}
    assert emps[1]["id"] in absent_ids
    assert emps[0]["id"] not in absent_ids


def test_not_in_payroll_carries_the_outstanding_balance(conn, db_copy):
    """The termination guard depends on this number being present and correct."""
    roster, _, _ = service.parse_text("Nobody, Real\tX\tY")
    out = service.resolve(roster)
    totals = db.get_outstanding_totals()
    assert out["not_in_payroll"], "everyone should look absent from this roster"
    for row in out["not_in_payroll"]:
        assert row["outstanding"] == totals.get(row["id"], 0.0)


def test_resolve_reports_unknown_names_as_joiners(conn, db_copy):
    roster, _, _ = service.parse_text("Zzzznonexistent, Person\tRiverbend\tAmbassador")
    out = service.resolve(roster)
    assert any(p["full_name"] == "Zzzznonexistent, Person"
               for p in out["new_in_payroll"])


def test_resolve_fuzzy_matches_a_typo_rather_than_inventing_a_joiner(conn, db_copy):
    """A one-character typo must read as the same person, not as a new hire plus a
    leaver — that pair of wrong decisions is how a staff record gets duplicated."""
    emp = _active_retail(conn, 1)[0]
    typo = emp["full_name"][:-1] + "x" if len(emp["full_name"]) > 4 else None
    if not typo:
        pytest.skip("name too short to perturb")
    roster, _, _ = service.parse_text(
        "{}\t{}\tAmbassador".format(typo, emp["current_store"] or "X"))
    out = service.resolve(roster)

    matched = [f for f in out["fuzzy_matches"] if f["db_id"] == emp["id"]]
    assert matched, "a near-miss name must fuzzy-match the existing employee"
    assert matched[0]["payroll_name"] == typo
    assert 0 < matched[0]["score"] <= 100
    # And it must NOT also be reported as a joiner or a leaver.
    assert not any(p["full_name"] == typo for p in out["new_in_payroll"])
    assert emp["id"] not in {c["id"] for c in out["not_in_payroll"]}


def test_resolve_is_deterministic(conn, db_copy):
    """Fuzzy pairing iterates over sets; unstable ordering would make a preview token
    disagree with its own apply and refuse a legitimate change."""
    emps = _active_retail(conn, 3)
    text = "\n".join(_roster_line(e, store="Somewhere Else") for e in emps)
    roster, _, _ = service.parse_text(text)
    first = service.resolve(roster)
    for _ in range(4):
        assert service.resolve(roster) == first


def test_resolve_never_touches_hq_staff(conn, db_copy):
    """A retail roster must not be able to terminate an HQ employee just because they
    are (correctly) absent from it."""
    hq = conn.execute("SELECT id FROM employees WHERE sector='hq' AND status='active' "
                      "LIMIT 1").fetchone()
    if hq is None:
        pytest.skip("no active HQ employees in the test DB")
    roster, _, _ = service.parse_text("Nobody, Real\tX\tY")
    out = service.resolve(roster)
    assert hq["id"] not in {c["id"] for c in out["not_in_payroll"]}


def test_resolve_flags_new_stores(conn, db_copy):
    emp = _active_retail(conn, 1)[0]
    roster, _, _ = service.parse_text(_roster_line(emp, store="Brand New Store 9x"))
    out = service.resolve(roster)
    assert "Brand New Store 9x" in out["new_stores"]


# --------------------------------------------------------------------------- #
# Application — the guards
# --------------------------------------------------------------------------- #
def test_owing_employee_is_kept_active_without_an_override(conn, db_copy):
    """The guard that matters: closing a leaver who still owes money silently
    abandons the debt."""
    totals = db.get_outstanding_totals()
    owing = next((eid for eid, amount in totals.items() if amount > 0), None)
    if owing is None:
        pytest.skip("no employee with an outstanding balance in the test DB")

    before = conn.execute("SELECT status FROM employees WHERE id=?",
                          (owing,)).fetchone()["status"]
    counts = service.apply_decisions(conn, terminations=[owing], outstanding=totals)
    conn.rollback()

    assert counts["terminated"] == 0
    assert counts["kept_owing"] == 1
    assert counts["kept_owing_ids"] == [owing]
    after = conn.execute("SELECT status FROM employees WHERE id=?",
                         (owing,)).fetchone()["status"]
    assert after == before


def test_force_terminate_overrides_the_owing_guard(conn, db_copy):
    totals = db.get_outstanding_totals()
    owing = next((eid for eid, amount in totals.items() if amount > 0), None)
    if owing is None:
        pytest.skip("no employee with an outstanding balance in the test DB")

    counts = service.apply_decisions(conn, terminations=[owing],
                                     force_terminate=[owing], outstanding=totals)
    status = conn.execute("SELECT status FROM employees WHERE id=?",
                          (owing,)).fetchone()["status"]
    conn.rollback()

    assert counts["terminated"] == 1 and counts["kept_owing"] == 0
    assert status == "terminated"


def test_a_move_writes_store_history_not_just_the_column(conn, db_copy):
    """A store move is a history event — a report of where someone worked in March
    depends on the old row being closed and a new one opened."""
    emp = _active_retail(conn, 1)[0]
    service.apply_decisions(conn, moves=[{"id": emp["id"], "new_store": "Testville"}],
                            effective="2026-03-01T00:00:00")

    current = conn.execute("SELECT current_store FROM employees WHERE id=?",
                           (emp["id"],)).fetchone()["current_store"]
    open_rows = conn.execute(
        "SELECT store, from_date FROM store_history "
        "WHERE employee_id=? AND to_date IS NULL", (emp["id"],)).fetchall()
    closed = conn.execute(
        "SELECT COUNT(*) c FROM store_history "
        "WHERE employee_id=? AND to_date=?",
        (emp["id"], "2026-03-01T00:00:00")).fetchone()["c"]
    conn.rollback()

    assert current == "Testville"
    assert len(open_rows) == 1 and open_rows[0]["store"] == "Testville"
    assert closed >= 1, "the previous store_history row must be closed off"


def test_a_move_with_no_target_store_is_skipped(conn, db_copy):
    emp = _active_retail(conn, 1)[0]
    counts = service.apply_decisions(conn, moves=[{"id": emp["id"], "new_store": "  "}])
    conn.rollback()
    assert counts["moved"] == 0


def test_addition_creates_a_retail_employee_with_history(conn, db_copy):
    counts = service.apply_decisions(conn, additions=[
        {"full_name": "Testperson, New", "store": "Riverbend", "job_title": "Ambassador"}])
    row = conn.execute(
        "SELECT id, sector, status, current_store FROM employees "
        "WHERE full_name='Testperson, New'").fetchone()
    history = conn.execute(
        "SELECT COUNT(*) c FROM store_history WHERE employee_id=?",
        (row["id"],)).fetchone()["c"] if row else 0
    conn.rollback()

    assert counts["added"] == 1
    assert row is not None and row["sector"] == "retail"
    assert row["current_store"] == "Riverbend"
    assert history == 1


def test_addition_without_a_name_is_skipped(conn, db_copy):
    counts = service.apply_decisions(conn, additions=[{"full_name": "  ", "store": "X"}])
    conn.rollback()
    assert counts["added"] == 0


def test_fuzzy_link_renames_in_place_rather_than_creating(conn, db_copy):
    emp = _active_retail(conn, 1)[0]
    before_count = conn.execute(
        "SELECT COUNT(*) c FROM employees").fetchone()["c"]
    counts = service.apply_decisions(conn, fuzzy_links=[
        {"id": emp["id"], "full_name": "Corrected, Name",
         "store": emp["current_store"] or "Riverbend", "job_title": "Ambassador"}])
    after = conn.execute("SELECT full_name FROM employees WHERE id=?",
                         (emp["id"],)).fetchone()["full_name"]
    after_count = conn.execute("SELECT COUNT(*) c FROM employees").fetchone()["c"]
    conn.rollback()

    assert counts["linked"] == 1
    assert after == "Corrected, Name"
    assert after_count == before_count, "a rename must not create a second record"


def test_new_store_insert_is_idempotent(conn, db_copy):
    existing = conn.execute("SELECT name FROM stores LIMIT 1").fetchone()
    if existing is None:
        pytest.skip("no stores in the test DB")
    service.apply_decisions(conn, new_stores=[existing["name"]])
    n = conn.execute("SELECT COUNT(*) c FROM stores WHERE name=?",
                     (existing["name"],)).fetchone()["c"]
    conn.rollback()
    assert n == 1


def test_apply_decisions_does_not_commit(conn, db_copy):
    """The caller owns the transaction — that is what lets a preview dry-run it."""
    emp = _active_retail(conn, 1)[0]
    service.apply_decisions(conn, moves=[{"id": emp["id"], "new_store": "Rollback Town"}])
    conn.rollback()
    after = conn.execute("SELECT current_store FROM employees WHERE id=?",
                         (emp["id"],)).fetchone()["current_store"]
    assert after != "Rollback Town"


def test_empty_apply_is_a_no_op(conn, db_copy):
    counts = service.apply_decisions(conn)
    assert counts == {"moved": 0, "terminated": 0, "added": 0, "linked": 0,
                      "kept_owing": 0, "stores_added": 0, "kept_owing_ids": []}


# --------------------------------------------------------------------------- #
# Effective dating
# --------------------------------------------------------------------------- #
def test_effective_date_uses_the_payroll_month_when_given():
    assert service.effective_date(2026, 3).startswith("2026-03-01")


@pytest.mark.parametrize("y,m", [(None, None), (2026, None), (2026, 0), (2026, 13)])
def test_effective_date_falls_back_to_now(y, m):
    from datetime import datetime
    got = service.effective_date(y, m)
    assert got.startswith(str(datetime.now().year))
