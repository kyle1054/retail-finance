"""Audit trail for the "reconciled in Xero" tick, incl. deliberate overrides.

The tick used to be available only on a transaction that already met the
finance-ready rule. In practice a transaction can be genuinely reconciled in
Xero while the cardholder never supplied the receipt, reason or location — the
admin still has to be able to close it off. That is an override, and because a
closed-off transaction now also disappears from the cardholder's checklist, it
needs to record who did it and that evidence was still missing.

Idempotent: only adds the columns if they aren't already there.
"""


def up(conn):
    cols = {row['name'] for row in conn.execute("PRAGMA table_info(cc_lines)")}
    if 'xero_reconciled_by' not in cols:
        conn.execute("ALTER TABLE cc_lines ADD COLUMN xero_reconciled_by TEXT")
    if 'xero_reconciled_override' not in cols:
        conn.execute("ALTER TABLE cc_lines ADD COLUMN "
                     "xero_reconciled_override INTEGER NOT NULL DEFAULT 0")
