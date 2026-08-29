"""Tests for hard-deleting a credit card (full cascade) and for the cardholder
login being removed once they have no card access left (receipts kept)."""
import datetime as dt

from northwind.data import database as db
from northwind.cards.parser import CardSnapshot, StatementLine, classify


def _make_card(name, email):
    line = StatementLine(dt.date(2026, 6, 1), 'TEST SHOP', -5000,
                         classify('TEST SHOP', -5000), False,
                         '2026-06-01|test shop|-5000', 0)
    snap = CardSnapshot(name, name.split()[0], dt.date(2026, 6, 1),
                        dt.date(2026, 6, 28), dt.date(2026, 6, 28), None, [line], 0, 't.xlsx')
    cid = db.import_card_snapshot(snap)['card_id']
    sid = db.list_cc_statements(cid)[0]['id']
    lid = db.get_cc_statement_lines(sid)[0]['id']
    db.set_cc_user_password(email, 'hash')
    db.add_cc_card_user(cid, email, 'Test', 'role')
    return cid, sid, lid


def test_delete_cc_card_cascades_everything(db_copy):
    cid, sid, lid = _make_card('ZZZ Delete Card', 'zzdelete@test.co')
    rid = db.add_cc_receipt(cid, sid, f'{cid}/x/test.jpg', 'test.jpg', 'image/jpeg',
                            'zzdelete@test.co', content_hash='deadbeef')
    db.add_cc_suggestion(lid, rid, 0.9)
    assert db.get_cc_card(cid) is not None

    files = db.delete_cc_card(cid)
    assert f'{cid}/x/test.jpg' in files          # caller told which files to remove
    assert db.get_cc_card(cid) is None

    c = db.get_db()
    try:
        for tbl, col in (('cc_lines', 'card_id'), ('cc_receipts', 'card_id'),
                         ('cc_statements', 'card_id'), ('cc_card_users', 'card_id')):
            assert c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {col}=?", (cid,)).fetchone()[0] == 0, tbl
        assert c.execute("SELECT COUNT(*) FROM cc_line_receipt_suggestions WHERE line_id=?",
                         (lid,)).fetchone()[0] == 0
    finally:
        c.close()
    # login removed — it only had this card (credential now lives in `users`)
    assert db.get_cc_user('zzdelete@test.co') is None


def test_delete_user_drops_login_only_when_last_card(db_copy):
    cid, _, _ = _make_card('ZZZ User Card', 'zzuser@test.co')
    uid = [u for u in db.list_cc_card_users(cid) if u['email'] == 'zzuser@test.co'][0]['id']

    db.delete_cc_card_user(uid)

    c = db.get_db()
    try:
        assert c.execute("SELECT COUNT(*) FROM cc_card_users WHERE id=?", (uid,)).fetchone()[0] == 0
    finally:
        c.close()
    assert db.get_cc_user('zzuser@test.co') is None  # last card -> login gone
    db.delete_cc_card(cid)  # cleanup


def test_receipt_delete_keeps_db_consistent_when_storage_fails(client, monkeypatch):
    """A transient R2 failure may leak a blob, but must not leave a dead DB row."""
    from northwind.services import storage
    cid, sid, _ = _make_card('ZZZ Delete Receipt', 'zzreceipt@test.co')
    rid = db.add_cc_receipt(cid, sid, f'{cid}/x/fail.pdf', 'fail.pdf',
                            'application/pdf', 'zzreceipt@test.co', content_hash='delete-fail')

    def fail_delete(_path):
        raise RuntimeError('simulated R2 outage')

    monkeypatch.setattr(storage, 'delete', fail_delete)
    response = client.post(f'/cards/{cid}/receipts/{rid}/delete')
    assert response.status_code == 302
    assert db.get_cc_receipt(rid) is None
    db.delete_cc_card(cid)
