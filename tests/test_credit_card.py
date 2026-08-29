"""Credit Card Reconciliation — regression tests for the review fixes.

Covers:
  - import_card_snapshot idempotency + the "receipts outstanding" count
    (must use the cc_receipt_lines join + exclude personal, not the abandoned
    cc_lines.receipt_id column);
  - find_cc_cards_for_email tolerating a None/blank email (the /portal/cards
    500 guard);
  - the receipt upload/serve hardening (content sniff + server-derived,
    never-text/html content types).
"""
import datetime as dt
from werkzeug.security import check_password_hash, generate_password_hash

from northwind.data import database as db
from northwind.cards import routes as rcc
from northwind.cards.parser import CardSnapshot, StatementLine


# ── helpers to build a snapshot without an external ledger workbook ──────────────────

def _line(ref, cents, fp, reconciled=False, category='spend'):
    return StatementLine(
        line_date=dt.date(2026, 5, 10), reference=ref, amount_cents=cents,
        category=category, reconciled=reconciled, fingerprint=fp, occurrence=0)


def _snapshot(card_name, lines):
    return CardSnapshot(
        card_name=card_name, display_name=card_name.split()[0],
        period_start=dt.date(2026, 5, 1), period_end=dt.date(2026, 5, 31),
        as_at=dt.date(2026, 5, 31), statement_balance_cents=None,
        lines=list(lines), duplicates_removed_by_xero=0,
        source_filename='pytest.xlsx')


def _three_spends(card_name):
    return _snapshot(card_name, [
        _line('AUTOSTOP FUEL', -5000, 'fp-m1'),
        _line('GREENFIELDS', -12345, 'fp-m2'),
        _line('CHICKEN CO', -8000, 'fp-m3'),
    ])


# ── import idempotency + outstanding count (#2) ───────────────────────────────

def test_import_is_idempotent_and_counts_outstanding(conn):
    card = 'Pytest ZZZ Credit Card'
    r1 = db.import_card_snapshot(_three_spends(card))
    cid = r1['card_id']
    assert r1['lines_new'] == 3
    assert r1['receipts_outstanding'] == 3      # all three spends need a receipt

    # Re-import the identical snapshot: nothing new, all updated in place.
    r2 = db.import_card_snapshot(_three_spends(card))
    assert r2['lines_new'] == 0
    assert r2['lines_updated'] == 3
    assert r2['receipts_outstanding'] == 3

    line_ids = [row['id'] for row in conn.execute(
        "SELECT id FROM cc_lines WHERE card_id=? ORDER BY id", (cid,)).fetchall()]
    assert len(line_ids) == 3

    # A personal charge no longer counts as needing a receipt.
    db.set_cc_line_personal(line_ids[0], True)
    assert db.import_card_snapshot(_three_spends(card))['receipts_outstanding'] == 2

    # Linking a receipt to a line via the join table covers it too.
    st = conn.execute("SELECT id FROM cc_statements WHERE card_id=?", (cid,)).fetchone()['id']
    rid = db.add_cc_receipt(cid, st, f'{cid}/2026-05/x.pdf', 'x.pdf',
                            'application/pdf', 'pytest')
    db.link_cc_receipt(rid, line_ids[1])
    assert db.import_card_snapshot(_three_spends(card))['receipts_outstanding'] == 1

    # The grid figure agrees with the import figure (they share the predicate).
    grid = {c['id']: c['receipts_outstanding'] for c in db.list_cc_cards()}
    assert grid[cid] == 1


def test_reconciled_line_not_counted_as_needing_receipt(conn):
    """A line reconciled in Xero is DONE — it must drop off the card tile's
    'needs a receipt' count (which used to ignore the reconciled flag, so a
    reconciled-but-receiptless line kept inflating the tile)."""
    r = db.import_card_snapshot(_three_spends('Pytest RECON Credit Card'))
    cid = r['card_id']
    assert {c['id']: c['receipts_outstanding'] for c in db.list_cc_cards()}[cid] == 3
    first = conn.execute("SELECT id FROM cc_lines WHERE card_id=? ORDER BY id", (cid,)).fetchone()['id']
    db.set_cc_line_xero_reconciled(first, True)
    # Tile count AND the importer's own count both exclude the reconciled line.
    assert {c['id']: c['receipts_outstanding'] for c in db.list_cc_cards()}[cid] == 2
    assert db.import_card_snapshot(_three_spends('Pytest RECON Credit Card'))['receipts_outstanding'] == 2


def test_import_buckets_lines_by_own_transaction_month(conn):
    """An 'as at' file carries lines from earlier months. Each must be filed
    under the month it actually occurred (its own line_date), not the file's
    as-at month — so carried-over lines merge with that month, and a later
    upload does not duplicate or misfile them."""
    def _dated(ref, cents, fp, d):
        return StatementLine(line_date=d, reference=ref, amount_cents=cents,
                             category='spend', reconciled=False,
                             fingerprint=fp, occurrence=0)
    card = 'Pytest MULTI Credit Card'
    # File is "as at" July but lists a June-dated carry-over line.
    snap = CardSnapshot(
        card_name=card, display_name='Pytest',
        period_start=dt.date(2026, 7, 1), period_end=dt.date(2026, 7, 9),
        as_at=dt.date(2026, 7, 9), statement_balance_cents=None,
        lines=[_dated('JUNE CARRYOVER', -1000, 'fp-jun', dt.date(2026, 6, 15)),
               _dated('JULY SHOP', -2000, 'fp-jul', dt.date(2026, 7, 2))],
        duplicates_removed_by_xero=0, source_filename='asat.xlsx')
    r = db.import_card_snapshot(snap)
    cid = r['card_id']
    assert r['lines_new'] == 2
    rows = {row['reference']: (row['year'], row['month']) for row in conn.execute(
        "SELECT l.reference, s.year, s.month FROM cc_lines l "
        "JOIN cc_statements s ON s.id=l.statement_id WHERE l.card_id=?", (cid,)).fetchall()}
    assert rows['JUNE CARRYOVER'] == (2026, 6)   # filed under June, not July
    assert rows['JULY SHOP'] == (2026, 7)

    # Re-import must not duplicate the carried-over line.
    db.import_card_snapshot(snap)
    assert conn.execute("SELECT COUNT(*) FROM cc_lines WHERE card_id=?",
                        (cid,)).fetchone()[0] == 2


def test_import_preserves_receipt_link_on_reimport(conn):
    card = 'Pytest YYY Credit Card'
    r = db.import_card_snapshot(_three_spends(card))
    cid = r['card_id']
    st = conn.execute("SELECT id FROM cc_statements WHERE card_id=?", (cid,)).fetchone()['id']
    line_id = conn.execute("SELECT id FROM cc_lines WHERE card_id=? ORDER BY id",
                           (cid,)).fetchone()['id']
    rid = db.add_cc_receipt(cid, st, f'{cid}/2026-05/y.pdf', 'y.pdf',
                            'application/pdf', 'pytest')
    db.link_cc_receipt(rid, line_id)
    db.import_card_snapshot(_three_spends(card))   # re-import must not drop the link
    links = conn.execute("SELECT COUNT(*) c FROM cc_receipt_lines WHERE line_id=?",
                         (line_id,)).fetchone()['c']
    assert links == 1


# ── /portal/cards crash guard (#3) ────────────────────────────────────────────

def test_find_cards_for_email_handles_none_and_blank(db_copy):
    assert db.find_cc_cards_for_email(None) == []
    assert db.find_cc_cards_for_email('') == []
    assert db.find_cc_cards_for_email('   ') == []


# ── upload content sniff + safe serve types (#1/#4) ───────────────────────────

def test_content_sniff_accepts_real_headers():
    assert rcc._content_matches_ext('.png', b'\x89PNG\r\n\x1a\n\x00\x00')
    assert rcc._content_matches_ext('.jpg', b'\xff\xd8\xff\xe0junk')
    assert rcc._content_matches_ext('.pdf', b'%PDF-1.7\n...')
    assert rcc._content_matches_ext('.gif', b'GIF89a....')
    assert rcc._content_matches_ext('.webp', b'RIFF\x00\x00\x00\x00WEBPVP8 ')


def test_content_sniff_rejects_html_disguised_as_image():
    html = b'<html><script>alert(1)</script></html>'
    assert not rcc._content_matches_ext('.png', html)
    assert not rcc._content_matches_ext('.jpg', html)
    assert not rcc._content_matches_ext('.pdf', html)


def test_served_types_are_never_html():
    # The only content types we can ever emit are images / PDF / octet-stream.
    assert 'text/html' not in rcc._SAFE_TYPES.values()
    assert all(not t.startswith('text/') for t in rcc._SAFE_TYPES.values())
    # PDFs and common images render inline; HEIC/HEIF are forced to download.
    assert rcc._SAFE_TYPES['.pdf'] in rcc._INLINE_TYPES
    assert rcc._SAFE_TYPES['.png'] in rcc._INLINE_TYPES
    assert rcc._SAFE_TYPES['.heic'] not in rcc._INLINE_TYPES


def test_pdf_receipt_serves_inline_and_viewer_friendly(client, conn, monkeypatch):
    """A PDF receipt must be served inline AND without 'no-store' (which breaks
    the browser's built-in PDF viewer). Its cache policy must still be private +
    must-revalidate so it can't be re-served to a different user from cache."""
    from northwind.services import storage
    pdf = b'%PDF-1.4\n%stub\n'
    monkeypatch.setattr(storage, 'read', lambda rel: pdf)

    r = db.import_card_snapshot(_three_spends('Pytest PDFVIEW Credit Card'))
    cid = r['card_id']
    st = conn.execute("SELECT id FROM cc_statements WHERE card_id=?", (cid,)).fetchone()['id']
    rid = db.add_cc_receipt(cid, st, f'{cid}/2026-05/inv.pdf', 'inv.pdf',
                            'application/pdf', 'pytest')

    resp = client.get(f'/portal/receipts/{rid}/file')
    assert resp.status_code == 200
    assert resp.headers['Content-Type'] == 'application/pdf'
    assert resp.headers['Content-Disposition'].startswith('inline')   # viewed, not downloaded
    cc = resp.headers.get('Cache-Control', '')
    assert 'no-store' not in cc          # the bug: no-store blanks the PDF viewer
    assert 'no-cache' in cc and 'private' in cc
    csp = resp.headers.get('Content-Security-Policy', '')
    # object-src must allow 'self' for the PDF plugin (not the strict 'none').
    assert "object-src 'self'" in csp
    # Must be framable SAME-ORIGIN so the slide-out preview iframe can show it
    # (DENY / frame-ancestors 'none' blanked it — the reported bug).
    assert resp.headers.get('X-Frame-Options') == 'SAMEORIGIN'
    assert "frame-ancestors 'self'" in csp


def test_heic_receipt_is_downloaded_instead_of_rendered_inline(
        client, conn, monkeypatch):
    from northwind.services import storage
    monkeypatch.setattr(storage, 'read', lambda rel: b'\x00\x00\x00\x18ftypheic')

    result = db.import_card_snapshot(_three_spends('Pytest HEICVIEW Credit Card'))
    cid = result['card_id']
    statement_id = conn.execute(
        "SELECT id FROM cc_statements WHERE card_id=?", (cid,)).fetchone()['id']
    receipt_id = db.add_cc_receipt(
        cid, statement_id, f'{cid}/2026-05/photo.HEIC', 'photo.HEIC',
        'image/heic', 'pytest')

    response = client.get(f'/portal/receipts/{receipt_id}/file')

    assert response.status_code == 200
    assert response.headers['Content-Type'].startswith('image/heic')
    assert response.headers['Content-Disposition'].startswith('attachment')


def test_other_pages_still_no_store(client):
    """The viewer-friendly policy is scoped to the file endpoint only — regular
    authed pages must still be 'no-store'."""
    resp = client.get('/cards')
    assert resp.headers.get('Cache-Control') == 'no-store'


# ── submitted month is no longer locked (edit-lock disabled 2026-07-17) ────────

def test_submitted_month_is_not_locked(db_copy):
    """A submitted statement must stay editable for cardholders — _locked() is
    hard-wired False so uploads/edits are never blocked after submit."""
    import app as a
    with a.app.test_request_context():
        assert rcc._locked({'submitted_at': '2026-05-31T10:00:00'}) is False
        assert rcc._locked(None) is False


def test_password_reset_is_scoped_to_users_of_that_card(client):
    """A crafted reset form must not overwrite an unrelated unified login."""
    own = db.import_card_snapshot(_three_spends('Pytest Reset Own Card'))['card_id']
    other = db.import_card_snapshot(_three_spends('Pytest Reset Other Card'))['card_id']
    email = 'reset-foreign@test.co'
    old_password = 'original-safe-password'
    db.set_cc_user_password(
        email, generate_password_hash(old_password, method='pbkdf2:sha256'))
    db.add_cc_card_user(other, email, 'Foreign', None)
    try:
        response = client.post(f'/cards/{own}/users/reset', data={
            'email': email, 'password': 'attacker-new-password'})
        assert response.status_code == 302
        assert check_password_hash(db.get_user(email)['password_hash'], old_password)
    finally:
        db.delete_cc_card(own)
        db.delete_cc_card(other)
