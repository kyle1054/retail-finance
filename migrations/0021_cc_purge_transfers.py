"""Purge stored bank-transfer lines from the credit-card ledger.

Card funding / money-in and balance carry-overs (category 'transfer') are not
merchant spend and must not live on the app. Earlier imports stored them (and a
negative "BALANCE TRANSFERRED" was even mislabelled 'spend'); import now skips
them going forward. This one-off cleanup removes the ones already stored, along
with any stray receipt links (transfers never carry a real receipt).
"""


def up(conn):
    # Re-label negative balance carry-overs that were stored as 'spend' so the
    # delete below catches them too.
    conn.execute(
        "UPDATE cc_lines SET category='transfer', needs_receipt=0 "
        "WHERE category='spend' AND reference LIKE '%BALANCE TRANSFERRED%'")
    conn.execute(
        "DELETE FROM cc_receipt_lines WHERE line_id IN "
        "(SELECT id FROM cc_lines WHERE category='transfer')")
    conn.execute("DELETE FROM cc_lines WHERE category='transfer'")
