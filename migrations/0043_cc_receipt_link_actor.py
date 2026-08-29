"""Provenance for receipt <-> charge links.

``cc_receipt_lines`` was a bare (receipt_id, line_id) join table with no record of who
created a link or when. That was tolerable while only humans and the AI worker made
links through the web app. It stops being tolerable now that the MCP connector can
create and remove them: "which of these links did the agent make, and when?" was
previously unanswerable, and it is the first question anyone asks after an agent has
worked through a card.

So: ``actor`` (the same convention as ``plan_adjustments_cents.actor`` — an admin
username for a browser action, ``'mcp:claude'`` for a connector write, ``'ai'`` for the
auto-matcher) and ``linked_at`` (ISO-8601 UTC).

Both are nullable with no default. Backfilling would mean inventing provenance for links
whose origin genuinely is not recorded, and a NULL that honestly means "created before
this migration" is more useful than a plausible lie. New links get stamped; old ones
read as unknown.

Idempotent: only adds a column that isn't already there.
"""


def up(conn):
    cols = {row['name'] for row in conn.execute("PRAGMA table_info(cc_receipt_lines)")}
    if 'actor' not in cols:
        conn.execute("ALTER TABLE cc_receipt_lines ADD COLUMN actor TEXT")
    if 'linked_at' not in cols:
        conn.execute("ALTER TABLE cc_receipt_lines ADD COLUMN linked_at TEXT")
    # Answering "what did the agent touch on this card" means filtering by actor across
    # a card's links; without this it is a full scan of the join table.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_receipt_lines_actor "
                 "ON cc_receipt_lines(actor)")
