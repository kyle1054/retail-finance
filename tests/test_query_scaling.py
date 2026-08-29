"""Guards against per-row database access creeping back into list pages.

Three pages used to price each row with its own handful of statements, so the
query count grew linearly with the table rather than staying flat:

    /admin        815 -> 44        /employees   258 -> 18
    /stores       243 -> 13        /undercharges 141 -> 15
    /cash         162 -> 11

None of that was slow *yet* — the dev DB is small and every page rendered in
under 25 ms. The problem is the shape: 815 statements for 47 undercharges means
8,000 for 470. These tests fail on the shape, not the wall clock, so the
regression is caught while it is still cheap.

The ceilings are deliberately loose (roughly 3x the current count). They exist to
catch "someone put get_undercharge_account back inside a loop", not to police
small honest changes.
"""
import sqlite3

import pytest

from northwind.data import database as db


class _Counter:
    """Counts every statement executed while it is installed."""

    def __init__(self):
        self.n = 0
        self._real = sqlite3.connect

    def __enter__(self):
        counter = self

        class Traced(sqlite3.Connection):
            def execute(self, sql, *a, **kw):
                counter.n += 1
                return super().execute(sql, *a, **kw)

            def executemany(self, sql, *a, **kw):
                counter.n += 1
                return super().executemany(sql, *a, **kw)

        def connect(*a, **kw):
            kw['factory'] = Traced
            return self._real(*a, **kw)

        sqlite3.connect = connect
        return self

    def __exit__(self, *exc):
        sqlite3.connect = self._real
        return False


# (url, ceiling). Current counts in the comment for context when one trips.
PAGE_CEILINGS = [
    ('/admin', 140),          # currently 44
    ('/employees', 60),       # currently 18
    ('/undercharges', 50),    # currently 15
    ('/laybys', 40),          # currently 10
    ('/uniforms', 40),        # currently 9
    ('/stores', 45),          # currently 13
    ('/cash', 40),            # currently 11
    ('/cash/cash-sales/2026/7', 40),   # currently 13 (14 once a Shopify CSV is loaded)
    ('/cards/review', 45),    # currently 13
]


@pytest.mark.parametrize('url,ceiling', PAGE_CEILINGS)
def test_list_pages_do_not_scale_queries_per_row(client, url, ceiling):
    client.get(url)                       # warm any caches
    with _Counter() as c:
        r = client.get(url)
    assert r.status_code == 200
    assert c.n <= ceiling, (
        f'{url} issued {c.n} statements (ceiling {ceiling}). Something is very '
        f'likely querying once per row again — look for a call inside a loop '
        f'over the page\'s rows, and use the batched form instead.')


def test_cash_overview_batching_matches_per_store_computation(conn):
    """The /cash prefetch replaced five statements per store with four total.

    It is money — the opening float carries forward across months — so this
    recomputes the same figures the per-store helpers produce and demands they
    agree exactly, including the January case where the prior month is in the
    previous YEAR, and a mid-month start where entries before it must count.
    """
    from northwind.services import money

    def per_store(start, end, scope):
        """The original implementation, expressed through the unchanged helpers."""
        c = db.get_db()
        try:
            out = []
            for store in scope:
                opening = db._recon_balance_before_cents(c, store, start)
                buckets = {'in': 0, 'expense': 0, 'banked': 0, 'adjust': 0}
                net = count = 0
                for r in db._recon_entries_range(c, store, start, end):
                    count += 1
                    amt = r['amount_cents']
                    net += amt if r['direction'] == 'in' else -amt
                    db._bucket_by_kind(r['kind'], amt, buckets)
                out.append({
                    'store': store, 'opening': money.to_rands(opening),
                    'total_in': money.to_rands(buckets['in']),
                    'total_expense': money.to_rands(buckets['expense']),
                    'total_banked': money.to_rands(buckets['banked']),
                    'total_adjust': money.to_rands(buckets['adjust']),
                    'closing': money.to_rands(opening + net),
                    'entry_count': count})
            out.sort(key=lambda d: d['store'])
            return out
        finally:
            c.close()

    stores = db.get_stores()
    assert stores, 'dev DB has no stores'
    # An explicit opening exercises the "set" branch; the January range exercises
    # the year rollover; the mid-month start exercises month-to-date netting.
    db.set_recon_opening(stores[0], 2026, 7, 1234.56)

    ranges = [('2026-07-01', '2026-07-31'),
              ('2026-01-01', '2026-01-31'),
              ('2026-07-15', '2026-07-20'),
              ('2026-06-10', '2026-08-05'),
              ('2099-01-01', '2099-12-31')]
    scopes = [None, stores[:1], stores[:3], []]

    for start, end in ranges:
        for scope in scopes:
            resolved = stores if scope is None else scope
            assert db.get_recon_overview_range(start, end, scope) == \
                per_store(start, end, resolved), \
                f'batched overview differs from per-store for {start}..{end}'


def _seed_cash_sales_month(client, conn, year, month, n_stores, n_locations):
    """Cash sales for n_stores plus a Shopify upload with n_locations."""
    import io
    sale = conn.execute(
        "SELECT id FROM recon_categories WHERE kind='income' "
        "AND lower(name) LIKE 'cash sale%' LIMIT 1").fetchone()['id']
    stores = db.get_stores()[:n_stores]
    for i, store in enumerate(stores):
        for day in (3, 17):
            conn.execute(
                "INSERT INTO cash_recon_entries (store, entry_date, category_id, description,"
                " direction, amount_cents, note, created_by) "
                "SELECT ?,?,?,name,'in',?,?,'pytest' FROM recon_categories WHERE id=?",
                (store, f'{year}-{month:02d}-{day:02d}', sale, 100000 + i,
                 f'QS-{i}-{day}', sale))
    conn.commit()
    lines = []
    for i in range(n_locations):
        location = f'NORTHWIND QSCALE {year}{month:02d} {i}'
        if i < len(stores):
            db.save_cash_shopify_mapping(location, stores[i])
        lines.append(f'{location},cash,QS{i},2,{100 + i}.00,0,{100 + i}.00\n'.encode())
    payload = (b'POS location name,Payment gateway,Order name,Transactions,Gross payments,'
               b'Refunded payments,Net payments\n' + b''.join(lines))
    r = client.post(f'/cash/cash-sales/{year}/{month}/shopify',
                    data={'shopify_csv': (io.BytesIO(payload), 'qscale.csv')},
                    content_type='multipart/form-data')
    assert r.status_code in (302, 303)


def test_cash_sales_page_does_not_scale_per_store_or_shopify_location(client, conn):
    """The month-end page aggregates in SQL; nothing may query per row.

    Both halves of it are loops waiting to happen — one over the journal's
    stores, one over the upload's POS locations — so this compares a one-store,
    one-location month against a whole-chain one and demands the same count.
    """
    stores = db.get_stores()
    if len(stores) < 5:
        pytest.skip('needs several stores to show the difference')
    _seed_cash_sales_month(client, conn, 2094, 1, 1, 1)
    _seed_cash_sales_month(client, conn, 2094, 2, len(stores), len(stores) + 5)

    counts = []
    for url in ('/cash/cash-sales/2094/1', '/cash/cash-sales/2094/2'):
        client.get(url)                   # warm caches
        with _Counter() as c:
            assert client.get(url).status_code == 200
        counts.append(c.n)
    assert counts[0] == counts[1], (
        f'1 store / 1 location took {counts[0]} statements, '
        f'{len(stores)} stores / {len(stores) + 5} locations took {counts[1]} — '
        f'something is querying per store or per Shopify location again')


def test_cash_overview_uses_a_flat_number_of_queries(conn):
    """Four prefetches regardless of how many stores are in scope."""
    stores = db.get_stores()
    if len(stores) < 4:
        pytest.skip('needs several stores to show the difference')
    with _Counter() as few:
        db.get_recon_overview_range('2026-07-01', '2026-07-31', stores[:2])
    with _Counter() as many:
        db.get_recon_overview_range('2026-07-01', '2026-07-31', stores)
    assert many.n == few.n, (
        f'{len(stores)} stores took {many.n} statements vs {few.n} for 2 — the '
        f'overview is querying per store again')
