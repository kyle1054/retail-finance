"""Per-cardholder login credentials.

Each card *user* (identified by email — they may hold access to several cards)
gets their own password, set/shown when you grant access and resettable later.
Stored hashed; the shared staff password no longer applies to cardholders.
"""


def up(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cc_users (
            email TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )''')
