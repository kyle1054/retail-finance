"""Unified identity + credential store.

Historically the app carried FOUR identities across THREE credential stores:
  - admin_users : admins (username + password + single role super/retail/hq)
  - store_emails: stores, authenticated by ONE shared staff password (no per-
                  person credential) — left as-is here (see phase 3).
  - cc_users    : cardholders AND Regional Managers (email + own password).

This migration introduces ONE canonical identity table, `users`, plus a
`user_roles` grant table, and backfills it from the two real credential stores
(admin_users, cc_users). From here on a person is a single `users` row with a
single password, and their access is decided by capabilities:
  - user_roles     -> admin capability grants ('super' / 'retail' / 'hq').
  - cc_card_users  -> which cards a portal person may see (joined by email).
  - rm_stores      -> which stores a Regional Manager may see (joined by email).

Columns:
  - login  : what you type to sign in — a username (admins) or an email
             (cardholders / RMs). UNIQUE, case-insensitive.
  - email  : the address used to join the scope tables above; equals login for
             email logins, NULL for pure-username admins.

The old tables (admin_users, cc_users) are LEFT IN PLACE, untouched, so this
change is reversible; all identity helpers in database.py are repointed to
`users` and nothing writes the old tables any more.

Idempotent: CREATE ... IF NOT EXISTS + INSERT OR IGNORE keyed on login.
"""


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def up(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            login         TEXT NOT NULL UNIQUE COLLATE NOCASE,
            email         TEXT COLLATE NOCASE,
            display_name  TEXT,
            password_hash TEXT NOT NULL,
            is_active     INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT DEFAULT (datetime('now')),
            updated_at    TEXT DEFAULT (datetime('now'))
        )
    ''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_roles (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role    TEXT NOT NULL,
            PRIMARY KEY (user_id, role)
        )
    ''')

    # ── Backfill admins: admin_users -> users (+ their role) ────────────────
    if _table_exists(conn, 'admin_users'):
        for r in conn.execute(
                "SELECT username, display_name, password_hash, role, created_at "
                "FROM admin_users").fetchall():
            login = (r['username'] or '').strip()
            if not login:
                continue
            email = login if '@' in login else None
            conn.execute(
                "INSERT OR IGNORE INTO users "
                "(login, email, display_name, password_hash, is_active, created_at) "
                "VALUES (?,?,?,?,1, COALESCE(?, datetime('now')))",
                (login, email, r['display_name'], r['password_hash'], r['created_at']))
            uid = conn.execute("SELECT id FROM users WHERE login=?", (login,)).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role) VALUES (?,?)",
                         (uid, (r['role'] or 'super')))

    # ── Backfill portal people: cc_users -> users (no admin role) ───────────
    if _table_exists(conn, 'cc_users'):
        for r in conn.execute(
                "SELECT email, password_hash, created_at FROM cc_users").fetchall():
            email = (r['email'] or '').strip().lower()
            if not email:
                continue
            # Best-effort display name from their card-access, then RM records.
            name_row = conn.execute(
                "SELECT name FROM cc_card_users WHERE email=? AND name IS NOT NULL "
                "ORDER BY id LIMIT 1", (email,)).fetchone()
            name = name_row['name'] if name_row else None
            if name is None and _table_exists(conn, 'rm_users'):
                nm = conn.execute(
                    "SELECT name FROM rm_users WHERE email=? AND name IS NOT NULL LIMIT 1",
                    (email,)).fetchone()
                name = nm['name'] if nm else None
            conn.execute(
                "INSERT OR IGNORE INTO users "
                "(login, email, display_name, password_hash, is_active, created_at) "
                "VALUES (?,?,?,?,1, COALESCE(?, datetime('now')))",
                (email, email, name, r['password_hash'], r['created_at']))
