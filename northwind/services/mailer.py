"""Outbound email, delivered to the application log.

This build ships no mail provider. `send()` still does everything that was ever
ours to do — validate the configuration, work out the real recipient list, honour
the dry-run valve — and then hands the finished message to `_deliver()`, which
writes a line to the log instead of posting it anywhere. Nothing reaches the
network, so a stray credential in the environment cannot turn a test run into
real mail landing in a cardholder's inbox.

That is a smaller module than a provider client, but it is not a stub: the logic
callers actually depend on was never the HTTP part. An address already on To:
must not also be blind-copied, or the reader gets the same statement twice; the
always-BCC address has to be merged in without duplicating; and the admin page
needs to know precisely what is still unset. All of that lives here.

`_deliver()` is the single seam. A test replaces it to capture what would have
been sent, and a real transport would be added by replacing it too — everything
above it is transport-agnostic already.

Nothing in this module sends on its own: there is no scheduler hook. Callers
trigger sends explicitly.
"""
import logging
import os

log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
# The From address. Deliberately not defaulted: a message logged as coming from
# an address nobody chose is worse than the admin page saying what to set.
SENDER = os.environ.get('NW_MAIL_SENDER', '').strip()

# Optional display name: "Northwind Apparel <admin@…>" rather than a bare address.
SENDER_NAME = os.environ.get('NW_MAIL_SENDER_NAME', '').strip()

# Blind-copy every send to one address, so a statement run stays searchable in
# whatever mailbox finance actually reads.
ALWAYS_BCC = os.environ.get('NW_MAIL_BCC', '').strip()

# A safety valve for wiring up automatic triggers: with this set, send() does
# everything except the delivery, so a new reminder job can be exercised against
# real data and inspected through `last_dry_run`.
DRY_RUN = os.environ.get('NW_MAIL_DRY_RUN', '0') == '1'

# Named in the send summary and on the admin page, so "where did that email go?"
# has an answer that is true.
BACKEND = 'console'

_REQUIRED = (
    ('NW_MAIL_SENDER', lambda: SENDER),
)

# The last dry-run message, so a caller can assert on what WOULD have been sent.
last_dry_run = None


class MailError(RuntimeError):
    """Anything that stopped an email going out, safe to show an admin."""


def missing_config():
    """Names of the env vars still needed to send. Never their values."""
    return [name for name, read in _REQUIRED if not read()]


def is_configured():
    return not missing_config()


def status():
    """Config summary for the admin page. Contains no secret material."""
    missing = missing_config()
    return {
        'configured': not missing,
        'missing': missing,
        'backend': BACKEND,
        'sender': SENDER or None,
        'sender_name': SENDER_NAME or None,
        'always_bcc': ALWAYS_BCC or None,
        'dry_run': DRY_RUN,
    }


def _as_list(value):
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [a.strip() for a in value if a and a.strip()]


def _from_header():
    """'Name <addr>' when a display name is set, else the bare address."""
    return '%s <%s>' % (SENDER_NAME, SENDER) if SENDER_NAME else SENDER


def _deliver(message):
    """Hand one finished message to the transport. Raises MailError on failure.

    The transport here is the log, which cannot fail — but the contract is the
    one a real transport needs, which is why callers still handle MailError.
    The body is not logged: these messages carry staff names and what they owe,
    and a log file is a far less considered place to keep that than the database
    the app already guards.
    """
    log.info('mail (not sent — console backend): from=%s to=%s cc=%s bcc=%s '
             'subject=%r', message['from'], ','.join(message['to']) or '-',
             ','.join(message['cc']) or '-', ','.join(message['bcc']) or '-',
             message['subject'])


def send(to, subject, html, *, text=None, cc=None, bcc=None, reply_to=None):
    """Send one HTML email as SENDER. Raises MailError on any failure.

    Returns a dict describing what was sent — the caller (or a test) can log it
    without having to reconstruct the payload.
    """
    global last_dry_run
    missing = missing_config()
    if missing:
        raise MailError('Email is not configured yet. Missing: %s' % ', '.join(missing))

    recipients = _as_list(to)
    if not recipients:
        raise MailError('No recipient address given.')
    if not (subject or '').strip():
        raise MailError('No subject given.')

    cc_list = _as_list(cc)
    # De-duplicated: an address that is already a recipient must not also be
    # blind-copied, or the reader gets the same statement twice.
    bcc_list = [a for a in _as_list(bcc) + _as_list(ALWAYS_BCC)
                if a not in recipients and a not in cc_list]
    bcc_list = list(dict.fromkeys(bcc_list))

    summary = {'to': recipients, 'cc': cc_list, 'bcc': bcc_list,
               'subject': subject, 'sender': SENDER, 'backend': BACKEND,
               'dry_run': DRY_RUN}
    if DRY_RUN:
        last_dry_run = dict(summary, html=html)
        return summary

    _deliver(dict(summary, html=html, text=text, reply_to=_as_list(reply_to),
                  **{'from': _from_header()}))
    return summary
