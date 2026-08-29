#!/usr/bin/env python3
"""Data-integrity checker for the NORTHWIND database — catch drift before it spreads.

Why this exists
---------------
As the app grows, data can quietly get inconsistent: an employee is deleted but
their ledger rows linger, a plan is removed while its transactions remain, a cash
entry names a store that no longer exists. The CC tables have real ``ON DELETE``
foreign keys, but the **core ledger** tables (``deduction_transactions_cents``,
``plan_adjustments_cents``, ``overpayments_cents``, ``layby_items_cents``) link by
convention with NO declared FK — so orphans there accumulate silently. This walks
every relationship (declared *and* conventional) plus a handful of money/state
invariants and reports what's wrong.

Usage
-----
    python tools/db_check.py                  # check deductions.db, human report
    python tools/db_check.py --db path.db
    python tools/db_check.py --json           # machine-readable

Exit code is 0 when clean, 1 when any issue is found — so it works in CI and as a
pre-deploy gate. Reads only; never mutates. Stdlib only.

The check catalogue is data-driven (see ``run_checks``) so adding a new invariant
is a few lines, not a new function.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.environ.get('NW_DB_PATH') or os.path.join(HERE, '..', 'db', 'deductions.db')

PLAN_TYPE_TABLE = {
    "uniform": "uniform_deductions_cents",
    "layby": "layby_deductions_cents",
    "undercharge": "undercharges_cents",
}

# Conventional (undeclared) relationships to check for orphans.
# (child_table, child_col, parent_table, parent_col, allow_null)
# parent_table "<by plan_type>" => resolve via the child row's plan_type column.
CONVENTIONAL_FKS = [
    ("deduction_transactions_cents", "employee_id", "employees", "id", False),
    ("deduction_transactions_cents", "plan_id", "<by plan_type>", "id", False),
    ("plan_adjustments_cents", "plan_id", "<by plan_type>", "id", False),
    ("overpayments_cents", "employee_id", "employees", "id", True),
    ("layby_items_cents", "layby_id", "layby_deductions_cents", "id", False),
    ("cash_recon_entries", "store", "stores", "name", False),
    ("cash_recon_opening", "store", "stores", "name", False),
    ("store_emails", "store", "stores", "name", False),
    ("rm_stores", "store", "stores", "name", False),
    ("cc_receipts", "statement_id", "cc_statements", "id", True),
]


class Issue:
    __slots__ = ("severity", "check", "detail", "count", "sample")

    def __init__(self, severity, check, detail, count=None, sample=None):
        self.severity = severity      # "error" | "warn"
        self.check = check
        self.detail = detail
        self.count = count
        self.sample = sample or []

    def as_dict(self):
        return {"severity": self.severity, "check": self.check,
                "detail": self.detail, "count": self.count, "sample": self.sample}


def _has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _cols(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _check_pragma_integrity(conn, issues):
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    if rows and rows[0][0] != "ok":
        issues.append(Issue("error", "integrity_check",
                            "SQLite reported corruption",
                            count=len(rows), sample=[r[0] for r in rows[:5]]))


def _check_declared_fks(conn, issues):
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    if not rows:
        return
    # rows: (table, rowid, referred_table, fk_index)
    by_table = {}
    for tbl, rowid, ref, _idx in rows:
        by_table.setdefault((tbl, ref), []).append(rowid)
    for (tbl, ref), rowids in by_table.items():
        issues.append(Issue(
            "error", "declared-fk",
            f"{tbl}: {len(rowids)} row(s) violate the declared FK to {ref}",
            count=len(rowids), sample=[f"rowid {r}" for r in rowids[:5]]))


def _orphans(conn, child, ccol, parent, pcol, allow_null):
    """Return (count, sample-of-offending-values) for child rows whose ccol has
    no matching parent.pcol. When allow_null the reference is optional and NULL
    keys are skipped; otherwise a NULL key is itself counted as an issue."""
    null_guard = f'AND c."{ccol}" IS NOT NULL' if allow_null else ""
    sql = (
        f'SELECT c."{ccol}", COUNT(*) FROM "{child}" c '
        f'LEFT JOIN "{parent}" p ON c."{ccol}" = p."{pcol}" '
        f'WHERE p."{pcol}" IS NULL {null_guard} '
        f'GROUP BY c."{ccol}"'
    )
    rows = conn.execute(sql).fetchall()
    total = sum(r[1] for r in rows)
    sample = [f'{ccol}={r[0]!r} ({r[1]})' for r in rows[:5]]
    return total, sample


def _check_conventional_fks(conn, issues):
    for child, ccol, parent, pcol, allow_null in CONVENTIONAL_FKS:
        if not _has_table(conn, child):
            continue
        if parent == "<by plan_type>":
            # Polymorphic: split by plan_type, check each against its own table.
            for ptype, ptable in PLAN_TYPE_TABLE.items():
                if not _has_table(conn, ptable):
                    continue
                sql = (
                    f'SELECT c."{ccol}", COUNT(*) FROM "{child}" c '
                    f'LEFT JOIN "{ptable}" p ON c."{ccol}" = p."{pcol}" '
                    f'WHERE c.plan_type = ? AND p."{pcol}" IS NULL '
                    f'AND c."{ccol}" IS NOT NULL GROUP BY c."{ccol}"'
                )
                rows = conn.execute(sql, (ptype,)).fetchall()
                total = sum(r[1] for r in rows)
                if total:
                    issues.append(Issue(
                        "error", "orphan",
                        f"{child}.{ccol} (plan_type={ptype}): {total} row(s) point to a missing {ptable}",
                        count=total, sample=[f'plan_id={r[0]} ({r[1]})' for r in rows[:5]]))
            # Unknown plan_type values
            bad = conn.execute(
                f'SELECT DISTINCT plan_type FROM "{child}" '
                f'WHERE plan_type NOT IN (?,?,?)',
                tuple(PLAN_TYPE_TABLE)).fetchall()
            if bad:
                issues.append(Issue(
                    "error", "bad-enum",
                    f"{child}.plan_type has unrecognised value(s)",
                    count=len(bad), sample=[str(r[0]) for r in bad[:5]]))
            continue

        if not _has_table(conn, parent):
            continue
        total, sample = _orphans(conn, child, ccol, parent, pcol, allow_null)
        if total:
            issues.append(Issue(
                "error", "orphan",
                f"{child}.{ccol}: {total} row(s) point to a missing {parent}.{pcol}",
                count=total, sample=sample))


def _check_money_and_state(conn, issues):
    # Plan totals / monthly should never be negative.
    for ptable in PLAN_TYPE_TABLE.values():
        if not _has_table(conn, ptable):
            continue
        cols = _cols(conn, ptable)
        for col in ("total_amount_cents", "monthly_amount_cents"):
            if col in cols:
                n = conn.execute(
                    f'SELECT COUNT(*) FROM "{ptable}" WHERE "{col}" < 0').fetchone()[0]
                if n:
                    issues.append(Issue("error", "negative-money",
                                        f"{ptable}.{col} negative in {n} row(s)", count=n))
        # payments_made must not exceed the term.
        if {"payments_made", "term_months"} <= cols:
            n = conn.execute(
                f'SELECT COUNT(*) FROM "{ptable}" '
                f'WHERE payments_made > term_months AND term_months > 0').fetchone()[0]
            if n:
                issues.append(Issue("warn", "over-paid",
                                    f"{ptable}: payments_made > term_months in {n} row(s)", count=n))

    # A 'split' undercharge must carry a usable split_months — the tick and
    # summary paths divide by it, so a NULL/non-positive value would crash a
    # payroll tick (the table CHECK only rejects 0/negative, not NULL).
    if _has_table(conn, "undercharges_cents"):
        n = conn.execute(
            "SELECT COUNT(*) FROM undercharges_cents "
            "WHERE recovery_method = 'split' "
            "AND (split_months IS NULL OR split_months <= 0)").fetchone()[0]
        if n:
            issues.append(Issue("error", "bad-split",
                                f"undercharges_cents: 'split' rows with NULL/non-positive split_months: {n} row(s)",
                                count=n))

    # deduction_transactions_cents.voided should be 0/1 only.
    if _has_table(conn, "deduction_transactions_cents"):
        n = conn.execute(
            "SELECT COUNT(*) FROM deduction_transactions_cents "
            "WHERE voided NOT IN (0,1)").fetchone()[0]
        if n:
            issues.append(Issue("error", "bad-enum",
                                f"deduction_transactions_cents.voided not in (0,1): {n} row(s)", count=n))

    # Unified identity: a user_roles row must point at a live users row (declared FK
    # covers deletes, but check active-user sanity too). And no duplicate logins.
    if _has_table(conn, "users"):
        dup = conn.execute(
            "SELECT login, COUNT(*) c FROM users GROUP BY LOWER(login) HAVING c > 1"
        ).fetchall()
        if dup:
            issues.append(Issue("error", "duplicate-login",
                                f"{len(dup)} login(s) duplicated in users (case-insensitive)",
                                count=len(dup), sample=[f'{r[0]} ({r[1]})' for r in dup[:5]]))


def _check_cc_line_duplicates(conn, issues):
    """A credit-card transaction (card + fingerprint + occurrence) must live in
    exactly ONE statement. The same txn in two statements is a duplicate — it
    double-counts on the card tile and receipt totals. (Legacy artifact from
    before carryover-bucketing; the importer no longer creates these, so any hit
    here means stale data to clean up.)"""
    if not _has_table(conn, "cc_lines"):
        return
    rows = conn.execute(
        "SELECT card_id, fingerprint, occurrence, COUNT(DISTINCT statement_id) s "
        "FROM cc_lines WHERE fingerprint IS NOT NULL "
        "GROUP BY card_id, fingerprint, occurrence HAVING s > 1").fetchall()
    if rows:
        issues.append(Issue(
            "error", "cc-duplicate-line",
            f"{len(rows)} credit-card txn(s) appear in more than one statement (duplicated)",
            count=len(rows),
            sample=[f"card {r[0]} fp={r[1]}" for r in rows[:5]]))


def _check_cc_legacy_line_links(conn, issues):
    """The legacy cc_receipts.line_id writer is retired; all links belong in
    cc_receipt_lines. A non-NULL value can cascade-delete the receipt when a
    statement line is purged, so flag it before that data loss can occur."""
    if not _has_table(conn, "cc_receipts") or "line_id" not in _cols(conn, "cc_receipts"):
        return
    rows = conn.execute(
        "SELECT id, line_id FROM cc_receipts WHERE line_id IS NOT NULL LIMIT 6"
    ).fetchall()
    if rows:
        issues.append(Issue(
            "error", "cc-legacy-line-link",
            "cc_receipts.line_id must stay NULL; use cc_receipt_lines for links",
            count=conn.execute(
                "SELECT COUNT(*) FROM cc_receipts WHERE line_id IS NOT NULL"
            ).fetchone()[0],
            sample=[f"receipt {r[0]} -> line {r[1]}" for r in rows[:5]]))


def _check_cc_receipt_link_scope(conn, issues):
    """Every receipt link must stay inside one card statement (migration 0045)."""
    if not all(_has_table(conn, name) for name in
               ("cc_receipt_lines", "cc_receipts", "cc_lines")):
        return
    rows = conn.execute(
        "SELECT rl.receipt_id, rl.line_id, r.card_id, r.statement_id, "
        "l.card_id, l.statement_id FROM cc_receipt_lines rl "
        "JOIN cc_receipts r ON r.id=rl.receipt_id "
        "JOIN cc_lines l ON l.id=rl.line_id "
        "WHERE r.card_id!=l.card_id OR r.statement_id IS NULL "
        "OR r.statement_id!=l.statement_id LIMIT 6").fetchall()
    if rows:
        issues.append(Issue(
            "error", "cc-receipt-link-scope",
            "cc_receipt_lines contains cross-card or cross-statement links",
            count=conn.execute(
                "SELECT COUNT(*) FROM cc_receipt_lines rl "
                "JOIN cc_receipts r ON r.id=rl.receipt_id "
                "JOIN cc_lines l ON l.id=rl.line_id "
                "WHERE r.card_id!=l.card_id OR r.statement_id IS NULL "
                "OR r.statement_id!=l.statement_id").fetchone()[0],
            sample=[f"receipt {r[0]} -> line {r[1]}" for r in rows[:5]]))


def _check_undercharge_timeline(conn, issues):
    """Schedule items must agree exactly with their linked payroll ledger row."""
    if not (_has_table(conn, "undercharge_schedule_items")
            and _has_table(conn, "deduction_transactions_cents")):
        return
    bad = conn.execute(
        "SELECT i.id,i.undercharge_id,i.due_year,i.due_month,i.amount_cents,t.id "
        "FROM undercharge_schedule_items i "
        "JOIN deduction_transactions_cents t ON t.id=i.transaction_id "
        "WHERE t.plan_type!='undercharge' OR t.plan_id!=i.undercharge_id "
        "OR t.year!=i.due_year OR t.month!=i.due_month "
        "OR t.amount_cents!=i.amount_cents LIMIT 6").fetchall()
    if bad:
        issues.append(Issue(
            "error", "undercharge-schedule-ledger",
            "Undercharge schedule item(s) disagree with their linked payroll transaction",
            count=len(bad), sample=[f"item {r[0]} -> transaction {r[5]}" for r in bad[:5]]))

    unlinked = conn.execute(
        "SELECT t.id,t.plan_id FROM deduction_transactions_cents t "
        "LEFT JOIN undercharge_schedule_items i ON i.transaction_id=t.id "
        "WHERE t.plan_type='undercharge' AND COALESCE(t.voided,0)=0 "
        "AND i.id IS NULL LIMIT 6").fetchall()
    if unlinked:
        issues.append(Issue(
            "error", "undercharge-unlinked-ledger",
            "Non-voided undercharge payroll transaction(s) have no timeline item",
            count=len(unlinked),
            sample=[f"transaction {r[0]} plan {r[1]}" for r in unlinked[:5]]))

    if {"legacy_payments_count", "payments_made"} <= _cols(conn, "undercharges_cents"):
        mismatch = conn.execute(
            "SELECT u.id FROM undercharges_cents u WHERE u.payments_made != "
            "u.legacy_payments_count + (SELECT COUNT(*) "
            "FROM deduction_transactions_cents t "
            "WHERE t.plan_type='undercharge' AND t.plan_id=u.id "
            "AND t.amount_cents>0 AND COALESCE(t.voided,0)=0) LIMIT 6").fetchall()
        if mismatch:
            issues.append(Issue(
                "error", "undercharge-payment-count",
                "Undercharge payment count disagrees with ledger + legacy carryover",
                count=len(mismatch), sample=[f"plan {r[0]}" for r in mismatch[:5]]))


def run_checks(conn):
    """Run every check against an open connection. Returns a list of Issue."""
    issues = []
    _check_pragma_integrity(conn, issues)
    _check_declared_fks(conn, issues)
    _check_conventional_fks(conn, issues)
    _check_money_and_state(conn, issues)
    _check_cc_line_duplicates(conn, issues)
    _check_cc_legacy_line_links(conn, issues)
    _check_cc_receipt_link_scope(conn, issues)
    _check_undercharge_timeline(conn, issues)
    return issues


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    try:
        issues = run_checks(conn)
    finally:
        conn.close()

    if args.json:
        print(json.dumps([i.as_dict() for i in issues], indent=2))
        return 1 if any(i.severity == "error" for i in issues) else 0

    if not issues:
        print(f"✓ {os.path.relpath(args.db)}: no integrity issues found.")
        return 0

    errors = [i for i in issues if i.severity == "error"]
    warns = [i for i in issues if i.severity == "warn"]
    print(f"Checked {os.path.relpath(args.db)} — "
          f"{len(errors)} error(s), {len(warns)} warning(s):\n")
    for i in errors + warns:
        mark = "✗" if i.severity == "error" else "⚠"
        print(f"{mark} [{i.check}] {i.detail}")
        for s in i.sample:
            print(f"      e.g. {s}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
