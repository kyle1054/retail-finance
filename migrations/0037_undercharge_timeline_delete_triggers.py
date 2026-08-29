"""Keep legacy delete workflows compatible with the new timeline foreign keys.

Operational plan deletion and test cleanup historically delete an undercharge
or transaction directly.  Preserve that contract while retaining declared FKs:
schedule/event children are removed with their plan, and a deleted transaction
simply unlinks its schedule item.
"""


def up(conn):
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_uc_timeline_before_plan_delete
        BEFORE DELETE ON undercharges_cents
        BEGIN
            DELETE FROM undercharge_schedule_items
             WHERE undercharge_id = OLD.id;
            DELETE FROM undercharge_schedule_revisions
             WHERE undercharge_id = OLD.id;
            DELETE FROM undercharge_events
             WHERE undercharge_id = OLD.id;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_uc_timeline_before_tx_delete
        BEFORE DELETE ON deduction_transactions_cents
        BEGIN
            UPDATE undercharge_schedule_items
               SET transaction_id = NULL
             WHERE transaction_id = OLD.id;
        END
        """
    )
