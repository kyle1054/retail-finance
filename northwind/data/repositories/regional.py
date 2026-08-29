"""Regional Managers repository (extracted from the database facade).

Calls the facade module at runtime (`_db.get_db()`, and the cards helpers
`_db.find_cc_cards_for_email` / `_db.get_cc_portal_task_count`) so the
facade's DB_PATH stays the single monkeypatchable connection source. The
facade re-exports these functions, so `db.<name>()` is unchanged.
"""
from northwind.data import database as _db


# ── Regional Managers (read-only per-store cash dashboard) ───────────────────
# RMs share the unified `users` credential store (one login for a person who may
# also be a cardholder). rm_users marks someone an RM (and gates via `active`); rm_stores
# is the store→RM assignment (store is the PK, so one RM per store).

def get_rm_user(email):
    """The rm_users row for an email (lower-cased), or None."""
    email = (email or '').strip().lower()
    if not email:
        return None
    conn = _db.get_db()
    try:
        return conn.execute(
            "SELECT * FROM rm_users WHERE email = ?", (email,)).fetchone()
    finally:
        conn.close()


def list_rm_users():
    """Every RM (active or not), sorted by name, with their store count and
    single company-card assignment. `card_count` may exceed one only for legacy
    data; the admin UI highlights that state and saving a card normalises it."""
    conn = _db.get_db()
    try:
        rows = conn.execute("SELECT email, name, active FROM rm_users").fetchall()
        out = []
        for r in rows:
            email = r['email']
            store_count = conn.execute(
                "SELECT COUNT(*) AS n FROM rm_stores WHERE email = ?",
                (email,)).fetchone()['n']
            cards = conn.execute(
                "SELECT c.id, COALESCE(c.display_name, c.card_name) AS name "
                "FROM cc_card_users u JOIN cc_cards c ON c.id = u.card_id "
                "WHERE u.email = ? AND c.active = 1 "
                "ORDER BY c.display_name COLLATE NOCASE, c.card_name COLLATE NOCASE",
                (email,)).fetchall()
            out.append({'email': email, 'name': r['name'], 'active': r['active'],
                        'store_count': store_count, 'is_cardholder': bool(cards),
                        'card_count': len(cards),
                        'card_id': cards[0]['id'] if len(cards) == 1 else None,
                        'card_name': cards[0]['name'] if len(cards) == 1 else None})
        out.sort(key=lambda d: ((d['name'] or d['email'] or '').lower()))
        return out
    finally:
        conn.close()


def upsert_rm_user(email, name, active=1):
    """Create or update an RM row (name/active). Email is lower-cased."""
    email = (email or '').strip().lower()
    conn = _db.get_db()
    try:
        conn.execute(
            "INSERT INTO rm_users (email, name, active) VALUES (?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET name = excluded.name, "
            "active = excluded.active",
            (email, (name or '').strip() or None, 1 if active else 0))
        conn.commit()
    finally:
        conn.close()


def set_rm_active(email, active):
    """Activate/deactivate an RM (a deactivated RM's stores stay assigned but the
    RM can no longer log in to the dashboard)."""
    email = (email or '').strip().lower()
    conn = _db.get_db()
    try:
        conn.execute("UPDATE rm_users SET active = ? WHERE email = ?",
                     (1 if active else 0, email))
        conn.commit()
    finally:
        conn.close()


def delete_rm_user(email):
    """Remove an RM and all of their store assignments. Their shared `users`
    login is left intact (harmless if they still hold cards; unused otherwise)."""
    email = (email or '').strip().lower()
    conn = _db.get_db()
    try:
        conn.execute("DELETE FROM rm_stores WHERE email = ?", (email,))
        conn.execute("DELETE FROM rm_users WHERE email = ?", (email,))
        conn.commit()
    finally:
        conn.close()


def get_rm_stores(email):
    """Store names assigned to this RM — ONLY if the RM row exists and is active.
    An inactive or unknown RM gets an empty list (the dashboard's core scoping
    guard). Sorted by name."""
    email = (email or '').strip().lower()
    if not email:
        return []
    conn = _db.get_db()
    try:
        active = conn.execute(
            "SELECT active FROM rm_users WHERE email = ?", (email,)).fetchone()
        if active is None or not active['active']:
            return []
        rows = conn.execute(
            "SELECT store FROM rm_stores WHERE email = ? ORDER BY store",
            (email,)).fetchall()
        return [r['store'] for r in rows]
    finally:
        conn.close()


def get_store_rm(store):
    """The email of the RM assigned to `store`, or None."""
    conn = _db.get_db()
    try:
        row = conn.execute(
            "SELECT email FROM rm_stores WHERE store = ?", (store,)).fetchone()
        return row['email'] if row else None
    finally:
        conn.close()


def assign_store_rm(store, email_or_none):
    """Set (or clear) the RM for a store. `store` is the PK, so this upserts a
    single assignment; passing None/'' deletes the row (store has no RM)."""
    conn = _db.get_db()
    try:
        email = (email_or_none or '').strip().lower()
        if not email:
            conn.execute("DELETE FROM rm_stores WHERE store = ?", (store,))
        else:
            conn.execute(
                "INSERT INTO rm_stores (store, email) VALUES (?, ?) "
                "ON CONFLICT(store) DO UPDATE SET email = excluded.email",
                (store, email))
        conn.commit()
    finally:
        conn.close()


def set_rm_card(email, card_id_or_none):
    """Replace an RM's card access with zero or one active company card.

    This is the canonical admin operation for RM card assignment. It deliberately
    replaces any legacy/mistaken multi-card grants in one transaction, while
    leaving the RM's unified login intact when a card is removed.
    """
    email = (email or '').strip().lower()
    conn = _db.get_db()
    try:
        rm = conn.execute(
            "SELECT name FROM rm_users WHERE email = ?", (email,)).fetchone()
        if not rm:
            raise ValueError('Unknown Regional Manager.')
        card = None
        if card_id_or_none not in (None, ''):
            try:
                card_id = int(card_id_or_none)
            except (TypeError, ValueError):
                raise ValueError('Choose a valid active credit card.')
            card = conn.execute(
                "SELECT id, card_name, display_name FROM cc_cards "
                "WHERE id = ? AND active = 1", (card_id,)).fetchone()
            if not card:
                raise ValueError('Choose a valid active credit card.')
        with conn:
            conn.execute("DELETE FROM cc_card_users WHERE email = ?", (email,))
            if card:
                conn.execute(
                    "INSERT INTO cc_card_users (card_id, email, name, access_note) "
                    "VALUES (?, ?, ?, ?)",
                    (card['id'], email, rm['name'], 'Assigned from Regional Managers'))
        return card
    finally:
        conn.close()


def rm_capabilities(email):
    """What a portal person can do. `is_rm` is
    True only for an ACTIVE rm_users row; `stores` is their assigned stores (empty
    if not an active RM); card metadata drives the shared portal switcher."""
    email = (email or '').strip().lower()
    row = get_rm_user(email)
    is_rm = row is not None and bool(row['active'])
    stores = get_rm_stores(email)          # empty unless active RM
    cards = _db.find_cc_cards_for_email(email)
    return {
        'is_rm': is_rm, 'rm_name': (row['name'] if row else None),
        'stores': stores, 'has_cards': bool(cards), 'card_count': len(cards),
        'card_task_count': _db.get_cc_portal_task_count(email) if cards else 0,
    }
