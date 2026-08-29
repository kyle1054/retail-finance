"""Migration 0046 — Shopify comparison tables + the seeded finance aliases.

Two things can silently rot here. A seeded name can stop matching `stores` (a
store rename leaves the mapping pointing at nothing, which is exactly the
"phantom comparison row" the page now has to defend against), and a re-run of
`up()` — a restored backup, a re-applied version — can overwrite an alias an
admin has since corrected on the Xero setup page.
"""
import importlib.util
import os
import sqlite3

from northwind.data import database as db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_migration():
    path = os.path.join(ROOT, 'migrations', '0046_cash_shopify_comparison.py')
    spec = importlib.util.spec_from_file_location('mig0046', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_0046_seeds_only_names_that_are_real_stores(db_copy):
    """Every seeded target must resolve, or it ships a dead mapping on day one."""
    mod = _load_migration()
    stores = set(db.get_stores())
    assert not {target for _, target in mod.MAPPINGS} - stores
    assert not set(mod.JOURNAL_LABELS) - stores
    # And no Shopify location is seeded twice under a different target.
    locations = [loc for loc, _ in mod.MAPPINGS]
    assert len(locations) == len({loc.casefold() for loc in locations})


def _fresh_db(mod):
    """A pre-0046 database: `stores` without the cash_sales_label column."""
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE stores (id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
    names = sorted({t for _, t in mod.MAPPINGS} | set(mod.JOURNAL_LABELS))
    conn.executemany("INSERT INTO stores (name) VALUES (?)", [(n,) for n in names])
    return conn


def test_0046_rerun_is_a_no_op_that_keeps_admin_edits():
    mod = _load_migration()
    conn = _fresh_db(mod)

    def label(store):
        return conn.execute("SELECT cash_sales_label FROM stores WHERE name=?",
                            (store,)).fetchone()[0]

    def mapped(location):
        row = conn.execute("SELECT store FROM cash_shopify_store_mappings "
                           "WHERE shopify_location=?", (location,)).fetchone()
        return row[0] if row else None

    def counts():
        return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
            'cash_shopify_store_mappings', 'cash_shopify_uploads',
            'cash_shopify_rows', 'cash_sales_variance_reasons'))

    mod.up(conn)
    assert label('Parkview Mall') == 'Parkview Park'
    assert mapped('NORTHWIND Parkview') == 'Parkview Mall'
    seeded = counts()
    assert seeded[0] == len(mod.MAPPINGS)

    # An admin corrects both on the Xero setup page.
    conn.execute("UPDATE stores SET cash_sales_label='Parkview (finance)' WHERE name='Parkview Mall'")
    conn.execute("UPDATE cash_shopify_store_mappings SET store='Riverbend' "
                 "WHERE shopify_location='NORTHWIND Parkview'")

    mod.up(conn)                       # must not raise, must not clobber

    assert label('Parkview Mall') == 'Parkview (finance)'
    assert mapped('NORTHWIND Parkview') == 'Riverbend'
    assert counts() == seeded
    # A store the migration never names keeps its untouched NULL label.
    assert label('Riverbend') is None
    conn.close()
