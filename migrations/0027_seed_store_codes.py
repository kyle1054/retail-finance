"""Seed each store's Cash In/Out (POS) account code into stores.store_code.

Used as the contra (credit) line on the store-expenses manual journal — the
journal credits this account for the total gross spent so the import balances.
Codes come from whatever the inventory system calls each store. Seeded only where NULL,
so an admin edit on the Xero-setup page is never clobbered. Every fictional store receives an explicit synthetic code.
"""

_POS = {
    'Riverbend': 'SC-117',
    'Westgate': 'SC-127',
    'Kingsway': 'SC-110',
    'Ashford': 'SC-100',
    'Brookfield': 'SC-102',
    'Fairview': 'SC-106',
    'Mill Street': 'SC-112',
    'Crossroads': 'SC-103',
    'Lakeside': 'SC-111',
    'Northgate': 'SC-113',
    'Elmwood': 'SC-105',
    'Sunfield': 'SC-123',
    'Ravine': 'SC-116',
    'Vineyard': 'SC-125',
    'Grand Central Mall': 'SC-107',
    'Highland Mall': 'SC-109',
    'Parkview Mall': 'SC-115',
    'Riverside': 'SC-118',
    'Eastvale': 'SC-104',
    'Stonebridge - Fairhaven': 'SC-121',
    'The Atrium': 'SC-124',
    'Rosewood': 'SC-119',
    'Summit': 'SC-122',
    'Somerton Mall': 'SC-120',
    'Oakvale': 'SC-114',
    'Harbour Point': 'SC-108',
    'Wellstone': 'SC-126',
    'Baymouth': 'SC-101',
    'Woodhaven': 'SC-128',
}


def up(conn):
    for name, code in _POS.items():
        conn.execute(
            "UPDATE stores SET store_code = ? "
            "WHERE name = ? AND (store_code IS NULL OR store_code = '')",
            (code, name))
