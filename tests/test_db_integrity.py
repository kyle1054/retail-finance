"""Guard rails for schema legibility + data integrity (tools/).

Two tools keep the growing DB from losing context:
  * tools/schema_map.py — regenerates SCHEMA.md from the live DB (can't drift).
  * tools/db_check.py   — flags orphans / broken invariants across every relationship.

These tests run the checker against the test DB copy (so CI fails if live data has
drifted) and prove the checker actually detects an injected orphan (so a green run
means something).
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import db_check       # noqa: E402
import schema_map     # noqa: E402


def test_live_data_has_no_integrity_errors(db_copy):
    """The seeded data must have no orphan/invariant errors."""
    conn = sqlite3.connect(db_copy)
    try:
        issues = db_check.run_checks(conn)
    finally:
        conn.close()
    errors = [i for i in issues if i.severity == "error"]
    assert not errors, "Integrity errors in live data:\n" + "\n".join(
        f"  [{i.check}] {i.detail}" for i in errors)


def test_checker_detects_injected_orphan(db_copy, tmp_path):
    """A checker that only ever says 'clean' is worthless — prove it bites."""
    probe = str(tmp_path / "probe.db")
    import shutil
    shutil.copy(db_copy, probe)
    conn = sqlite3.connect(probe)
    conn.execute(
        "INSERT INTO deduction_transactions_cents "
        "(plan_type, plan_id, employee_id, amount_cents, year, month, voided) "
        "VALUES ('uniform', 999999, 'GHOST_NO_SUCH_EMP', 5000, 2026, 7, 0)")
    conn.commit()
    issues = db_check.run_checks(conn)
    conn.close()
    orphans = [i for i in issues if i.check == "orphan"]
    assert orphans, "checker failed to flag an injected orphan transaction"


def test_checker_detects_cross_statement_duplicate(db_copy, tmp_path):
    """Prove the cc-duplicate-line check bites: the same txn in two statements."""
    probe = str(tmp_path / "probe_dup.db")
    import shutil
    shutil.copy(db_copy, probe)
    conn = sqlite3.connect(probe)
    row = conn.execute(
        "SELECT card_id, statement_id, fingerprint, occurrence FROM cc_lines "
        "WHERE fingerprint IS NOT NULL LIMIT 1").fetchone()
    if row is None:
        conn.close()
        return  # no CC data in this DB copy — nothing to probe
    card_id, stmt_id, fp, occ = row
    other = conn.execute(
        "SELECT id FROM cc_statements WHERE card_id=? AND id<>? LIMIT 1",
        (card_id, stmt_id)).fetchone()
    if other is None:
        other_id = conn.execute(
            "INSERT INTO cc_statements (card_id, year, month) VALUES (?, 2099, 1)",
            (card_id,)).lastrowid
    else:
        other_id = other[0]
    # Same (card, fingerprint, occurrence) in a DIFFERENT statement = duplicate.
    conn.execute(
        "INSERT INTO cc_lines (statement_id, card_id, reference, amount_cents, "
        "category, fingerprint, occurrence, status) "
        "VALUES (?,?,?,?,?,?,?,'outstanding')",
        (other_id, card_id, 'DUP PROBE', -100, 'spend', fp, occ))
    conn.commit()
    issues = db_check.run_checks(conn)
    conn.close()
    assert [i for i in issues if i.check == "cc-duplicate-line"], \
        "checker failed to flag a cross-statement duplicate line"


def test_checker_detects_legacy_receipt_line_link(db_copy, tmp_path):
    """The retired cc_receipts.line_id column must never be re-armed."""
    probe = str(tmp_path / "probe_legacy_receipt.db")
    import shutil
    shutil.copy(db_copy, probe)
    conn = sqlite3.connect(probe)
    row = conn.execute(
        "SELECT r.id, l.id FROM cc_receipts r JOIN cc_lines l "
        "ON l.card_id=r.card_id LIMIT 1").fetchone()
    if row is None:
        conn.close()
        return
    conn.execute("UPDATE cc_receipts SET line_id=? WHERE id=?", (row[1], row[0]))
    conn.commit()
    issues = db_check.run_checks(conn)
    conn.close()
    assert [i for i in issues if i.check == "cc-legacy-line-link"]


def test_checker_detects_cross_scope_receipt_link(db_copy, tmp_path):
    """The audit still detects corruption even if its prevention trigger is lost."""
    probe = str(tmp_path / "probe_receipt_scope.db")
    import shutil
    shutil.copy(db_copy, probe)
    conn = sqlite3.connect(probe)
    pair = conn.execute(
        "SELECT r.id, l.id FROM cc_receipts r JOIN cc_lines l "
        "ON l.card_id!=r.card_id LIMIT 1").fetchone()
    if pair is None:
        conn.close()
        return
    conn.execute("DROP TRIGGER IF EXISTS cc_receipt_lines_scope_insert")
    conn.execute(
        "INSERT INTO cc_receipt_lines (receipt_id, line_id) VALUES (?, ?)", pair)
    conn.commit()
    issues = db_check.run_checks(conn)
    conn.close()
    assert [i for i in issues if i.check == "cc-receipt-link-scope"]


def test_schema_map_is_current(db_copy):
    """SCHEMA.md must match the live schema — regenerate with tools/schema_map.py.

    Uses the test DB copy (which has all migrations applied) as the source of truth,
    so this fails if someone changed the schema without regenerating the doc.
    """
    conn = sqlite3.connect(db_copy)
    try:
        generated = schema_map.build_markdown(conn)
    finally:
        conn.close()
    out = os.path.join(ROOT, "SCHEMA.md")
    assert os.path.exists(out), "SCHEMA.md missing — run `python tools/schema_map.py`"
    with open(out, encoding="utf-8") as fh:
        on_disk = fh.read()
    # Compare structure, ignoring the volatile row-count lines (data changes daily).
    def strip_counts(text):
        return "\n".join(l for l in text.splitlines() if not l.strip().startswith("_") or "rows_" not in l)
    assert strip_counts(on_disk).strip() == strip_counts(generated).strip(), (
        "SCHEMA.md is out of date — run `python tools/schema_map.py`")
