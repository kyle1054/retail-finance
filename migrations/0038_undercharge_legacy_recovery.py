"""Preserve pre-ledger undercharge payments explicitly.

Some early records advanced ``payments_made`` before the immutable transaction
ledger existed.  Do not invent dated payroll transactions for them, but do
carry their historical value/count into the new balance engine as clearly
labelled legacy recovery.
"""


def _amounts(total_cents, count):
    base, remainder = divmod(int(total_cents), int(count))
    values = [base] * count
    values[-1] += remainder
    return values


def up(conn):
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(undercharges_cents)").fetchall()}
    if "legacy_paid_cents" not in cols:
        conn.execute(
            "ALTER TABLE undercharges_cents "
            "ADD COLUMN legacy_paid_cents INTEGER NOT NULL DEFAULT 0")
    if "legacy_payments_count" not in cols:
        conn.execute(
            "ALTER TABLE undercharges_cents "
            "ADD COLUMN legacy_payments_count INTEGER NOT NULL DEFAULT 0")

    plans = conn.execute(
        "SELECT id,total_amount_cents,recovery_method,split_months,payments_made "
        "FROM undercharges_cents WHERE COALESCE(type,'undercharge')='undercharge'"
    ).fetchall()
    for plan in plans:
        split = 1 if plan["recovery_method"] == "full" else max(
            int(plan["split_months"] or 1), 1)
        old_count = min(max(int(plan["payments_made"] or 0), 0), split)
        scheduled_paid = sum(_amounts(plan["total_amount_cents"], split)[:old_count])
        actual = conn.execute(
            "SELECT COUNT(*) count,COALESCE(SUM(amount_cents),0) cents "
            "FROM deduction_transactions_cents WHERE plan_type='undercharge' "
            "AND plan_id=? AND amount_cents>0 AND COALESCE(voided,0)=0",
            (plan["id"],)).fetchone()
        legacy_count = max(old_count - int(actual["count"]), 0)
        legacy_cents = max(scheduled_paid - int(actual["cents"]), 0)
        conn.execute(
            "UPDATE undercharges_cents SET legacy_paid_cents=?,"
            "legacy_payments_count=? WHERE id=?",
            (legacy_cents, legacy_count, plan["id"]))
