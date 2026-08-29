"""Correct four expense-category account codes.

The earlier seed carried placeholder codes for four categories, so their
spend landed in the wrong account on the expenses journal. Each is moved
to the account it should always have used; the exact codes are in _FIXES
below.

Each UPDATE is guarded on the *current* (wrong) code, so it (a) is idempotent —
a second run is a no-op once corrected — and (b) never clobbers a value an admin
has since set by hand on the Xero-setup page.
"""

_FIXES = [
    ('electricity%', '6030', '6380'),
    ('parking%', '6330', '6320'),
    ('petrol%', '6330', '6230'),
    ('staff transport%', '6330', '6400'),
]


def up(conn):
    for name_like, old, new in _FIXES:
        conn.execute(
            "UPDATE recon_categories SET xero_code = ? "
            "WHERE kind = 'expense' AND lower(name) LIKE ? AND xero_code = ?",
            (new, name_like, old))
