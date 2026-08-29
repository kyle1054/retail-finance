"""Tests for the pre-deploy hardening pass on the credit-card section:
- failed receipts are retried by the AI worker (not stuck forever);
- linking a receipt rejects its other open suggestions (no stale mis-link);
- an admin can dismiss a wrong suggestion;
- cc_line_has_receipt / count_cc_receipt_links helpers.
"""
import datetime as dt

from northwind.data import database as db
from northwind.cards.parser import CardSnapshot, StatementLine, classify


def _two_line_card(name, email):
    lines = [
        StatementLine(dt.date(2026, 6, 1), 'SHOP A', -5000,
                      classify('SHOP A', -5000), False, '2026-06-01|shop a|-5000', 0),
        StatementLine(dt.date(2026, 6, 2), 'SHOP B', -7000,
                      classify('SHOP B', -7000), False, '2026-06-02|shop b|-7000', 0),
    ]
    snap = CardSnapshot(name, name.split()[0], dt.date(2026, 6, 1),
                        dt.date(2026, 6, 28), dt.date(2026, 6, 28), None, lines, 0, 't.xlsx')
    cid = db.import_card_snapshot(snap)['card_id']
    sid = db.list_cc_statements(cid)[0]['id']
    line_ids = [l['id'] for l in db.get_cc_statement_lines(sid)]
    return cid, sid, line_ids


def _receipt(cid, sid, email, tag):
    return db.add_cc_receipt(cid, sid, f'{cid}/x/{tag}.jpg', f'{tag}.jpg',
                             'image/jpeg', email, content_hash=tag)


def test_failed_receipts_are_retried(db_copy):
    cid, sid, _ = _two_line_card('ZZZ Retry', 'retry@test.co')
    rid = _receipt(cid, sid, 'retry@test.co', 'failme')
    db.set_cc_receipt_ai_status(rid, 'failed')
    pending_ids = [r['id'] for r in db.list_cc_receipts_pending_ai(statement_id=sid)]
    assert rid in pending_ids  # 'failed' is transient → retried
    db.set_cc_receipt_ai_status(rid, 'unreadable')
    pending_ids = [r['id'] for r in db.list_cc_receipts_pending_ai(statement_id=sid)]
    assert rid not in pending_ids  # 'unreadable' is terminal → not retried


def test_link_rejects_other_open_suggestions(db_copy):
    cid, sid, (a, b) = _two_line_card('ZZZ Link', 'link@test.co')
    rid = _receipt(cid, sid, 'link@test.co', 'onefile')
    db.add_cc_suggestion(a, rid, 0.8)
    db.add_cc_suggestion(b, rid, 0.7)
    db.link_cc_receipt(rid, a)  # commit the receipt to line A
    open_line_ids = {s['line_id'] for s in db.list_cc_suggestions_for_statement(sid)}
    # A is hidden (already linked) and B was rejected → no confirmable suggestions.
    assert open_line_ids == set()


def test_reject_cc_suggestion(db_copy):
    cid, sid, (a, _) = _two_line_card('ZZZ Reject', 'reject@test.co')
    rid = _receipt(cid, sid, 'reject@test.co', 'bad')
    db.add_cc_suggestion(a, rid, 0.6)
    assert db.list_cc_suggestions_for_statement(sid)  # visible before dismiss
    assert db.reject_cc_suggestion(
        db.list_cc_suggestions_for_statement(sid)[0]['id']) == cid
    assert db.list_cc_suggestions_for_statement(sid) == []


def test_line_receipt_helpers(db_copy):
    cid, sid, (a, b) = _two_line_card('ZZZ Helpers', 'help@test.co')
    rid = _receipt(cid, sid, 'help@test.co', 'r1')
    assert db.cc_line_has_receipt(a) is False
    db.link_cc_receipt(rid, a)
    assert db.cc_line_has_receipt(a) is True
    assert db.cc_line_has_receipt(b) is False
    assert db.count_cc_receipt_links(rid) == 1
    db.link_cc_receipt(rid, b)
    assert db.count_cc_receipt_links(rid) == 2


def test_ai_auto_link_refuses_an_already_covered_line(db_copy):
    cid, sid, (line_id, _) = _two_line_card('ZZZ Atomic Link', 'atomic@test.co')
    first = _receipt(cid, sid, 'atomic@test.co', 'atomic-first')
    second = _receipt(cid, sid, 'atomic@test.co', 'atomic-second')
    assert db.auto_link_cc_receipt_if_uncovered(first, line_id) is True
    assert db.auto_link_cc_receipt_if_uncovered(second, line_id) is False
    assert db.count_cc_receipt_links(first) == 1
    assert db.count_cc_receipt_links(second) == 0


def test_confirmed_suggestion_not_downgraded(db_copy):
    cid, sid, (a, _) = _two_line_card('ZZZ NoDown', 'nodown@test.co')
    rid = _receipt(cid, sid, 'nodown@test.co', 'c1')
    db.add_cc_suggestion(a, rid, 0.9, status='confirmed')
    db.add_cc_suggestion(a, rid, 0.5)  # a later re-run must NOT downgrade it
    c = db.get_db()
    try:
        status = c.execute(
            "SELECT status FROM cc_line_receipt_suggestions WHERE line_id=? AND receipt_id=?",
            (a, rid)).fetchone()['status']
    finally:
        c.close()
    assert status == 'confirmed'


def test_set_cc_line_location(db_copy):
    cid, sid, (a, _) = _two_line_card('ZZZ Loc', 'loc@test.co')
    db.set_cc_line_location(a, '  Summit City  ')
    assert db.get_cc_line(a)['location'] == 'Summit City'  # trimmed
    db.set_cc_line_location(a, '')
    assert db.get_cc_line(a)['location'] is None  # blank clears


def test_ai_receipt_claim_is_exclusive_and_stale_claim_retries(db_copy, conn):
    cid, sid, _ = _two_line_card('ZZZ Claim', 'claim@test.co')
    rid = _receipt(cid, sid, 'claim@test.co', 'claim-one')
    first = db.claim_cc_receipts_pending_ai(statement_id=sid)
    assert [r['id'] for r in first] == [rid]
    assert db.claim_cc_receipts_pending_ai(statement_id=sid) == []

    conn.execute(
        "UPDATE cc_receipts SET ai_processed_at=datetime('now', '-31 minutes') WHERE id=?",
        (rid,))
    conn.commit()
    retried = db.claim_cc_receipts_pending_ai(statement_id=sid, stale_minutes=30)
    assert [r['id'] for r in retried] == [rid]


def test_ai_coding_rationale_is_scrubbed_at_db_boundary(db_copy):
    _, _, (line_id, _) = _two_line_card('ZZZ Scrub Rationale', 'scrub@test.co')
    db.set_cc_line_ai_coding(
        line_id, '6000', 'Travel', 'medium', True,
        'Email person@example.com phone 082 123 4567 card 4111111111111111', 'ai')
    saved = db.get_cc_line(line_id)['ai_rationale']
    assert 'person@example.com' not in saved
    assert '082 123 4567' not in saved
    assert '4111111111111111' not in saved
