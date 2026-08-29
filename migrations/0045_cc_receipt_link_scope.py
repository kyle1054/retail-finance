"""Enforce that every receipt link stays inside one card statement.

The web routes already scope links before writing them, but ``cc_receipt_lines``
is the final source of truth.  These triggers make a cross-card or cross-month
link impossible even if a future caller forgets the route-level guard.
"""


def up(conn):
    bad = conn.execute(
        "SELECT COUNT(*) FROM cc_receipt_lines rl "
        "JOIN cc_receipts r ON r.id=rl.receipt_id "
        "JOIN cc_lines l ON l.id=rl.line_id "
        "WHERE r.card_id!=l.card_id OR r.statement_id IS NULL "
        "OR r.statement_id!=l.statement_id"
    ).fetchone()[0]
    if bad:
        raise RuntimeError(
            f"Cannot enforce receipt-link scope: {bad} existing link(s) cross a card/statement")

    scope_check = (
        "NOT EXISTS ("
        "SELECT 1 FROM cc_receipts r JOIN cc_lines l "
        "ON l.card_id=r.card_id AND l.statement_id=r.statement_id "
        "WHERE r.id=NEW.receipt_id AND l.id=NEW.line_id)"
    )
    conn.execute(f'''
        CREATE TRIGGER IF NOT EXISTS cc_receipt_lines_scope_insert
        BEFORE INSERT ON cc_receipt_lines
        WHEN {scope_check}
        BEGIN
            SELECT RAISE(ABORT, 'receipt and transaction must share card and statement');
        END
    ''')
    conn.execute(f'''
        CREATE TRIGGER IF NOT EXISTS cc_receipt_lines_scope_update
        BEFORE UPDATE OF receipt_id, line_id ON cc_receipt_lines
        WHEN {scope_check}
        BEGIN
            SELECT RAISE(ABORT, 'receipt and transaction must share card and statement');
        END
    ''')
