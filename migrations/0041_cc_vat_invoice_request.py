"""Add a finance-controlled VAT tax-invoice request to card transactions.

The request is separate from ``require_individual``: that older flag asks for
any receipt tied to the transaction, while this flag explicitly asks the
cardholder to obtain a compliant supplier tax invoice showing the company VAT
number. Finance clears the request after reviewing the replacement invoice.
"""


def up(conn):
    cols = {row['name'] for row in conn.execute("PRAGMA table_info(cc_lines)")}
    if 'vat_invoice_required' not in cols:
        conn.execute(
            "ALTER TABLE cc_lines ADD COLUMN "
            "vat_invoice_required INTEGER NOT NULL DEFAULT 0")
    if 'vat_invoice_requested_at' not in cols:
        conn.execute(
            "ALTER TABLE cc_lines ADD COLUMN vat_invoice_requested_at TEXT")
    if 'vat_invoice_requested_by' not in cols:
        conn.execute(
            "ALTER TABLE cc_lines ADD COLUMN vat_invoice_requested_by TEXT")
