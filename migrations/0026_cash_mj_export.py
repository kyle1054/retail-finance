"""Cash-recon Xero Manual-Journal export scaffolding.

Adds the per-store Xero *tracking option* name needed for the store-expenses MJ
(the `TrackingOption1` value, e.g. "Wellstone Park - EC") and seeds sensible
defaults for the two pieces of per-line VAT/tracking data the export needs:

  - stores.xero_tracking_name : the Xero tracking option for TrackingName1='Store'
    on every expense line. Seeded (only where NULL) from the store name using the
    store-to-tracking-option mapping configured for the ledger. Stores with no known mapping
    (Baymouth, Woodhaven) are left NULL for the admin to fill on the Xero-setup page.
  - recon_categories.vat_type  : 'standard' (Standard Rate Purchases) or 'novat'
    (No VAT (0%)). Default every expense category to 'novat', then flip the small
    set that is standard-rated. Only rows still NULL are touched.

`recon_categories.xero_code` and `stores.store_code` already exist (migration
0022) and are NOT overwritten here — the admin reconciles account codes on the
Xero-setup page. Additive + idempotent: guarded column add, seeds only NULLs.
"""


def _has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


# DB store name -> Xero tracking option name (from the configured category mapping).
_TRACKING = {
    'Riverbend': 'Riverbend - EST',
    'Westgate': 'Westgate - WST',
    'Kingsway': 'Kingsway - NTH',
    'Ashford': 'Ashford Square - WST',
    'Brookfield': 'Brookfield - NTH',
    'Fairview': 'Fairview - NTH',
    'Mill Street': 'Camden - STH',
    'Crossroads': 'Crossroads - NTH',
    'Lakeside': 'Lakeside - STH',
    'Northgate': 'Northgate - EST',
    'Elmwood': 'Elmwood - NTH',
    'Sunfield': 'Sunfield Mall - CEN',
    'Ravine': 'Ravine - WST',
    'Vineyard': 'Vineyard - STH',
    'Grand Central Mall': 'Grand Central Mall - NTH',
    'Highland Mall': 'Highland Mall - LP',
    'Parkview Mall': 'Parkview - NTH',
    'Riverside': 'Riverside - NW',
    'Eastvale': 'Eastvale - NTH',
    'Stonebridge - Fairhaven': 'Stonebridge - BFN',
    'The Atrium': 'NORTHWIND The Atrium',
    'Rosewood': 'Rosewood - NTH',
    'Summit': 'Summit - NTH',
    'Somerton Mall': 'Somerton - WST',
    'Oakvale': 'Oakvale - STH',
    'Harbour Point': 'Harbour Point - WST',
    'Wellstone': 'Wellstone Park - EC',
}

# Expense categories that are Standard-rated (everything else defaults to novat).
_STANDARD = (
    '%airtime%', '%electricity%', '%printing%', '%consumables%',
    '%cleaning services%', '%waste removal%',
)


def up(conn):
    # ── New column (guarded) ─────────────────────────────────────────────────
    if not _has_column(conn, 'stores', 'xero_tracking_name'):
        conn.execute("ALTER TABLE stores ADD COLUMN xero_tracking_name TEXT")

    # ── Seed per-store tracking name (only where NULL) ────────────────────────
    for name, tracking in _TRACKING.items():
        conn.execute(
            "UPDATE stores SET xero_tracking_name = ? "
            "WHERE name = ? AND (xero_tracking_name IS NULL OR xero_tracking_name = '')",
            (tracking, name))

    # ── Seed VAT type per category (only where NULL, so admin edits survive) ──
    # Set the standard-rated ones first, then default the rest to No VAT. Both
    # guard on `vat_type IS NULL`, so re-running never clobbers a later change.
    for pat in _STANDARD:
        conn.execute(
            "UPDATE recon_categories SET vat_type = 'standard' "
            "WHERE vat_type IS NULL AND kind = 'expense' AND lower(name) LIKE ?",
            (pat,))
    conn.execute(
        "UPDATE recon_categories SET vat_type = 'novat' "
        "WHERE vat_type IS NULL AND kind = 'expense'")
