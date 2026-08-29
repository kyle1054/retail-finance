"""Prevent legacy count-only installments from being deducted a second time."""


def up(conn):
    conn.execute(
        """
        UPDATE undercharge_schedule_items
           SET state='cancelled',
               state_reason='Historical payment predates transaction ledger',
               state_changed_at=datetime('now')
         WHERE transaction_id IS NULL
           AND state='scheduled'
           AND sequence <= (
               SELECT u.payments_made
                 FROM undercharges_cents u
                WHERE u.id=undercharge_schedule_items.undercharge_id
           )
           AND EXISTS (
               SELECT 1 FROM undercharges_cents u
                WHERE u.id=undercharge_schedule_items.undercharge_id
                  AND u.legacy_payments_count > 0
           )
        """
    )
