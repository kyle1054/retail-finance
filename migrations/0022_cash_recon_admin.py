"""Cash-recon admin phase — reason hints, Xero scaffolding, mandatory reasons.

Additive only. Three new nullable columns plus a one-time seed of per-category
reason hints:
  - recon_categories.reason_hint : drives the (now mandatory) reason field's
    label/placeholder per category in the ledger UI.
  - recon_categories.vat_type    : scaffolding for a future Xero manual-journal
    export (VAT type per line). Left NULL — no export is built here.
  - stores.store_code            : scaffolding for the same export (Xero store /
    tracking code). Left NULL.

Column adds are guarded against PRAGMA table_info so the migration is safe to
re-run and never errors if a column already exists.
"""


def _has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def up(conn):
    # ── New columns (guarded) ────────────────────────────────────────────────
    if not _has_column(conn, 'recon_categories', 'reason_hint'):
        conn.execute("ALTER TABLE recon_categories ADD COLUMN reason_hint TEXT")
    if not _has_column(conn, 'recon_categories', 'vat_type'):
        conn.execute("ALTER TABLE recon_categories ADD COLUMN vat_type TEXT")
    if not _has_column(conn, 'stores', 'store_code'):
        conn.execute("ALTER TABLE stores ADD COLUMN store_code TEXT")

    # ── Seed reason hints per category (only where still NULL) ────────────────
    # Most specific matches first; a catch-all by kind at the end.
    def _set(where_sql, params, hint):
        conn.execute(
            f"UPDATE recon_categories SET reason_hint = ? "
            f"WHERE reason_hint IS NULL AND ({where_sql})",
            (hint, *params))

    # Cash Sale -> receipt number
    _set("lower(name) LIKE ?", ('%cash sale%',), 'Sale / receipt number')
    # Float top up / Cash Top Up -> who + reference
    _set("lower(name) LIKE ? OR lower(name) LIKE ?",
         ('%top up%', '%top-up%'), 'Who topped up + reference')
    # Banked -> who banked it + slip/reference
    _set("lower(name) LIKE ?", ('%bank%',), 'Who banked it + slip / reference')
    # Cash Balancing / adjustments -> reason for the adjustment
    _set("lower(name) LIKE ? OR kind = ?",
         ('%balancing%', 'adjustment'), 'Reason for the adjustment')
    # Store Expense Other -> describe it
    _set("lower(name) LIKE ?", ('%expense other%',), 'Describe the expense')
    # Receipt / specify expenses -> receipt no. + what for
    _set("(lower(name) LIKE ? OR lower(name) LIKE ?) AND kind = ?",
         ('%receipt%', '%specify%', 'expense'), 'Receipt no. + what it was for')
    # Any remaining expense -> what was it for?
    _set("kind = ?", ('expense',), 'What was it for?')
    # Any remaining income -> reference
    _set("kind = ?", ('income',), 'Reference / details')
    # Absolute catch-all
    _set("1 = 1", (), 'Reason / details')
