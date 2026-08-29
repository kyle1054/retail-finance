"""Index cc_lines by statement_id.

The portal month view, the import merge, and the per-statement coverage queries
all look lines up by statement_id, but the only existing index on cc_lines is
(card_id, status, needs_receipt). Add the missing one so those stay fast as the
statement history grows.
"""


def up(conn):
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_lines_statement "
                 "ON cc_lines(statement_id)")
