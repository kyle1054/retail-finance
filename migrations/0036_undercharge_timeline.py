"""Versioned undercharge schedules and non-payroll financial events.

The original undercharge model stored one total, one term, and a payment count.
That cannot represent a changed future schedule without reinterpreting payments
that already happened.  These tables make completed payroll transactions
immutable and store each future deduction/refund as an exact cent amount.
"""


EVENT_TYPES = (
    "customer_payment",
    "write_off",
    "liability_adjustment",
    "external_refund",
    "refund_waiver",
    "customer_payment_reversal",
)


def _add_month(year, month, offset):
    idx = year * 12 + (month - 1) + offset
    return idx // 12, idx % 12 + 1


def _amounts(total_cents, count):
    base, remainder = divmod(abs(int(total_cents)), int(count))
    values = [base] * count
    values[-1] += remainder
    return values


def up(conn):
    statements = [
        """
        CREATE TABLE IF NOT EXISTS undercharge_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            undercharge_id INTEGER NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN (
                'customer_payment', 'write_off', 'liability_adjustment',
                'external_refund', 'refund_waiver',
                'customer_payment_reversal'
            )),
            amount_cents INTEGER NOT NULL,
            effective_year INTEGER,
            effective_month INTEGER,
            note TEXT,
            actor TEXT,
            reverses_event_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (undercharge_id) REFERENCES undercharges_cents(id),
            FOREIGN KEY (reverses_event_id) REFERENCES undercharge_events(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_uc_events_plan
            ON undercharge_events(undercharge_id, created_at, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_uc_events_reverse
            ON undercharge_events(reverses_event_id)
        """,
        """
        CREATE TABLE IF NOT EXISTS undercharge_schedule_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            undercharge_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('deduction', 'refund')),
            start_year INTEGER NOT NULL,
            start_month INTEGER NOT NULL CHECK(start_month BETWEEN 1 AND 12),
            total_cents INTEGER NOT NULL CHECK(total_cents > 0),
            installment_count INTEGER NOT NULL CHECK(installment_count > 0),
            reason TEXT,
            actor TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (undercharge_id, version),
            FOREIGN KEY (undercharge_id) REFERENCES undercharges_cents(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_uc_schedule_revisions_plan
            ON undercharge_schedule_revisions(undercharge_id, version)
        """,
        """
        CREATE TABLE IF NOT EXISTS undercharge_schedule_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id INTEGER NOT NULL,
            undercharge_id INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            due_year INTEGER NOT NULL,
            due_month INTEGER NOT NULL CHECK(due_month BETWEEN 1 AND 12),
            amount_cents INTEGER NOT NULL CHECK(amount_cents != 0),
            state TEXT NOT NULL DEFAULT 'scheduled'
                CHECK(state IN ('scheduled', 'superseded', 'cancelled', 'missed')),
            transaction_id INTEGER,
            state_reason TEXT,
            state_changed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (revision_id, sequence),
            FOREIGN KEY (revision_id) REFERENCES undercharge_schedule_revisions(id),
            FOREIGN KEY (undercharge_id) REFERENCES undercharges_cents(id),
            FOREIGN KEY (transaction_id) REFERENCES deduction_transactions_cents(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_uc_schedule_items_due
            ON undercharge_schedule_items(due_year, due_month, state)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_uc_schedule_items_plan
            ON undercharge_schedule_items(undercharge_id, state)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_uc_schedule_active_month
            ON undercharge_schedule_items(undercharge_id, due_year, due_month)
            WHERE state = 'scheduled'
        """,
    ]
    for statement in statements:
        conn.execute(statement)

    # Backfill one exact original schedule per existing undercharge.  Processed
    # items are linked to the immutable transaction for their month.  Terminal
    # plans keep their history but unprocessed items are cancelled.
    plans = conn.execute(
        """
        SELECT id, total_amount_cents, recovery_method, split_months,
               payments_made, status,
               COALESCE(start_year, incident_year) AS sy,
               COALESCE(start_month, incident_month) AS sm
        FROM undercharges_cents
        WHERE COALESCE(type, 'undercharge') = 'undercharge'
        ORDER BY id
        """
    ).fetchall()
    terminal = {
        "recovered", "written_off", "accounted_for",
        "paid_by_customer", "reimbursed",
    }
    for plan in plans:
        if not plan["sy"] or not plan["sm"]:
            continue
        count = 1 if plan["recovery_method"] == "full" else max(
            int(plan["split_months"] or 1), 1
        )
        cur = conn.execute(
            """
            INSERT INTO undercharge_schedule_revisions
                (undercharge_id, version, kind, start_year, start_month,
                 total_cents, installment_count, reason, actor)
            VALUES (?, 1, 'deduction', ?, ?, ?, ?, 'Legacy schedule backfill', 'migration')
            """,
            (
                plan["id"], int(plan["sy"]), int(plan["sm"]),
                int(plan["total_amount_cents"]), count,
            ),
        )
        revision_id = cur.lastrowid
        for sequence, amount in enumerate(_amounts(plan["total_amount_cents"], count), 1):
            year, month = _add_month(int(plan["sy"]), int(plan["sm"]), sequence - 1)
            tx = conn.execute(
                """
                SELECT id FROM deduction_transactions_cents
                WHERE plan_type='undercharge' AND plan_id=?
                  AND year=? AND month=? AND COALESCE(voided, 0)=0
                ORDER BY id DESC LIMIT 1
                """,
                (plan["id"], year, month),
            ).fetchone()
            state = "scheduled"
            reason = None
            if not tx and plan["status"] in terminal:
                state = "cancelled"
                reason = "Plan was already terminal during schedule migration"
            conn.execute(
                """
                INSERT INTO undercharge_schedule_items
                    (revision_id, undercharge_id, sequence, due_year, due_month,
                     amount_cents, state, transaction_id, state_reason,
                     state_changed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END)
                """,
                (
                    revision_id, plan["id"], sequence, year, month, amount,
                    state, tx["id"] if tx else None, reason, reason,
                ),
            )

        # Preserve historical reimbursements that do not belong to the original
        # positive schedule as their own one-item refund revisions.
        refunds = conn.execute(
            """
            SELECT id, amount_cents, year, month
            FROM deduction_transactions_cents
            WHERE plan_type='undercharge' AND plan_id=?
              AND amount_cents < 0 AND COALESCE(voided, 0)=0
            ORDER BY year, month, id
            """,
            (plan["id"],),
        ).fetchall()
        version = 1
        for refund in refunds:
            version += 1
            cur = conn.execute(
                """
                INSERT INTO undercharge_schedule_revisions
                    (undercharge_id, version, kind, start_year, start_month,
                     total_cents, installment_count, reason, actor)
                VALUES (?, ?, 'refund', ?, ?, ?, 1,
                        'Legacy reimbursement backfill', 'migration')
                """,
                (
                    plan["id"], version, refund["year"], refund["month"],
                    abs(int(refund["amount_cents"])),
                ),
            )
            conn.execute(
                """
                INSERT INTO undercharge_schedule_items
                    (revision_id, undercharge_id, sequence, due_year, due_month,
                     amount_cents, transaction_id)
                VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    cur.lastrowid, plan["id"], refund["year"], refund["month"],
                    int(refund["amount_cents"]), refund["id"],
                ),
            )
