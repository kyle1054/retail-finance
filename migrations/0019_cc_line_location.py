"""Add a free-text `location` to cc_lines.

Cardholders capture where a charge happened (e.g. "Summit City", "OR Tambo")
alongside the reason, so finance has the context it needs to code the expense.
Idempotent: only adds the column if it isn't already there.
"""


def up(conn):
    cols = {r['name'] for r in conn.execute("PRAGMA table_info(cc_lines)")}
    if 'location' not in cols:
        conn.execute("ALTER TABLE cc_lines ADD COLUMN location TEXT")
