"""Receipt drop-off inbox — receipts uploaded ahead of a statement.

Covers the new cardholder-portal routes END-TO-END through the auth gate (the
endpoints must be in CC_PORTAL_ENDPOINTS or a real cardholder is bounced), the
card-scoping (a foreign cardholder is refused), the assign-into-month move, and
the rule that inbox receipts are excluded from AI matching until assigned.
"""
import io
import zipfile
import time
import datetime as dt

import pytest

from northwind.data import database as db
from northwind.services import storage
from northwind.cards.parser import CardSnapshot, StatementLine

PDF = b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n'


def _line(ref, cents, fp):
    return StatementLine(line_date=dt.date(2026, 5, 10), reference=ref,
                         amount_cents=cents, category='spend', reconciled=False,
                         fingerprint=fp, occurrence=0)


def _make_card(name):
    snap = CardSnapshot(
        card_name=name, display_name=name.split()[0],
        period_start=dt.date(2026, 5, 1), period_end=dt.date(2026, 5, 31),
        as_at=dt.date(2026, 5, 31), statement_balance_cents=None,
        lines=[_line('AUTOSTOP', -5000, name + '-a')],
        duplicates_removed_by_xero=0, source_filename='pytest.xlsx')
    return db.import_card_snapshot(snap)['card_id']


def _statement_id(conn, card_id):
    return conn.execute("SELECT id FROM cc_statements WHERE card_id=?",
                        (card_id,)).fetchone()['id']


@pytest.fixture
def cardholder_client(db_copy, monkeypatch):
    import app as a
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False
    # Never touch real receipt storage in tests.
    monkeypatch.setattr(storage, 'save', lambda rel, data: None)
    monkeypatch.setattr(storage, 'delete', lambda rel: None)
    return a.app.test_client()


def _login(client, email):
    if db.get_user(email) is None:
        db.set_cc_user_password(email, 'test-only-password-hash')
    identity = db.get_user(email)
    with client.session_transaction() as s:
        s['cc_user'] = email
        s['uid'] = identity['id']
        s['auth_version'] = identity['auth_version']
        s['cc_last_active'] = time.time()


def test_dropoff_upload_reaches_view_and_stores_in_inbox(cardholder_client, conn):
    """The endpoint is gated: an authorised cardholder must actually reach the
    view (regression for the endpoint missing from CC_PORTAL_ENDPOINTS)."""
    email = 'zzdrop@test.co'
    cid = _make_card('Pytest Dropoff Credit Card')
    db.add_cc_card_user(cid, email, 'Drop', None)
    _login(cardholder_client, email)

    before = db.count_cc_inbox_receipts(cid)
    resp = cardholder_client.post(
        f'/portal/cards/{cid}/inbox/upload',
        data={'receipts': (io.BytesIO(PDF), 'invoice.pdf')},
        content_type='multipart/form-data')
    assert resp.status_code == 302                       # not 302→login, not 403
    assert '/portal/cards/' in resp.headers['Location']
    assert db.count_cc_inbox_receipts(cid) == before + 1
    assert db.list_cc_inbox_receipts(cid)[0]['statement_id'] is None


def test_dropoff_upload_foreign_cardholder_refused(cardholder_client, conn):
    cid = _make_card('Pytest Dropoff Guard Credit Card')
    _login(cardholder_client, 'nobody@test.co')          # no access to this card
    resp = cardholder_client.post(
        f'/portal/cards/{cid}/inbox/upload',
        data={'receipts': (io.BytesIO(PDF), 'x.pdf')},
        content_type='multipart/form-data')
    assert resp.status_code == 403
    assert db.count_cc_inbox_receipts(cid) == 0


def test_dropoff_assign_moves_into_month(cardholder_client, conn):
    email = 'zzassign@test.co'
    cid = _make_card('Pytest Assign Credit Card')
    db.add_cc_card_user(cid, email, 'Assign', None)
    _login(cardholder_client, email)
    sid = _statement_id(conn, cid)
    rid = db.add_cc_receipt(cid, None, f'{cid}/inbox/x.pdf', 'x.pdf',
                            'application/pdf', email, content_hash='drop-assign')
    assert db.count_cc_inbox_receipts(cid) == 1

    resp = cardholder_client.post(
        f'/portal/cards/{cid}/inbox/assign',
        data={'receipt_id': rid, 'statement_id': sid})
    assert resp.status_code == 302
    assert db.count_cc_inbox_receipts(cid) == 0
    assert db.get_cc_receipt(rid)['statement_id'] == sid


def test_dropoff_assign_rejects_foreign_receipt(cardholder_client, conn):
    """A receipt id from another card can't be assigned into my statement."""
    email = 'zzassign2@test.co'
    mine = _make_card('Pytest Assign Mine Credit Card')
    other = _make_card('Pytest Assign Other Credit Card')
    db.add_cc_card_user(mine, email, 'Assign2', None)
    _login(cardholder_client, email)
    sid = _statement_id(conn, mine)
    foreign_rid = db.add_cc_receipt(other, None, f'{other}/inbox/z.pdf', 'z.pdf',
                                    'application/pdf', 'x@test.co', content_hash='drop-foreign')
    resp = cardholder_client.post(
        f'/portal/cards/{mine}/inbox/assign',
        data={'receipt_id': foreign_rid, 'statement_id': sid})
    assert resp.status_code == 302
    assert db.get_cc_receipt(foreign_rid)['statement_id'] is None   # untouched


def test_inbox_receipt_excluded_from_ai_until_assigned(cardholder_client, conn):
    cid = _make_card('Pytest AIskip Credit Card')
    rid = db.add_cc_receipt(cid, None, f'{cid}/inbox/y.pdf', 'y.pdf',
                            'application/pdf', 'x@test.co', content_hash='drop-ai')
    assert rid not in [r['id'] for r in db.list_cc_receipts_pending_ai(2000)]
    db.assign_cc_receipt_to_statement(rid, _statement_id(conn, cid))
    assert rid in [r['id'] for r in db.list_cc_receipts_pending_ai(2000)]


def test_dropoff_card_appears_only_when_it_has_a_job(cardholder_client, conn):
    """The inbox is for receipts with nowhere to go yet. Once a month is loaded,
    "Add receipts" posts straight into it and the AI matches immediately, so
    showing a second uploader here would only cost an extra "Use this month"
    click. It comes back the moment a file is actually waiting."""
    email = 'zzshowdrop@test.co'
    cid = _make_card('Pytest ShowDrop Credit Card')
    db.add_cc_card_user(cid, email, 'Show', None)
    _login(cardholder_client, email)

    # Month loaded, nothing waiting — the single "Add receipts" card is enough.
    page = cardholder_client.get(f'/portal/cards/{cid}')
    assert page.status_code == 200
    assert b'Drop-off inbox' not in page.data
    assert b'Add receipts' in page.data

    # A dropped-off file still needs claiming, so the card returns with it.
    db.add_cc_receipt(cid, None, f'{cid}/inbox/waiting.pdf', 'waiting.pdf',
                      'application/pdf', email, content_hash='drop-show')
    page = cardholder_client.get(f'/portal/cards/{cid}')
    assert b'Drop-off inbox' in page.data
    assert b'Use this month' in page.data


def test_whole_card_receipts_zip_includes_inbox(client, monkeypatch):
    cid = _make_card('Pytest Inbox Zip Credit Card')
    rid = db.add_cc_receipt(cid, None, f'{cid}/inbox/early.pdf', 'early.pdf',
                            'application/pdf', 'zip@test.co', content_hash='zip-inbox')
    monkeypatch.setattr(storage, 'read', lambda rel: PDF)
    response = client.get(f'/cards/{cid}/receipts.zip')
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert f'inbox/{rid}_early.pdf' in archive.namelist()
