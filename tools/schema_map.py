#!/usr/bin/env python3
"""Generate an always-current schema reference from a live database.

Why this exists
---------------
As the app grows it gets hard to hold the whole DB in your head — which tables
exist, how they relate, and which is the source of truth (the ``*_cents`` tables
vs their compatibility views, ``users`` vs the frozen ``admin_users``/``cc_users``).
A hand-maintained diagram drifts the moment you forget to update it, which is
exactly *how* context gets lost. This reads the database and writes
``SCHEMA.md`` so the reference can never drift — regenerate it after any schema
change and the map is correct by construction.

Usage
-----
    python tools/schema_map.py                 # writes SCHEMA.md from deductions.db
    python tools/schema_map.py --db path.db    # point at another DB
    python tools/schema_map.py --out FILE.md   # write elsewhere
    python tools/schema_map.py --check         # exit 1 if SCHEMA.md is stale (for CI)

Stdlib only — no deps, safe to run against the live DB (reads only).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.environ.get('NW_DB_PATH') or os.path.join(HERE, '..', 'db', 'deductions.db')
DEFAULT_OUT = os.path.join(HERE, '..', 'SCHEMA.md')

# --- Domain grouping ---------------------------------------------------------
# Tables are bucketed so the map reads by feature area, not alphabetically.
# A table not listed here falls into "Other" (which flags newcomers to triage).
DOMAINS = [
    ("Auth & identity", [
        "users", "user_roles", "admin_users", "cc_users",
        "store_emails", "employee_logins",
        "rm_users", "rm_stores",
    ]),
    ("People & stores", ["employees", "stores", "store_history"]),
    ("Deductions — plans (cents = source of truth)", [
        "uniform_deductions_cents", "layby_deductions_cents",
        "layby_items_cents", "undercharges_cents",
    ]),
    ("Deductions — ledger, adjustments & period locking", [
        "deduction_transactions_cents", "plan_adjustments_cents",
        "overpayments_cents", "locked_periods", "undercharge_events",
        "undercharge_schedule_revisions", "undercharge_schedule_items",
    ]),
    ("HQ allowances", ["allowances", "allowance_purchases"]),
    ("Staff requests (portal ask → admin queue)", [
        "staff_requests", "staff_request_items", "staff_request_events",
    ]),
    ("Credit-card reconciliation", [
        "cc_cards", "cc_card_users", "cc_statements", "cc_lines",
        "cc_receipts", "cc_receipt_lines", "cc_line_receipt_suggestions",
        "cc_merchant_map",
    ]),
    ("Cash reconciliation", [
        "cash_recon_entries", "cash_recon_opening", "recon_categories",
        "cash_sales_variance_reasons", "cash_shopify_uploads",
        "cash_shopify_rows", "cash_shopify_store_mappings",
    ]),
    ("System", ["schema_migrations", "app_settings", "sqlite_sequence"]),
]

# --- Undeclared (conventional) relationships ---------------------------------
# The core ledger tables reference plans/employees/stores by convention but carry
# NO declared FK, so they never show up in PRAGMA foreign_key_list. List them
# here so the ERD and db_check both know about them. Format:
#   (child_table, child_col, parent_table, parent_col, note)
# A parent_table of "<by plan_type>" means the target is polymorphic — resolved
# at check time from the row's plan_type. Keep this list in sync with db_check.py.
CONVENTIONAL_FKS = [
    ("deduction_transactions_cents", "employee_id", "employees", "id", ""),
    ("deduction_transactions_cents", "plan_id", "<by plan_type>", "id", "uniform/layby/undercharge"),
    ("plan_adjustments_cents", "plan_id", "<by plan_type>", "id", "uniform/layby/undercharge"),
    ("overpayments_cents", "employee_id", "employees", "id", "nullable — walk-in names use individual_name"),
    ("layby_items_cents", "layby_id", "layby_deductions_cents", "id", ""),
    ("cash_recon_entries", "store", "stores", "name", "store referenced by name"),
    ("cash_recon_opening", "store", "stores", "name", "store referenced by name"),
    ("store_emails", "store", "stores", "name", "store referenced by name"),
    ("rm_stores", "store", "stores", "name", "store referenced by name"),
    ("cc_receipts", "statement_id", "cc_statements", "id", "nullable — NULL = drop-off inbox receipt"),
]

PLAN_TYPE_TABLE = {
    "uniform": "uniform_deductions_cents",
    "layby": "layby_deductions_cents",
    "undercharge": "undercharges_cents",
}


def _rows(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def _objects(conn, kind):
    return [r[0] for r in _rows(
        conn,
        "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%' ORDER BY name",
        (kind,),
    )]


def _table_info(conn, name):
    return _rows(conn, f'PRAGMA table_info("{name}")')


def _fk_list(conn, name):
    return _rows(conn, f'PRAGMA foreign_key_list("{name}")')


def _index_list(conn, name):
    return _rows(conn, f'PRAGMA index_list("{name}")')


def _index_cols(conn, index):
    # r[2] is NULL for expression/rowid index columns — show a placeholder.
    return [(r[2] if r[2] is not None else "<expr>")
            for r in _rows(conn, f'PRAGMA index_info("{index}")')]


def _index_predicate(conn, index):
    """The `WHERE ...` clause of a partial index, or ''.

    PRAGMA index_list/index_info do not expose it, and leaving it out would
    misreport the app's most important constraint: the unique index that makes a
    second deduction for the same plan-month impossible is unique *only* over
    non-voided rows.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index,)
    ).fetchone()
    sql = " ".join((row[0] or "").split()) if row and row[0] else ""
    head, sep, tail = sql.partition(" WHERE ")
    return ("WHERE " + tail) if sep else ""


def _row_count(conn, name):
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    except sqlite3.Error:
        return "?"


def _view_sql(conn, name):
    r = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (name,)
    ).fetchone()
    return (r[0] or "").strip() if r else ""


def _anchor(title):
    """GitHub-style heading anchor: lower-cased, punctuation dropped, spaces
    hyphenated."""
    keep = [c for c in title.lower() if c.isalnum() or c in " -_"]
    return "-".join("".join(keep).split())


def build_markdown(conn):
    tables = set(_objects(conn, "table"))
    views = _objects(conn, "view")
    triggers = _objects(conn, "trigger")

    out = []
    out.append("# Database schema (generated)\n")
    out.append(
        "> **Do not hand-edit — this file is generated from a live database.** "
        "Regenerate it with `python tools/schema_map.py` against a demo-seeded "
        "database (`python scripts/seed_demo.py --reset`), which is what the "
        "checked-in copy is built from — the row counts below are part of what "
        "`--check` compares, so an empty database reports it as stale. "
        "(`--db` points it at another database.) "
        "Money lives in the `*_cents` tables — integer cents, and the source of "
        "truth; the un-suffixed names are Rand **views** over them. Write to the "
        "table, read from either.\n"
    )
    out.append(
        f"{len(tables)} tables, {len(views)} views, {len(triggers)} triggers "
        "(SQLite's own `sqlite_sequence` bookkeeping table is not counted). "
        "Tables are grouped by feature area; a table missing from a group would "
        "appear under \"Other\", so an empty \"Other\" section is the grouping "
        "staying complete.\n"
    )

    # Contents
    out.append("## Sections\n")
    for title, _ in DOMAINS:
        out.append(f"- [{title}](#{_anchor(title)})")
    out.append("- [Relationship map (ERD)](#relationship-map-erd)")
    out.append("- [Cents tables and their Rand views](#cents-tables-and-their-rand-views)")
    out.append("- [Views](#views)")
    out.append("- [Triggers](#triggers)\n")

    grouped = set()
    for title, names in DOMAINS:
        present = [n for n in names if n in tables]
        if not present:
            continue
        grouped.update(present)
        out.append(f"## {title}\n")
        for name in present:
            out.append(_render_table(conn, name))

    # Any table not in a declared domain
    ungrouped = sorted(tables - grouped)
    if ungrouped:
        out.append("## Other (ungrouped — add to DOMAINS in tools/schema_map.py)\n")
        for name in ungrouped:
            out.append(_render_table(conn, name))

    out.append(_render_erd(conn, tables))
    out.append(_render_cents_pairs(tables, views))
    out.append(_render_views(conn, views))
    out.append(_render_triggers(conn, triggers))
    return "\n".join(out).rstrip() + "\n"


def _render_cents_pairs(tables, views):
    """The base-table / compatibility-view pairs, spelled out.

    This is the one thing about the schema you cannot afford to get wrong, so it
    gets its own section rather than being left implicit in the naming.
    """
    lines = ["## Cents tables and their Rand views\n",
             "Every money column is integer cents on the base table. The view of "
             "the same name (minus the suffix) divides by 100, so existing reads "
             "— including `SELECT *` and `SUM()` — kept working unchanged when "
             "the conversion happened. **Writes must go to the `_cents` table**: "
             "these views are not writable, and a Rand float is not a value this "
             "schema stores.\n",
             "| Write here (integer cents) | Read-only Rand view |",
             "|---|---|"]
    viewset = set(views)
    paired = 0
    for t in sorted(tables):
        if not t.endswith("_cents"):
            continue
        base = t[: -len("_cents")]
        view = f"`{base}`" if base in viewset else "_(no view)_"
        if base in viewset:
            paired += 1
        lines.append(f"| `{t}` | {view} |")
    orphan_views = sorted(v for v in viewset if f"{v}_cents" not in tables)
    if orphan_views:
        lines.append("")
        lines.append("_Views with no `_cents` table of the same name: "
                     + ", ".join(f"`{v}`" for v in orphan_views) + "._")
    lines.append("")
    return "\n".join(lines)


def _render_triggers(conn, triggers):
    """Triggers, verbatim. Several of them are integrity rules that exist
    nowhere else — the DB refusing a write the application layer might miss."""
    if not triggers:
        return ""
    lines = ["## Triggers\n",
             "Database-enforced rules. Two of these (`RAISE(ABORT, ...)`) are "
             "constraints SQLite cannot express as a CHECK; the rest keep "
             "denormalised or dependent rows consistent on write and delete.\n"]
    for name in triggers:
        row = conn.execute(
            "SELECT tbl_name, sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (name,)).fetchone()
        tbl = row[0] if row else "?"
        sql = (row[1] or "").strip() if row else ""
        lines.append(f"### `{name}`\n")
        lines.append(f"On `{tbl}`.\n")
        lines.append("```sql")
        lines.append(sql)
        lines.append("```\n")
    return "\n".join(lines)


def _render_table(conn, name):
    info = _table_info(conn, name)
    fks = _fk_list(conn, name)
    fk_by_col = {}
    for fk in fks:
        # id, seq, table, from, to, on_update, on_delete, match
        fk_by_col.setdefault(fk[3], []).append((fk[2], fk[4], fk[6]))

    # Row counts are only worth printing when there are rows — a freshly
    # bootstrapped database is empty by design, and 45 lines of "0 rows" is noise.
    count = _row_count(conn, name)
    if count in (0, "?"):
        lines = [f"### `{name}`\n"]
    else:
        lines = [f"### `{name}`\n_{count} row{'' if count == 1 else 's'}_\n"]
    lines.append("| Column | Type | Notes |")
    lines.append("|---|---|---|")
    for col in info:
        # cid, name, type, notnull, dflt, pk
        cname, ctype, notnull, dflt, pk = col[1], col[2] or "", col[3], col[4], col[5]
        notes = []
        if pk:
            notes.append("**PK**")
        if notnull:
            notes.append("NOT NULL")
        if dflt is not None:
            notes.append(f"default `{dflt}`")
        for tgt_table, tgt_col, on_del in fk_by_col.get(cname, []):
            tag = f"→ `{tgt_table}.{tgt_col}`"
            if on_del and on_del != "NO ACTION":
                tag += f" (ON DELETE {on_del})"
            notes.append("FK " + tag)
        lines.append(f"| `{cname}` | {ctype} | {' · '.join(notes)} |")

    # Conventional (undeclared) FKs originating here
    conv = [c for c in CONVENTIONAL_FKS if c[0] == name]
    if conv:
        lines.append("")
        lines.append("_Conventional links (no declared FK — enforced by code, checked by `tools/db_check.py`):_")
        for _, ccol, ptable, pcol, note in conv:
            suffix = f" — {note}" if note else ""
            lines.append(f"- `{ccol}` → `{ptable}.{pcol}`{suffix}")

    # Indexes (skip auto PK indexes)
    idxs = [i for i in _index_list(conn, name) if not str(i[1]).startswith("sqlite_")]
    if idxs:
        lines.append("")
        lines.append("_Indexes:_")
        for i in idxs:
            uniq = "UNIQUE " if i[2] else ""
            cols = ", ".join(_index_cols(conn, i[1]))
            where = _index_predicate(conn, i[1])
            suffix = f" — partial: `{where}`" if where else ""
            lines.append(f"- {uniq}`{i[1]}` ({cols}){suffix}")

    lines.append("")
    return "\n".join(lines)


def _render_erd(conn, tables):
    lines = ["## Relationship map (ERD)\n",
             "Declared FKs plus conventional links. Rendered by GitHub / any Mermaid viewer.\n",
             "```mermaid", "erDiagram"]
    seen = set()

    def edge(child, parent, label):
        key = (child, parent, label)
        if parent in ("<by plan_type>",):
            for pt in PLAN_TYPE_TABLE.values():
                edge(child, pt, label + "?")
            return
        if parent not in tables or child not in tables:
            return
        if key in seen:
            return
        seen.add(key)
        lines.append(f'    {parent} ||--o{{ {child} : "{label}"')

    for child in sorted(tables):
        for fk in _fk_list(conn, child):
            edge(child, fk[2], fk[3])
    for child, ccol, ptable, _pcol, _note in CONVENTIONAL_FKS:
        edge(child, ptable, ccol + " (soft)")

    lines.append("```\n")
    return "\n".join(lines)


def _render_views(conn, views):
    lines = ["## Views\n",
             "Rand-float compatibility views over the `*_cents` tables. "
             "**Read** through these or the tables; **write** only to the `*_cents` tables.\n"]
    for v in views:
        lines.append(f"### `{v}`\n")
        lines.append("```sql")
        lines.append(_view_sql(conn, v))
        lines.append("```\n")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the on-disk file differs from freshly generated (CI drift guard)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    try:
        md = build_markdown(conn)
    finally:
        conn.close()

    if args.check:
        existing = ""
        if os.path.exists(args.out):
            with open(args.out, encoding="utf-8") as fh:
                existing = fh.read()
        if existing.strip() != md.strip():
            print(f"STALE: {args.out} is out of date — run `python tools/schema_map.py`",
                  file=sys.stderr)
            return 1
        print(f"OK: {args.out} matches the live schema.")
        return 0

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(f"Wrote {args.out} ({md.count(chr(10))} lines) from {os.path.relpath(args.db)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
