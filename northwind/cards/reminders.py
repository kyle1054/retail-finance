"""Receipt-chase emails to cardholders.

One email per cardholder listing every transaction on their card that still
needs something, plus a link straight to their portal page. Sending is always
explicit — an admin presses the button on the card page. Nothing in here runs on
a schedule.

The chase list comes from db.list_cc_reminder_lines(), which shares its WHERE
clause with the portal's own counter, so the email and the page the link opens
can never disagree about what is outstanding.
"""
import os

from flask import render_template, url_for

from northwind.data import database as db
from northwind.services import mailer

# Absolute link for the button. url_for(_external=True) is right when an admin
# is pressing the button (there is a request to take the host from); the env var
# is the escape hatch for a future scheduled send, which has no request.
PUBLIC_URL = os.environ.get('NW_PUBLIC_URL', '').strip().rstrip('/')

def portal_url(card_id):
    """Deep link to this card's portal page, absolute so it works in mail."""
    if PUBLIC_URL:
        return '%s/portal/cards/%d' % (PUBLIC_URL, card_id)
    return url_for('cc_portal_card', card_id=card_id, _external=True)

def build_context(card, recipient, recipient_name=None):
    """Only the identity and secure portal link belong in the email."""
    return {
        'card_label': card['display_name'] or card['card_name'],
        'recipient': recipient,
        'recipient_name': recipient_name,
        'portal_url': portal_url(card['id']),
    }


def build_text(context):
    """Plain-text twin with the same short, human message."""
    name = (' ' + context['recipient_name']) if context['recipient_name'] else ''
    return '\n'.join([
        'Hi%s,' % name,
        '',
        'The transactions for your %s company card have been updated.'
        % context['card_label'],
        '',
        'When you have a moment, please provide any outstanding receipts or VAT invoices.',
        'The portal will show you if anything else is needed.',
        '',
        'Open card portal: %s' % context['portal_url'],
        '',
        'Thank you,',
        'Finance',
        '',
        "If you need help or see a transaction that isn't yours, reply to this email.",
    ])


def build(card, lines, recipient, recipient_name=None):
    """(subject, html, text) for one cardholder. No sending, so this is testable."""
    del lines  # Used by send_for_card to decide whether to send, not exposed here.
    context = build_context(card, recipient, recipient_name)
    subject = 'Your %s card transactions have been updated' % context['card_label']
    return subject, render_template('email/cc_receipt_reminder.html', **context), \
        build_text(context)


def send_for_card(card_id, only_email=None):
    """Email every cardholder on this card. Returns a per-recipient report.

    Never raises for a single bad address: one cardholder with a typo'd email
    must not stop the rest of the card's people being chased. The caller shows
    the report and the admin can retry just the failures.
    """
    card = db.get_cc_card(card_id)
    if not card:
        raise ValueError('No such card.')

    lines = db.list_cc_reminder_lines(card_id)
    users = db.list_cc_card_users(card_id)
    if only_email:
        wanted = only_email.strip().lower()
        users = [u for u in users if (u['email'] or '').strip().lower() == wanted]

    report = {'card': card['display_name'] or card['card_name'],
              'outstanding': len(lines), 'sent': [], 'failed': [], 'skipped': []}
    if not lines:
        report['skipped'] = [u['email'] for u in users]
        return report
    if not users:
        return report

    for user in users:
        address = (user['email'] or '').strip()
        if not address:
            continue
        try:
            subject, html, text = build(card, lines, address, user['name'])
            mailer.send(address, subject, html, text=text)
            report['sent'].append(address)
        except mailer.MailError as exc:
            report['failed'].append((address, str(exc)))
    return report
