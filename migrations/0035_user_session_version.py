"""Immediately revoke sessions after password, status, or role changes."""


def up(conn):
    columns = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if 'auth_version' not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 1"
        )
