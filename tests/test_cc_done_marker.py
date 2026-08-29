"""The "done — reconciled in Xero" marker, on both sides of the card.

an admin reconciles a card month in Xero whether or not the cardholder ever
supplied the receipt/reason/location. Ticking a transaction done must therefore
(a) be possible on an incomplete transaction, but only as a recorded, explicit
override, and (b) take that transaction off the cardholder's checklist too —
including every mutation they could otherwise still post against it.
"""
import datetime as dt
import io
import time

import pytest

from northwind.data import database as db
from northwind.services import storage
from northwind.cards.parser import CardSnapshot, StatementLine

PDF = b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n'


def _make_card(name, year=2026, month=7):
    lines = [
        StatementLine(line_date=dt.date(year, month, 10),
                      reference=f'{name} READY MERCHANT', amount_cents=-12345,
                      category='spend', reconciled=False,
                      fingerprint=f'{name}-ready', occurrence=0),
        StatementLine(line_date=dt.date(year, month, 11),
                      reference=f'{name} INCOMPLETE MERCHANT', amount_cents=-6789,
                      category='spend', reconciled=False,
                      fingerprint=f'{name}-incomplete', occurrence=0),
    ]
    snapshot = CardSnapshot(
        card_name=name, display_name=name.replace(' Credit Card', ''),
        period_start=dt.date(year, month, 1), period_end=dt.date(year, month, 28),
        as_at=dt.date(year, month, 28), statement_balance_cents=None,
        lines=lines, duplicates_removed_by_xero=0, source_filename='done-test.xlsx')
    card_id = db.import_card_snapshot(snapshot)['card_id']
    statement_id = db.list_cc_statements(card_id)[0]['id']
    return card_id, statement_id, [l['id'] for l in
                                   db.get_cc_statement_lines(statement_id)]


def _make_ready(card_id, statement_id, line_id, suffix):
    receipt_id = db.add_cc_receipt(
        card_id, statement_id, f'{card_id}/done-{suffix}.pdf',
        f'done-{suffix}.pdf', 'application/pdf', 'pytest',
        content_hash=f'done-{suffix}-{card_id}')
    db.link_cc_receipt(receipt_id, line_id)
    db.set_cc_line_reason(line_id, 'Reviewed business expense')
    db.set_cc_line_location(line_id, 'HQ')
    db.set_cc_lines_submitted([line_id], 'cardholder@test.co')
    return receipt_id


@pytest.fixture
def cardholder_client(db_copy, monkeypatch):
    import app as a
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False
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


def test_override_tick_closes_an_incomplete_transaction_and_records_it(client):
    """The bare POST is still refused; override=1 goes through and is stamped."""
    card_id, statement_id, (_, incomplete_id) = _make_card(
        'ZZZ Done Override Credit Card')

    refused = client.post(f'/cards/{card_id}/lines/{incomplete_id}/reconciled',
                          data={'statement_id': statement_id},
                          headers={'X-Requested-With': 'fetch'})
    assert refused.status_code == 400
    assert db.get_cc_line(incomplete_id)['xero_reconciled'] == 0

    forced = client.post(f'/cards/{card_id}/lines/{incomplete_id}/reconciled',
                         data={'statement_id': statement_id, 'override': '1'},
                         headers={'X-Requested-With': 'fetch'})
    assert forced.status_code == 200
    assert forced.get_json()['forced'] is True
    line = db.get_cc_line(incomplete_id)
    assert line['xero_reconciled'] == 1
    assert line['xero_reconciled_override'] == 1
    assert line['xero_reconciled_by'] == 'pytest'
    assert line['xero_reconciled_at']

    # Unticking reopens it and clears every stamp, including the override.
    reopened = client.post(f'/cards/{card_id}/lines/{incomplete_id}/reconciled',
                           data={'statement_id': statement_id},
                           headers={'X-Requested-With': 'fetch'})
    assert reopened.get_json() == {**reopened.get_json(), 'reconciled': False,
                                   'forced': False}
    line = db.get_cc_line(incomplete_id)
    assert (line['xero_reconciled'], line['xero_reconciled_override'],
            line['xero_reconciled_by'], line['xero_reconciled_at']) == (0, 0, None, None)


def test_a_ready_transaction_is_not_recorded_as_an_override(client):
    card_id, statement_id, (ready_id, _) = _make_card(
        'ZZZ Done Ready Credit Card')
    _make_ready(card_id, statement_id, ready_id, 'ready-not-override')

    # An override flag on a transaction that doesn't need one is ignored.
    response = client.post(f'/cards/{card_id}/lines/{ready_id}/reconciled',
                           data={'statement_id': statement_id, 'override': '1'},
                           headers={'X-Requested-With': 'fetch'})
    assert response.get_json()['forced'] is False
    assert db.get_cc_line(ready_id)['xero_reconciled_override'] == 0


def test_done_transactions_leave_the_cardholder_checklist(cardholder_client, client):
    """The point of the tick: it clears the transaction from the cardholder too."""
    email = 'zzdone@test.co'
    card_id, statement_id, (ready_id, incomplete_id) = _make_card(
        'ZZZ Done Portal Credit Card')
    db.add_cc_card_user(card_id, email, 'Done', None)
    _login(cardholder_client, email)

    page = cardholder_client.get(f'/portal/cards/{card_id}').get_data(as_text=True)
    assert 'READY MERCHANT' in page and 'INCOMPLETE MERCHANT' in page
    assert db.get_cc_portal_task_count(email) >= 2

    client.post(f'/cards/{card_id}/lines/{incomplete_id}/reconciled',
                data={'statement_id': statement_id, 'override': '1'})

    page = cardholder_client.get(f'/portal/cards/{card_id}').get_data(as_text=True)
    assert 'READY MERCHANT' in page
    assert 'INCOMPLETE MERCHANT' not in page

    # ...and the cardholder can no longer edit or attach to what is closed.
    reason = cardholder_client.post(
        f'/portal/cards/{card_id}/reason',
        data={'statement_id': statement_id, 'line_id': incomplete_id,
              'reason': 'Late explanation'})
    assert reason.status_code == 302
    assert db.get_cc_line(incomplete_id)['reason'] is None

    upload = cardholder_client.post(
        f'/portal/cards/{card_id}/upload',
        data={'statement_id': statement_id, 'line_id': incomplete_id,
              'receipts': (io.BytesIO(PDF), 'late.pdf')},
        headers={'X-Requested-With': 'fetch'},
        content_type='multipart/form-data')
    assert upload.status_code == 400

    # submit-lines drops out-of-scope ids the way it always has — the closed
    # transaction is simply not among the lines it submits.
    submit = cardholder_client.post(
        f'/portal/cards/{card_id}/submit-lines',
        data={'statement_id': statement_id, 'line_id': incomplete_id},
        headers={'X-Requested-With': 'fetch'})
    assert submit.get_json()['count'] == 0
    assert db.get_cc_line(incomplete_id)['submitted_at'] is None

    # Reopening it puts the transaction back in front of the cardholder.
    client.post(f'/cards/{card_id}/lines/{incomplete_id}/reconciled',
                data={'statement_id': statement_id})
    page = cardholder_client.get(f'/portal/cards/{card_id}').get_data(as_text=True)
    assert 'INCOMPLETE MERCHANT' in page


def test_closed_transactions_drop_out_of_portal_counts_and_ai_matching(db_copy):
    email = 'zzdonecount@test.co'
    card_id, statement_id, (ready_id, incomplete_id) = _make_card(
        'ZZZ Done Counts Credit Card')
    db.add_cc_card_user(card_id, email, 'Counts', None)
    before = db.get_cc_portal_task_count(email)
    assert {ready_id, incomplete_id} <= {
        l['id'] for l in db.get_cc_spend_lines_for_matching(statement_id)}

    db.set_cc_line_xero_reconciled(incomplete_id, True, actor='pytest', override=True)

    assert db.get_cc_portal_task_count(email) == before - 1
    # A closed transaction is no longer a candidate for a receipt match.
    assert incomplete_id not in {
        l['id'] for l in db.get_cc_spend_lines_for_matching(statement_id)}
    # Nor does it reach the cardholder checklist query.
    assert incomplete_id not in {l['id'] for l in db.get_cc_statement_lines(
        statement_id, needing_receipts_only=True, exclude_reconciled=True)}


def test_force_reconcile_sweep_closes_every_selected_transaction(db_copy):
    card_id, statement_id, (ready_id, incomplete_id) = _make_card(
        'ZZZ Done Sweep Credit Card')
    _make_ready(card_id, statement_id, ready_id, 'sweep')

    # Strict sweep leaves the incomplete row behind (unchanged behaviour)...
    assert db.bulk_reconcile_cc_lines(
        card_id, statement_id, [ready_id, incomplete_id], actor='pytest') == 1
    assert db.get_cc_line(incomplete_id)['xero_reconciled'] == 0
    assert db.get_cc_line(ready_id)['xero_reconciled_by'] == 'pytest'

    # ...the override sweep closes it and reports it as forced.
    done, forced = db.force_reconcile_cc_lines(
        card_id, statement_id, [ready_id, incomplete_id, 999999], actor='pytest')
    assert (done, forced) == (1, 1)
    closed = db.get_cc_line(incomplete_id)
    assert closed['xero_reconciled'] == 1
    assert closed['xero_reconciled_override'] == 1
    # The already-done row keeps its honest, non-override stamp.
    assert db.get_cc_line(ready_id)['xero_reconciled_override'] == 0


def test_force_sweep_cannot_reach_another_cards_transaction(db_copy):
    card_id, statement_id, (_, mine) = _make_card('ZZZ Done Scope A Credit Card')
    other_card, other_statement, (_, theirs) = _make_card(
        'ZZZ Done Scope B Credit Card')

    done, forced = db.force_reconcile_cc_lines(
        card_id, statement_id, [mine, theirs], actor='pytest')

    assert (done, forced) == (1, 1)
    assert db.get_cc_line(theirs)['xero_reconciled'] == 0


def test_bulk_route_override_reports_what_it_closed(client):
    card_id, statement_id, (ready_id, incomplete_id) = _make_card(
        'ZZZ Done Bulk Route Credit Card')
    _make_ready(card_id, statement_id, ready_id, 'bulk-route')

    response = client.post(
        f'/cards/{card_id}/lines/bulk',
        data={'statement_id': statement_id, 'action': 'reconcile',
              'override': '1', 'line_id': [str(ready_id), str(incomplete_id)]},
        follow_redirects=True)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'Marked 2 transactions reconciled in Xero' in body
    assert '1 transaction was still missing' in body
    assert db.get_cc_line(ready_id)['xero_reconciled'] == 1
    assert db.get_cc_line(incomplete_id)['xero_reconciled'] == 1
