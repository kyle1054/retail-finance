"""Record WHO made a plan adjustment on `plan_adjustments_cents`.

Until now an adjustment row carried only a free-text `note`, so an adjustment made
by an admin in the browser and one made through the MCP connector were
indistinguishable in the audit trail. The undercharge tables already track an
`actor` (see `undercharge_events` / `undercharge_schedule_revisions`); this brings
uniform/lay-by adjustments in line, which is a precondition for letting an agent
write plans at all.

Existing rows are backfilled to 'admin' — every historical adjustment was made
through the web UI, which is admin-only.
"""


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(plan_adjustments_cents)")}
    if 'actor' not in cols:
        conn.execute("ALTER TABLE plan_adjustments_cents ADD COLUMN actor TEXT")
        conn.execute("UPDATE plan_adjustments_cents SET actor='admin' WHERE actor IS NULL")

    # The Rands compatibility view is a fixed column list, so it has to be
    # recreated to expose the new column.
    conn.execute("DROP VIEW IF EXISTS plan_adjustments")
    conn.execute("""
        CREATE VIEW plan_adjustments AS
            SELECT id, plan_type, plan_id, amount_cents / 100.0 AS amount, note,
                   CASE WHEN new_monthly_cents IS NULL THEN NULL
                        ELSE new_monthly_cents / 100.0 END AS new_monthly,
                   actor, created_at
            FROM plan_adjustments_cents
    """)
