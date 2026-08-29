"""Lease account-coding work so concurrent AI workers do not duplicate calls."""


def up(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cc_lines)")}
    if 'coding_status' not in cols:
        conn.execute("ALTER TABLE cc_lines ADD COLUMN coding_status TEXT")
    if 'coding_claimed_at' not in cols:
        conn.execute("ALTER TABLE cc_lines ADD COLUMN coding_claimed_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cc_lines_coding_claim "
        "ON cc_lines(coding_dirty, coding_status, coding_claimed_at)")
