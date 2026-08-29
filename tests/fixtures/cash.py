"""Store cash reconciliation: opening floats, daily entries, Shopify comparison.

Entries are kept modest and income-heavy so every seeded store closes each month
with a positive float — a store whose float goes negative is a data-entry error
in the real app, not a shape worth shipping in a sample database.
"""
from .calendar_math import iso_in_month, shift
from . import names


def _categories(conn):
    rows = conn.execute(
        "SELECT id, name, kind FROM recon_categories WHERE active=1 ORDER BY id"
    ).fetchall()
    by_kind = {}
    for row in rows:
        by_kind.setdefault(row['kind'], []).append(dict(row))
    return by_kind


def seed(conn, profile, stores):
    """Openings + entries for the most recent months, on a slice of the stores."""
    by_kind = _categories(conn)
    income = by_kind.get('income', [])
    expense = by_kind.get('expense', [])
    transfer = by_kind.get('transfer', [])
    adjustment = by_kind.get('adjustment', [])
    if not (income and expense):
        return {'stores': 0, 'entries': 0}

    scope = stores[:profile['recon_stores']]
    months = list(range(-(profile['recon_months'] - 1), 1))
    entries = 0

    for store_index, store in enumerate(scope):
        opening_cents = 100000 + store_index * 25000
        first_year, first_month = shift(months[0])
        conn.execute(
            "INSERT OR REPLACE INTO cash_recon_opening "
            "(store, year, month, opening_cents) VALUES (?,?,?,?)",
            (store, first_year, first_month, opening_cents))

        for month_index, offset in enumerate(months):
            step = store_index + month_index

            def _add(category, day, amount_cents, direction, note):
                conn.execute(
                    "INSERT INTO cash_recon_entries "
                    "(store, entry_date, category_id, description, direction, "
                    " amount_cents, note, status, created_by) "
                    "VALUES (?,?,?,?,?,?,?,'submitted',?)",
                    (store, iso_in_month(offset, day), category['id'],
                     category['name'], direction, amount_cents, note, store))

            # Money in first, so the float never dips below zero mid-month.
            for k in range(2):
                _add(income[(step + k) % len(income)], 3 + k * 9,
                     60000 + k * 15000, 'in',
                     'Till %s' % (k + 1))
                entries += 1
            for k in range(4):
                _add(expense[(step + k * 3) % len(expense)], 5 + k * 5,
                     4500 + k * 2200, 'out',
                     names.CASH_NOTES[(step + k) % len(names.CASH_NOTES)])
                entries += 1
            if transfer:
                _add(transfer[step % len(transfer)], 26, 40000, 'out',
                     'Banked at month end')
                entries += 1
            if adjustment and month_index == 0:
                _add(adjustment[step % len(adjustment)], 27, 1500, 'out',
                     'Till count short')
                entries += 1

    return {'stores': len(scope), 'entries': entries}


def seed_settings(conn):
    """The Xero cash-sales journal settings, set to the app's own defaults.

    Written explicitly rather than left absent so the settings page has saved
    values to show; the values match the code defaults, so nothing behaves
    differently for having been stored.
    """
    for key, value in (('cash_sales_contra_code', '8990.9'),
                       ('cash_sales_narration', 'Retail cash sales journal')):
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    return 2


def seed_shopify_comparison(conn, stores, month_offsets=(-2, -1)):
    """Shopify cash-sale rows for a couple of months, plus variance notes.

    Locations are taken from the mappings migration 0046 seeded, so the
    comparison page has rows that actually resolve to a store.
    """
    total = 0
    for offset in month_offsets:
        total += _shopify_month(conn, offset)
    return total


def _shopify_month(conn, month_offset):
    year, month = shift(month_offset)
    mapped = conn.execute(
        "SELECT shopify_location, store FROM cash_shopify_store_mappings "
        "ORDER BY shopify_location LIMIT 8").fetchall()
    if not mapped:
        return 0
    rows = []
    for index, row in enumerate(mapped):
        gross = 250000 + index * 35000
        refunded = 0 if index % 3 else 12500
        rows.append({
            'source_row': index + 2,
            'pos_location_name': row['shopify_location'],
            'payment_gateway': 'Cash',
            'order_name': '#%04d' % (1200 + index),
            'transactions': 4 + index,
            'gross_cents': gross,
            'refunded_cents': refunded,
            'net_cents': gross - refunded,
        })
    cur = conn.execute(
        "INSERT OR IGNORE INTO cash_shopify_uploads "
        "(year, month, source_filename, source_sha256, row_count, uploaded_by) "
        "VALUES (?,?,?,?,?,?)",
        (year, month, 'cash-sales-%04d-%02d.xlsx' % (year, month),
         '0' * 64, len(rows), 'seed'))
    upload_id = cur.lastrowid
    if not upload_id:
        found = conn.execute(
            "SELECT id FROM cash_shopify_uploads WHERE year=? AND month=?",
            (year, month)).fetchone()
        if not found:
            return 0
        upload_id = found['id']
    for row in rows:
        conn.execute(
            "INSERT INTO cash_shopify_rows "
            "(upload_id, source_row, pos_location_name, payment_gateway, order_name, "
            " transactions, gross_cents, refunded_cents, net_cents) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (upload_id, row['source_row'], row['pos_location_name'],
             row['payment_gateway'], row['order_name'], row['transactions'],
             row['gross_cents'], row['refunded_cents'], row['net_cents']))
    for row, reason in zip(mapped[:2], (
            'Late till close rolled one day into the next month.',
            'A refund was banked before the sale cleared.')):
        conn.execute(
            "INSERT OR IGNORE INTO cash_sales_variance_reasons "
            "(year, month, store, reason, updated_by) VALUES (?,?,?,?,?)",
            (year, month, row['store'], reason, 'seed'))
    return len(rows)
