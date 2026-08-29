"""Credit-card statements, receipts and AI coding.

Statements go in through ``db.import_card_snapshot`` — the same importer the
xlsx upload uses — so fingerprints, occurrences and the per-statement uniqueness
the duplicate checker relies on are produced the real way. Receipts are then
attached with ``db.link_cc_receipt``, which keeps every link inside one card and
one statement (migration 0045's scope trigger would refuse otherwise).

These helpers open their own connections, so this generator runs as its own
phase after the caller has committed.
"""
import datetime as dt

from northwind.data import database as db
from northwind.cards.parser import CardSnapshot, StatementLine

from . import names
from .calendar_math import shift

AI_CONFIDENCE = ('high', 'high', 'medium', 'low')


def _lines(card_index, year, month, count):
    """A month of statement lines: mostly spend, with a fee and a transfer."""
    out = []
    for index in range(count):
        if index == count - 2 and names.CARD_FEES:
            reference, category = names.CARD_FEES[index % len(names.CARD_FEES)]
            amount = -(2500 + index * 100)
        elif index == count - 1 and names.CARD_TRANSFERS:
            reference, category = names.CARD_TRANSFERS[0]
            amount = 500000
        else:
            reference, category, _code, _name = names.MERCHANTS[
                (card_index * 3 + index) % len(names.MERCHANTS)]
            amount = -(4500 + ((card_index + index) % 11) * 3300)
        day = min(2 + index * 2, 27)
        out.append(StatementLine(
            line_date=dt.date(year, month, day),
            reference=reference,
            amount_cents=amount,
            category=category,
            # A slice arrives already reconciled in the source report, which is
            # the state that makes a line NOT need a receipt.
            reconciled=(index % 5 == 4),
            fingerprint='%04d%02d-%d-%02d' % (year, month, card_index, index),
            occurrence=0,
        ))
    return out


def seed(conn, profile):
    """Cards, statements, lines, receipts and AI coding.

    `conn` is used only for the direct column updates the app has no writer for
    (the AI coding fields and the merchant memory); everything structural goes
    through db.* so the invariants db_check asserts hold.
    """
    made = {'cards': 0, 'statements': 0, 'lines': 0, 'receipts': 0, 'coded': 0}
    card_ids = []

    for card_index, (card_name, display, email) in enumerate(
            names.CARDS[:profile['cards']]):
        for statement_index in range(profile['statements_per_card']):
            offset = -(profile['statements_per_card'] - statement_index)
            year, month = shift(offset)
            lines = _lines(card_index, year, month, profile['lines_per_statement'])
            snapshot = CardSnapshot(
                card_name=card_name,
                display_name=display,
                period_start=dt.date(year, month, 1),
                period_end=dt.date(year, month, 28),
                as_at=dt.date(year, month, 28),
                statement_balance_cents=sum(l.amount_cents for l in lines),
                lines=lines,
                duplicates_removed_by_xero=0,
                source_filename='card-%04d-%02d.xlsx' % (year, month),
            )
            result = db.import_card_snapshot(snapshot)
            card_id = result['card_id']
            made['statements'] += 1
            made['lines'] += len(lines)
        card_ids.append(card_id)
        made['cards'] += 1
        db.add_cc_card_user(card_id, email, display, 'Primary holder')

    # Receipts: one per statement, linked to the first two spend lines in that
    # same statement, plus one loose receipt per card in the inbox.
    for card_index, card_id in enumerate(card_ids):
        # Oldest statement first, so the LOWEST receipt id on a card belongs to
        # the same statement as the lowest line ids. Migration 0045 forbids a
        # cross-statement link, and the helper some tests use to find "a receipt
        # and a line on the same card" takes the first pair it sees.
        for statement in sorted(db.list_cc_statements(card_id),
                                key=lambda s: (s['year'], s['month'])):
            statement_id = statement['id']
            spend = [l for l in db.get_cc_statement_lines(statement_id)
                     if l['category'] == 'spend']
            if not spend:
                continue
            receipt_id = db.add_cc_receipt(
                card_id, statement_id,
                '%d/%d/receipt-%d.pdf' % (card_id, statement_id, spend[0]['id']),
                'receipt-%d.pdf' % spend[0]['id'], 'application/pdf',
                'seed', content_hash='seed-%d-%d' % (card_id, statement_id))
            made['receipts'] += 1
            for line in spend[:2]:
                db.link_cc_receipt(receipt_id, line['id'], actor='seed')
            db.set_cc_receipt_ai(
                receipt_id, spend[0]['reference'],
                statement['period_end'], abs(spend[0]['amount_cents']),
                '{"vendor": "%s"}' % spend[0]['reference'], 'verified')

            # A receipt the extractor could not read, so the error paths have data.
            unread = db.add_cc_receipt(
                card_id, statement_id,
                '%d/%d/unreadable.jpg' % (card_id, statement_id),
                'unreadable.jpg', 'image/jpeg', 'seed',
                content_hash='seed-bad-%d-%d' % (card_id, statement_id))
            db.set_cc_receipt_ai_status(unread, 'failed', 'Image too blurry to read')
            made['receipts'] += 1

    # AI coding, reasons and locations, written straight onto the lines.
    merchant_codes = {reference: (code, account)
                      for reference, _cat, code, account in names.MERCHANTS}
    rows = conn.execute(
        "SELECT id, reference, card_id FROM cc_lines WHERE category='spend' "
        "ORDER BY id").fetchall()
    for index, row in enumerate(rows):
        code, account = merchant_codes.get(row['reference'], (None, None))
        if code is None:
            continue
        confidence = AI_CONFIDENCE[index % len(AI_CONFIDENCE)]
        needs_review = 1 if confidence == 'low' else 0
        conn.execute(
            "UPDATE cc_lines SET ai_account_code=?, ai_account_name=?, "
            "ai_confidence=?, ai_rationale=?, ai_needs_review=?, ai_source='ai', "
            "ai_coded_at=datetime('now'), coding_dirty=0 WHERE id=?",
            (code, account, confidence,
             'Merchant descriptor matched the %s account.' % account,
             needs_review, row['id']))
        made['coded'] += 1
        if index % 3 == 0:
            conn.execute(
                "UPDATE cc_lines SET xero_account_code=?, xero_account_name=?, "
                "reason=?, location=? WHERE id=?",
                (code, account, 'Confirmed business expense', 'HQ', row['id']))
        if index % 7 == 0:
            conn.execute(
                "UPDATE cc_lines SET ai_coding_error=? WHERE id=?",
                ('The model returned an account that is not on the chart.',
                 row['id']))

    # Two suggestions the AI proposed but nobody has confirmed yet.
    for line in conn.execute(
            "SELECT l.id, r.id AS receipt_id FROM cc_lines l "
            "JOIN cc_receipts r ON r.statement_id=l.statement_id "
            "WHERE l.category='spend' AND NOT EXISTS ("
            "  SELECT 1 FROM cc_receipt_lines rl WHERE rl.line_id=l.id) "
            "ORDER BY l.id LIMIT 2").fetchall():
        conn.execute(
            "INSERT OR IGNORE INTO cc_line_receipt_suggestions "
            "(line_id, receipt_id, score, status) VALUES (?,?,?,'suggested')",
            (line['id'], line['receipt_id'], 0.82))

    # Merchant memory the app has "learned" for merchants that appear on no
    # statement line anywhere — so the rows exist without changing what the
    # coding pass decides for any line the suite looks at.
    for key, code, account in (
            ('archivebox storage', '6250', 'Warehousing'),
            ('glasshouse florist', '6170', 'Sundry Expenses')):
        conn.execute(
            "INSERT OR IGNORE INTO cc_merchant_map "
            "(merchant_key, account_code, account_name, hits) VALUES (?,?,?,2)",
            (key, code, account))

    if profile['merchant_memory']:
        # Coding memory is LEARNED state, and it changes what the next coding
        # pass does: a remembered brand is applied without asking the model.
        # Shipping it pre-filled is right for a demo (the feature looks alive)
        # and wrong for the test database (it would move behaviour the AI tests
        # assert on), so it is a profile switch rather than a constant.
        for reference, _cat, code, account in names.MERCHANTS:
            conn.execute(
                "INSERT OR IGNORE INTO cc_merchant_map "
                "(merchant_key, account_code, account_name, hits) VALUES (?,?,?,?)",
                (reference.lower(), code, account, 3))

    made['card_ids'] = card_ids
    return made
