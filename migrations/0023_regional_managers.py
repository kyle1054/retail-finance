"""Regional Managers (RMs) — a read-only per-store cash-dashboard persona.

Additive + idempotent. Two new tables plus an index:
  - rm_users  : a portal person who is a Regional Manager. Credentials are shared
                with the existing cc_users store (email + password_hash), so an RM
                who is also a cardholder uses ONE login. `active` gates access.
  - rm_stores : the store→RM assignment. `store` is the PRIMARY KEY, so each store
                has exactly one RM; an RM (email) may own many stores.

Guarded with CREATE TABLE/INDEX IF NOT EXISTS so it is safe to re-run.
"""


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rm_users (
            email      TEXT PRIMARY KEY,
            name       TEXT,
            active     INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rm_stores (
            store      TEXT PRIMARY KEY,
            email      TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rm_stores_email ON rm_stores(email)")
