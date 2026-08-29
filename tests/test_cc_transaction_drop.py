"""Fail-closed direct receipt uploads and transaction drop-target wiring."""
import datetime as dt
import io
import re
import sqlite3
import time

import pytest

from northwind.data import database as db
from northwind.services import storage
from northwind.cards.parser import CardSnapshot, StatementLine


def _make_card(name, month=5):
    line = StatementLine(
        line_date=dt.date(2026, month, 10), reference='TARGET MERCHANT',
        amount_cents=-12345, category='spend', reconciled=False,
        fingerprint=f'{name}-{month}-target', occurrence=0)
    snapshot = CardSnapshot(
        card_name=name, display_name=name.split()[0],
        period_start=dt.date(2026, month, 1),
        period_end=dt.date(2026, month, 28), as_at=None,
        statement_balance_cents=None, lines=[line],
        duplicates_removed_by_xero=0, source_filename='drop-test.xlsx')
    return db.import_card_snapshot(snapshot)['card_id']


def _ids(conn, card_id):
    row = conn.execute(
        "SELECT statement_id, id AS line_id FROM cc_lines WHERE card_id=? "
        "ORDER BY id LIMIT 1", (card_id,)).fetchone()
    return row['statement_id'], row['line_id']


@pytest.fixture
def cardholder_client(db_copy):
    import app as app_module
    app_module.app.config['TESTING'] = True
    app_module.app.config['WTF_CSRF_ENABLED'] = False
    return app_module.app.test_client()


def _login(client, email):
    if db.get_user(email) is None:
        db.set_cc_user_password(email, 'test-only-password-hash')
    identity = db.get_user(email)
    with client.session_transaction() as sess:
        sess['cc_user'] = email
        sess['uid'] = identity['id']
        sess['auth_version'] = identity['auth_version']
        sess['cc_last_active'] = time.time()


def _target_from_page(client, card_id, statement_id, line_id):
    html = client.get(
        f'/portal/cards/{card_id}?statement_id={statement_id}').get_data(as_text=True)
    match = re.search(
        rf'<form method="post"\s+action="([^"]*statements/{statement_id}/lines/{line_id}/receipts)"'
        rf'.*?<input type="hidden" name="upload_token" value="([^"]+)"',
        html, re.S)
    assert match, 'transaction-scoped upload form/token missing'
    return html, match.group(1), match.group(2)


def _pdf(name='receipt.pdf'):
    return io.BytesIO(b'%PDF-1.4\n%%EOF\n'), name


def test_portal_renders_exact_drop_target_and_confirmation(
        cardholder_client, conn):
    email = 'zzdrop-ui@test.co'
    card_id = _make_card('Pytest Drop UI Credit Card')
    db.add_cc_card_user(card_id, email, 'Drop UI', None)
    _login(cardholder_client, email)
    statement_id, line_id = _ids(conn, card_id)

    html, action, token = _target_from_page(
        cardholder_client, card_id, statement_id, line_id)
    assert action.endswith(
        f'/portal/cards/{card_id}/statements/{statement_id}/lines/{line_id}/receipts')
    assert token
    assert f'data-line-id="{line_id}"' in html
    assert 'Drop receipt on this transaction' in html
    assert 'Attach to this transaction?' in html
    assert 'The files will be attached only to the transaction shown above.' in html


def test_gallery_offers_one_tap_add_without_losing_the_no_js_upload(
        cardholder_client, conn):
    """The receipt gallery's three routes, and the fallback under all of them.

    "Add file" opens the picker directly (`data-pick` -> the input's id) and the
    change handler submits it, so static/cc-portal.css hides that input and the
    Upload button behind `html.cc-attach-js`. That flag is only set once the
    confirmation <dialog> is usable — so the markup those CSS rules hide MUST
    still be in the page, or a browser without <dialog> would have no way to
    attach a receipt at all.
    """
    email = 'zzdrop-tiles@test.co'
    card_id = _make_card('Pytest Drop Tiles Credit Card', month=6)
    db.add_cc_card_user(card_id, email, 'Drop Tiles', None)
    _login(cardholder_client, email)
    statement_id, line_id = _ids(conn, card_id)
    html = cardholder_client.get(
        f'/portal/cards/{card_id}?statement_id={statement_id}').get_data(as_text=True)

    # One tap: the tile names the input it opens, and that input exists.
    assert f'data-pick="pick-{line_id}"' in html
    assert re.search(rf'<input type="file" id="pick-{line_id}"[^>]*cc-pick-input',
                     html, re.S), 'the picker the Add tile targets is missing'
    # The no-JS path the CSS hides is still rendered.
    assert 'Upload here' in html
    # Camera keeps its own separate input (a forced capture hides the gallery).
    assert f'id="cam-{line_id}"' in html


def test_signed_direct_upload_attaches_only_to_echoed_target(
        cardholder_client, conn, monkeypatch):
    email = 'zzdrop-route@test.co'
    card_id = _make_card('Pytest Drop Route Credit Card')
    db.add_cc_card_user(card_id, email, 'Drop Route', None)
    _login(cardholder_client, email)
    statement_id, line_id = _ids(conn, card_id)
    _, action, token = _target_from_page(
        cardholder_client, card_id, statement_id, line_id)
    monkeypatch.setattr(storage, 'save', lambda _path, _data: None)
    monkeypatch.setattr(storage, 'delete', lambda _path: None)

    response = cardholder_client.post(
        action, data={'upload_token': token, 'receipts': _pdf()},
        content_type='multipart/form-data',
        headers={'X-Requested-With': 'fetch'})

    assert response.status_code == 200
    body = response.get_json()
    assert body['ok'] is True
    assert body['target'] == {
        'card_id': card_id, 'statement_id': statement_id, 'line_id': line_id}
    assert db.cc_line_has_receipt(line_id) is True
    assert conn.execute(
        "SELECT COUNT(*) FROM cc_receipt_lines WHERE line_id=?", (line_id,)
    ).fetchone()[0] == 1


def test_target_token_cannot_be_tampered_or_moved_to_another_line(
        cardholder_client, conn, monkeypatch):
    email = 'zzdrop-token@test.co'
    card_id = _make_card('Pytest Drop Token Credit Card')
    db.add_cc_card_user(card_id, email, 'Drop Token', None)
    # Add a second valid spend line inside the same card/statement. This is the
    # dangerous case a card-level ownership check alone cannot distinguish.
    statement_id, first_line = _ids(conn, card_id)
    conn.execute(
        "INSERT INTO cc_lines (statement_id, card_id, line_date, reference, "
        "amount_cents, category, status, fingerprint, occurrence) "
        "VALUES (?, ?, '2026-05-11', 'OTHER MERCHANT', -5000, 'spend', "
        "'outstanding', ?, 0)",
        (statement_id, card_id, f'drop-other-{card_id}'))
    conn.commit()
    other_line = conn.execute(
        "SELECT id FROM cc_lines WHERE card_id=? ORDER BY id DESC LIMIT 1",
        (card_id,)).fetchone()['id']
    _login(cardholder_client, email)
    _, action, token = _target_from_page(
        cardholder_client, card_id, statement_id, first_line)
    monkeypatch.setattr(storage, 'save', lambda _path, _data: None)
    monkeypatch.setattr(storage, 'delete', lambda _path: None)

    moved_action = action.replace(
        f'/lines/{first_line}/receipts', f'/lines/{other_line}/receipts')
    moved = cardholder_client.post(
        moved_action, data={'upload_token': token, 'receipts': _pdf('moved.pdf')},
        content_type='multipart/form-data',
        headers={'X-Requested-With': 'fetch'})
    tampered = cardholder_client.post(
        action, data={'upload_token': token + 'x', 'receipts': _pdf('tampered.pdf')},
        content_type='multipart/form-data',
        headers={'X-Requested-With': 'fetch'})

    assert moved.status_code == 400
    assert tampered.status_code == 400
    assert db.cc_line_has_receipt(first_line) is False
    assert db.cc_line_has_receipt(other_line) is False
    assert db.list_cc_receipts(statement_id) == []


def test_direct_upload_is_all_or_fail_when_link_write_breaks(
        cardholder_client, conn, monkeypatch):
    email = 'zzdrop-atomic@test.co'
    card_id = _make_card('Pytest Drop Atomic Credit Card')
    db.add_cc_card_user(card_id, email, 'Drop Atomic', None)
    _login(cardholder_client, email)
    statement_id, line_id = _ids(conn, card_id)
    _, action, token = _target_from_page(
        cardholder_client, card_id, statement_id, line_id)
    saved = []
    deleted = []
    monkeypatch.setattr(storage, 'save', lambda path, _data: saved.append(path))
    monkeypatch.setattr(storage, 'delete', lambda path: deleted.append(path))

    def fail_link(_conn, _line_id):
        raise RuntimeError('simulated link failure')

    monkeypatch.setattr(db, '_mark_cc_line_coding_dirty', fail_link)
    response = cardholder_client.post(
        action, data={'upload_token': token, 'receipts': _pdf('atomic.pdf')},
        content_type='multipart/form-data',
        headers={'X-Requested-With': 'fetch'})

    assert response.status_code == 400
    assert saved and deleted == saved
    assert db.list_cc_receipts(statement_id) == []
    assert db.cc_line_has_receipt(line_id) is False


def test_duplicate_retry_is_one_receipt_and_one_link(
        cardholder_client, conn, monkeypatch):
    email = 'zzdrop-retry@test.co'
    card_id = _make_card('Pytest Drop Retry Credit Card')
    db.add_cc_card_user(card_id, email, 'Drop Retry', None)
    _login(cardholder_client, email)
    statement_id, line_id = _ids(conn, card_id)
    _, action, token = _target_from_page(
        cardholder_client, card_id, statement_id, line_id)
    monkeypatch.setattr(storage, 'save', lambda _path, _data: None)
    monkeypatch.setattr(storage, 'delete', lambda _path: None)

    for filename in ('first.pdf', 'retry.pdf'):
        response = cardholder_client.post(
            action, data={'upload_token': token, 'receipts': _pdf(filename)},
            content_type='multipart/form-data',
            headers={'X-Requested-With': 'fetch'})
        assert response.status_code == 200

    assert len(db.list_cc_receipts(statement_id)) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM cc_receipt_lines WHERE line_id=?", (line_id,)
    ).fetchone()[0] == 1


def test_database_trigger_rejects_cross_card_receipt_link(conn):
    first = _make_card('Pytest Drop Scope One Credit Card')
    second = _make_card('Pytest Drop Scope Two Credit Card')
    first_statement, _ = _ids(conn, first)
    _, second_line = _ids(conn, second)
    receipt_id = db.add_cc_receipt(
        first, first_statement, f'{first}/test/scope.pdf', 'scope.pdf',
        'application/pdf', 'pytest', content_hash=f'scope-{first}')

    with pytest.raises(sqlite3.IntegrityError, match='share card and statement'):
        conn.execute(
            "INSERT INTO cc_receipt_lines (receipt_id, line_id) VALUES (?, ?)",
            (receipt_id, second_line))
    conn.rollback()


def test_old_month_endpoint_refuses_direct_line_target(
        cardholder_client, conn, monkeypatch):
    email = 'zzdrop-oldpath@test.co'
    card_id = _make_card('Pytest Drop Old Path Credit Card')
    db.add_cc_card_user(card_id, email, 'Drop Old Path', None)
    _login(cardholder_client, email)
    statement_id, line_id = _ids(conn, card_id)
    monkeypatch.setattr(storage, 'save', lambda _path, _data: None)

    response = cardholder_client.post(
        f'/portal/cards/{card_id}/upload',
        data={'statement_id': statement_id, 'line_id': line_id,
              'receipts': _pdf('old.pdf')},
        content_type='multipart/form-data',
        headers={'X-Requested-With': 'fetch'})
    assert response.status_code == 400
    assert db.list_cc_receipts(statement_id) == []
