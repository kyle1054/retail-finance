"""Receipt-chase emails to cardholders.

No test reaches the transport: mailer._deliver is patched, so what would have
been sent is captured and asserted on instead.
"""
import datetime as dt

import pytest

from northwind.data import database as db
from northwind.cards.parser import CardSnapshot, StatementLine, classify
from northwind.cards import reminders
from northwind.services import mailer


def _card(name='ZZZ Chase Card', references=None):
    """A card with two outstanding spends and one cardholder."""
    references = references or ['CHASE SHOP ONE', 'CHASE SHOP TWO']
    lines = [
        StatementLine(dt.date(2026, 7, 1 + i), ref, -5000 - i,
                      classify(ref, -5000 - i), False, f'{name}|{i}', 0)
        for i, ref in enumerate(references)
    ]
    snap = CardSnapshot(name, name, dt.date(2026, 7, 1), dt.date(2026, 7, 31),
                        dt.date(2026, 7, 31), None, lines, 0, 'chase.xlsx')
    card_id = snap and db.import_card_snapshot(snap)['card_id']
    db.add_cc_card_user(card_id, 'holder@northwind-apparel.example', name='Nadia')
    return db.get_cc_card(card_id)


@pytest.fixture(autouse=True)
def _mailer_ready(monkeypatch):
    monkeypatch.setattr(mailer, 'SENDER', 'admin@northwind-apparel.example')
    monkeypatch.setattr(mailer, 'ALWAYS_BCC', '')
    monkeypatch.setattr(mailer, 'DRY_RUN', False)


@pytest.fixture
def outbox(monkeypatch):
    """Every message send() would have delivered, captured at the seam."""
    sent = []
    monkeypatch.setattr(mailer, '_deliver', lambda message: sent.append(message))
    return sent


# ── The chase list ────────────────────────────────────────────────────────────

def test_chase_list_agrees_with_the_portal_counter(db_copy):
    """The email and the page its link opens must never disagree about what is
    outstanding, or the recipient stops trusting the mail."""
    for card in db.list_cc_cards(active_only=False):
        lines = db.list_cc_reminder_lines(card['id'])
        counted = sum(db.count_cc_outstanding_by_statement(card['id']).values())
        assert len(lines) == counted, card['card_name']


def test_a_settled_transaction_drops_off_the_chase_list(db_copy):
    card = _card('ZZZ Chase Settles')
    lines = db.list_cc_reminder_lines(card['id'])
    assert len(lines) == 2

    first = lines[0]
    statement_id = first['statement_id']
    receipt = db.add_cc_receipt(card['id'], statement_id, f"{card['id']}/r.pdf",
                                'r.pdf', 'application/pdf', 'test')
    db.link_cc_receipt(receipt, first['id'])
    db.set_cc_line_reason(first['id'], 'Client lunch')
    db.set_cc_line_location(first['id'], 'Westgate')

    remaining = db.list_cc_reminder_lines(card['id'])
    assert first['id'] not in {r['id'] for r in remaining}
    assert len(remaining) == 1


# ── The email ─────────────────────────────────────────────────────────────────

def test_build_is_a_simple_update_notice_and_links_to_portal(db_copy, client):
    card = _card('ZZZ Chase Build')
    lines = db.list_cc_reminder_lines(card['id'])

    with client.application.test_request_context('/', base_url='https://northwind.example'):
        subject, html, text = reminders.build(card, lines, 'holder@northwind-apparel.example', 'Nadia')

    assert subject == 'Your ZZZ Chase Build card transactions have been updated'
    assert 'ZZZ Chase Build' in subject
    for reference in ('CHASE SHOP ONE', 'CHASE SHOP TWO'):
        assert reference not in html and reference not in text
    link = 'https://northwind.example/portal/cards/%d' % card['id']
    assert link in html and link in text
    assert 'Hi Nadia' in html
    expected = ('When you have a moment, please open the card portal and provide '
                'any outstanding receipts or VAT invoices.')
    assert expected in html
    assert 'When you have a moment, please provide any outstanding receipts' in text


def test_email_is_light_mobile_friendly_and_action_first(db_copy, client):
    card = _card('ZZZ Chase Design')
    lines = db.list_cc_reminder_lines(card['id'])

    with client.application.test_request_context('/', base_url='https://northwind.example'):
        _, html, _ = reminders.build(card, lines, 'holder@northwind-apparel.example', 'Nadia')

    assert '<meta name="color-scheme" content="light only">' in html
    assert 'Open card portal' in html
    assert 'The portal will show you if anything else is needed.' in html
    assert 'role="presentation"' in html
    assert '<html lang="en">' in html
    assert '<h1 ' in html
    assert '<!--[if mso]>' in html
    # The old four-column statement table squeezed badly on phones.
    assert '<th ' not in html


def test_email_does_not_expose_counts_periods_dates_or_amounts(db_copy, client):
    card = _card('ZZZ Chase Private')
    lines = db.list_cc_reminder_lines(card['id'])

    with client.application.test_request_context('/', base_url='https://northwind.example'):
        _, html, text = reminders.build(card, lines, 'holder@northwind-apparel.example')

    for private_detail in ('July 2026', '2 transactions', '2026-07-01', 'R 50.00'):
        assert private_detail not in html and private_detail not in text


def test_merchant_details_stay_in_the_authenticated_portal(db_copy, client):
    card = _card('ZZZ Chase Escape', references=['TOYS & CO <SCRIPT>', 'PLAIN SHOP'])
    lines = db.list_cc_reminder_lines(card['id'])

    with client.application.test_request_context('/', base_url='https://northwind.example'):
        _, html, _ = reminders.build(card, lines, 'holder@northwind-apparel.example')

    assert 'TOYS' not in html and 'PLAIN SHOP' not in html
    assert '<SCRIPT>' not in html and '&lt;SCRIPT&gt;' not in html


def test_public_url_env_wins_when_there_is_no_request(db_copy, monkeypatch):
    """A future scheduled send has no request to take the host from."""
    monkeypatch.setattr(reminders, 'PUBLIC_URL', 'https://northwind-deductions.example')
    assert reminders.portal_url(7) == 'https://northwind-deductions.example/portal/cards/7'


# ── Sending ───────────────────────────────────────────────────────────────────

def test_send_for_card_mails_each_holder_once(db_copy, client, outbox):
    card = _card('ZZZ Chase Send')
    db.add_cc_card_user(card['id'], 'second@northwind-apparel.example', name='Marcus')

    with client.application.test_request_context('/', base_url='https://northwind.example'):
        report = reminders.send_for_card(card['id'])

    assert sorted(report['sent']) == ['holder@northwind-apparel.example', 'second@northwind-apparel.example']
    assert report['outstanding'] == 2
    assert len(outbox) == 2
    # Separate emails, not one message with everyone on the To: line.
    assert [m['to'] for m in outbox] == [['holder@northwind-apparel.example'], ['second@northwind-apparel.example']]
    assert all(m['text'] for m in outbox), 'a plain-text alternative should be sent'


def test_one_bad_address_does_not_stop_the_rest(db_copy, client, monkeypatch):
    card = _card('ZZZ Chase Partial')
    db.add_cc_card_user(card['id'], 'second@northwind-apparel.example')

    def flaky(message):
        if message['to'] == ['holder@northwind-apparel.example']:
            raise mailer.MailError('The transport rejected that recipient.')

    monkeypatch.setattr(mailer, '_deliver', flaky)
    with client.application.test_request_context('/', base_url='https://northwind.example'):
        report = reminders.send_for_card(card['id'])

    assert report['sent'] == ['second@northwind-apparel.example']
    assert [a for a, _ in report['failed']] == ['holder@northwind-apparel.example']


def test_nothing_outstanding_sends_nothing(db_copy, client, outbox):
    """An email that says "0 transactions" is worse than no email."""
    card = _card('ZZZ Chase Clean')
    for line in db.list_cc_reminder_lines(card['id']):
        db.set_cc_line_xero_reconciled(line['id'], True)

    with client.application.test_request_context('/', base_url='https://northwind.example'):
        report = reminders.send_for_card(card['id'])

    assert report['outstanding'] == 0
    assert report['sent'] == [] and report['skipped'] == ['holder@northwind-apparel.example']
    assert not outbox


def test_only_email_chases_one_person(db_copy, client, outbox):
    card = _card('ZZZ Chase One')
    db.add_cc_card_user(card['id'], 'second@northwind-apparel.example')

    with client.application.test_request_context('/', base_url='https://northwind.example'):
        report = reminders.send_for_card(card['id'], only_email='second@northwind-apparel.example')

    assert report['sent'] == ['second@northwind-apparel.example']
    assert len(outbox) == 1


# ── The route ─────────────────────────────────────────────────────────────────

def test_route_sends_and_reports(db_copy, client, outbox):
    card = _card('ZZZ Chase Route')
    response = client.post('/cards/%d/remind' % card['id'], follow_redirects=True)
    assert response.status_code == 200
    assert len(outbox) == 1
    assert 'holder@northwind-apparel.example' in response.get_data(as_text=True)


def test_route_refuses_while_email_is_unconfigured(db_copy, client, outbox, monkeypatch):
    card = _card('ZZZ Chase Unconfigured')
    monkeypatch.setattr(mailer, 'SENDER', '')
    response = client.post('/cards/%d/remind' % card['id'], follow_redirects=True)
    assert 'NW_MAIL_SENDER' in response.get_data(as_text=True)
    assert not outbox, 'an unconfigured mailer must not compose anything'


def test_route_404s_for_an_unknown_card(db_copy, client):
    assert client.post('/cards/99999999/remind').status_code == 404


def test_reminder_is_super_admin_only():
    """It emails staff, so a scoped admin must not be able to trigger it."""
    from northwind import core
    assert core.admin_endpoint_allowed('cc_card_remind', 'super') is True
    for role in ('retail', 'hq'):
        assert core.admin_endpoint_allowed('cc_card_remind', role) is False


def test_card_page_offers_the_button_with_the_real_count(db_copy, client):
    card = _card('ZZZ Chase Button')
    page = client.get('/cards/%d' % card['id']).get_data(as_text=True)
    assert 'Email everyone the 2 outstanding transactions' in page
    assert '/cards/%d/remind' % card['id'] in page
