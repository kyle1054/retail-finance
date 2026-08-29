"""Credit-card workflow, status, bulk-action and accessibility regressions."""
import datetime as dt
import io
import zipfile

from northwind.data import database as db
from northwind.services import storage
from northwind.cards import ai as cc_ai
from werkzeug.security import generate_password_hash
from northwind.cards.parser import CardSnapshot, StatementLine, classify


def _card(name='ZZZ QoL Card'):
    lines = [
        StatementLine(dt.date(2026, 7, 1), 'READY SHOP', -5000,
                      classify('READY SHOP', -5000), False, f'{name}|ready', 0),
        StatementLine(dt.date(2026, 7, 2), 'NOT READY', -7000,
                      classify('NOT READY', -7000), False, f'{name}|not-ready', 0),
    ]
    snap = CardSnapshot(name, 'QoL', dt.date(2026, 7, 1), dt.date(2026, 7, 31),
                        dt.date(2026, 7, 31), None, lines, 0, 'qol.xlsx')
    cid = db.import_card_snapshot(snap)['card_id']
    sid = db.list_cc_statements(cid)[0]['id']
    lids = [r['id'] for r in db.get_cc_statement_lines(sid)]
    return cid, sid, lids


def test_card_tile_task_counts_and_archive(db_copy):
    cid, sid, (ready, other) = _card('ZZZ QoL Counts')
    rid = db.add_cc_receipt(cid, sid, f'{cid}/r.pdf', 'r.pdf', 'application/pdf', 'qol')
    db.link_cc_receipt(rid, ready)
    db.set_cc_line_reason(ready, 'Store supplies')
    db.set_cc_line_ai_coding(other, '6000', 'Marketing', 'high', False, 'reviewed', 'ai')
    db.add_cc_receipt(cid, None, f'{cid}/inbox/i.pdf', 'i.pdf', 'application/pdf', 'qol')
    row = next(c for c in db.list_cc_cards() if c['id'] == cid)
    assert row['ready_to_submit'] == 1
    assert row['coding_missing'] == 2
    assert row['inbox_count'] == 1
    db.set_cc_card_active(cid, False)
    assert cid not in {c['id'] for c in db.list_cc_cards()}
    assert cid in {c['id'] for c in db.list_cc_cards(active_only=False)}


def test_ai_status_retry_and_coding_claim(db_copy, conn):
    cid, sid, (first, _) = _card('ZZZ QoL AI')
    rid = db.add_cc_receipt(cid, sid, f'{cid}/bad.pdf', 'bad.pdf', 'application/pdf', 'qol')
    db.set_cc_receipt_ai_status(rid, 'failed')
    assert db.get_cc_ai_status(cid, sid)['failed'] == 1
    assert db.retry_cc_receipt_ai(cid, sid) == 1
    assert db.get_cc_ai_status(cid, sid)['pending'] == 1

    claimed = db.claim_cc_lines_needing_coding(statement_id=sid)
    assert first in {r['id'] for r in claimed}
    assert db.claim_cc_lines_needing_coding(statement_id=sid) == []
    conn.execute("UPDATE cc_lines SET coding_claimed_at=datetime('now','-31 minutes') "
                 "WHERE statement_id=?", (sid,))
    conn.commit()
    assert db.claim_cc_lines_needing_coding(statement_id=sid, stale_minutes=30)


def test_bulk_actions_are_scoped_and_require_ready_rows(db_copy):
    cid, sid, (ready, incomplete) = _card('ZZZ QoL Bulk')
    rid = db.add_cc_receipt(cid, sid, f'{cid}/ready.pdf', 'ready.pdf',
                            'application/pdf', 'qol')
    db.link_cc_receipt(rid, ready)
    db.set_cc_line_reason(ready, 'Display stock')
    db.set_cc_line_location(ready, 'HQ')
    db.set_cc_lines_submitted([ready], 'qol@test.co')
    db.set_cc_line_ai_coding(ready, '6000', 'Marketing', 'high', False, 'reviewed', 'ai')
    db.set_cc_line_ai_coding(incomplete, '6000', 'Marketing', 'high', False, 'reviewed', 'ai')
    assert db.bulk_accept_cc_ai_accounts(cid, sid, [ready, incomplete, 999999]) == 2
    assert db.bulk_reconcile_cc_lines(cid, sid, [ready, incomplete, 999999]) == 1
    assert db.get_cc_line(ready)['xero_reconciled'] == 1
    assert db.get_cc_line(incomplete)['xero_reconciled'] == 0


def test_card_rename_and_disabled_access_are_visible(client, db_copy):
    cid, sid, _ = _card('ZZZ QoL Access')
    email = 'qol-disabled@test.co'
    db.set_cc_user_password(email, 'hash')
    db.add_cc_card_user(cid, email, 'Disabled', 'holder')
    user = db.get_cc_user(email)
    db.set_user_active(user['id'], False)
    page = client.get(f'/cards/{cid}?statement_id={sid}')
    assert b'disabled' in page.data
    assert b'Enable &amp; reset password' in page.data
    response = client.post(f'/cards/{cid}/rename', data={'display_name': 'Travel Card'})
    assert response.status_code == 302
    assert db.get_cc_card(cid)['display_name'] == 'Travel Card'


def test_card_workspace_prioritizes_reconciliation_and_progressive_bulk_actions(client):
    cid, sid, _ = _card('ZZZ QoL Workspace')
    page = client.get(f'/cards/{cid}?statement_id={sid}')
    html = page.get_data(as_text=True)

    assert page.status_code == 200
    assert 'cc-card.css' in html and 'cc-card-workspace.js' in html
    assert (
        f'/cards/review?cards_present=1&amp;card_id={cid}'
        f'&amp;period=2026-07&amp;status=unreconciled'
    ) in html
    assert 'Open in Xero review' in html
    assert 'Ready for Xero' in html
    assert 'Meets the finance rule' in html
    assert 'Filter by date' in html
    assert 'Select transactions for batch actions' not in html  # JS supplies the quiet state
    assert html.count('data-cc-bulk-action disabled') == 2
    assert 'Xero account <span class="cc-column-note">advisory</span>' in html
    assert 'Reason missing' in html and 'Location missing' in html


def test_card_receipt_previews_only_thumbnail_browser_safe_images(client):
    cid, sid, (png_line, heic_line) = _card('ZZZ QoL Preview Formats')
    # Deliberately wrong legacy MIME metadata: the UI must follow the same
    # validated file extension that the serving route uses.
    png_id = db.add_cc_receipt(
        cid, sid, f'{cid}/receipt.png', 'receipt.png', 'image/heic', 'qol')
    heic_id = db.add_cc_receipt(
        cid, sid, f'{cid}/receipt.HEIC', 'receipt.HEIC', 'image/jpeg', 'qol')
    pdf_id = db.add_cc_receipt(
        cid, sid, f'{cid}/invoice.pdf', 'invoice.pdf',
        'application/octet-stream', 'qol')
    db.link_cc_receipt(png_id, png_line)
    db.link_cc_receipt(heic_id, heic_line)
    db.link_cc_receipt(pdf_id, png_line)

    page = client.get(f'/cards/{cid}?statement_id={sid}')
    html = page.get_data(as_text=True)

    assert page.status_code == 200
    assert f'<img src="/portal/receipts/{png_id}/file"' in html
    assert f'<img src="/portal/receipts/{heic_id}/file"' not in html
    assert f'href="/portal/receipts/{pdf_id}/file"' in html
    assert 'data-preview-kind="image"' in html
    assert 'data-preview-kind="pdf"' in html
    assert 'data-preview-kind="file"' in html
    assert 'bi-file-earmark-arrow-down' in html and 'HEIC' in html
    assert 'aria-label="Open receipt in new tab"' in html
    assert 'cc-card.css' in html and 'cc-card-workspace.js' in html
    assert '<style>\n.cc-prev' not in html
    assert 'window.open(url' not in html


def test_ai_match_suggestion_shows_receipt_preview_before_confirm(client, monkeypatch):
    monkeypatch.setattr(cc_ai, 'FEATURE_ENABLED', True)
    cid, sid, (line_id, _) = _card('ZZZ QoL Suggestion Preview')
    receipt_id = db.add_cc_receipt(
        cid, sid, f'{cid}/suggested.png', 'suggested.png',
        'image/png', 'qol')
    db.add_cc_suggestion(line_id, receipt_id, 0.86)

    html = client.get(
        f'/cards/{cid}?statement_id={sid}').get_data(as_text=True)

    assert 'cc-match-suggestion' in html
    assert f'href="/portal/receipts/{receipt_id}/file"' in html
    assert f'<img src="/portal/receipts/{receipt_id}/file"' in html
    assert 'data-preview-kind="image"' in html
    assert 'Open receipt before confirming' in html
    assert 'Confirm suggested.png for this transaction' in html


def test_admin_vat_invoice_request_blocks_readiness_and_guides_cardholder(client):
    cid, sid, (line_id, _) = _card('ZZZ QoL VAT Invoice Request')
    receipt_id = db.add_cc_receipt(
        cid, sid, f'{cid}/tax-invoice.pdf', 'tax-invoice.pdf',
        'application/pdf', 'qol')
    db.link_cc_receipt(receipt_id, line_id)
    db.set_cc_line_reason(line_id, 'Business travel')
    db.set_cc_line_location(line_id, 'HQ')
    db.set_cc_lines_submitted([line_id], 'holder@test.co')
    assert line_id in db.get_cc_xero_ready_line_ids([line_id])

    response = client.post(
        f'/cards/{cid}/lines/{line_id}/vat-invoice',
        data={'statement_id': sid})

    assert response.status_code == 302
    line = db.get_cc_line(line_id)
    assert line['vat_invoice_required'] == 1
    assert line['vat_invoice_requested_at']
    assert line['vat_invoice_requested_by'] == 'pytest'
    assert line_id not in db.get_cc_ready_line_ids([line_id])
    assert line_id not in db.get_cc_xero_ready_line_ids([line_id])

    admin_html = client.get(
        f'/cards/{cid}?statement_id={sid}').get_data(as_text=True)
    assert 'VAT invoice requested' in admin_html
    assert 'Clear VAT tax invoice request' in admin_html

    portal_html = client.get(
        f'/portal/cards/{cid}?statement_id={sid}').get_data(as_text=True)
    assert 'Finance needs a VAT tax invoice for this transaction' in portal_html
    assert 'showing the company VAT number' in portal_html
    # The row's own status line carries the request; the receipt gallery's "+ Add"
    # is the upload path, so there is no separate "Upload VAT invoice" button.
    assert 'VAT tax invoice requested' in portal_html
    assert 'data-vat="requested"' in portal_html

    client.post(
        f'/cards/{cid}/lines/{line_id}/vat-invoice',
        data={'statement_id': sid})
    assert db.get_cc_line(line_id)['vat_invoice_required'] == 0
    assert line_id in db.get_cc_xero_ready_line_ids([line_id])


def test_shared_identity_survives_capability_removal(db_copy, conn):
    cid, _, _ = _card('ZZZ QoL Identity')
    card_store_email = 'qol-card-store@test.co'
    db.set_cc_user_password(card_store_email, 'hash')
    db.add_cc_card_user(cid, card_store_email)
    store = conn.execute("SELECT name FROM stores ORDER BY name LIMIT 1").fetchone()['name']
    conn.execute("INSERT INTO store_emails(store,email) VALUES (?,?)",
                 (store, card_store_email))
    conn.commit()
    grant = next(u for u in db.list_cc_card_users(cid) if u['email'] == card_store_email)
    db.delete_cc_card_user(grant['id'])
    assert db.get_user(card_store_email) is not None

    admin_card_email = 'qol-admin-card@test.co'
    db.create_admin_user(admin_card_email, 'QoL Dual',
                         generate_password_hash('long-password', method='pbkdf2:sha256'),
                         role='retail')
    db.add_cc_card_user(cid, admin_card_email)
    admin_id = db.get_admin_user(admin_card_email)['id']
    db.delete_admin_user(admin_id)
    assert db.get_user(admin_card_email) is not None
    assert db.cc_card_user_has_access(cid, admin_card_email)


def test_receipt_zip_sanitizes_entry_names(client, monkeypatch):
    cid, sid, _ = _card('ZZZ QoL Zip')
    db.add_cc_receipt(cid, sid, f'{cid}/evil.pdf', '../../outside.pdf',
                      'application/pdf', 'qol')
    monkeypatch.setattr(storage, 'read', lambda _path: b'%PDF-test')
    response = client.get(f'/cards/{cid}/receipts.zip?statement_id={sid}')
    assert response.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(response.data)).namelist()
    assert names and all('..' not in name and '\\' not in name for name in names)
    assert all(name.count('/') == 1 for name in names)


def test_portal_assets_and_accessibility_markup(client):
    cid, sid, _ = _card('ZZZ QoL A11y')
    page = client.get(f'/portal/cards/{cid}')  # super-admin review path
    assert page.status_code == 200
    assert b'cc-portal.js' in page.data and b'cc-portal.css' in page.data
    assert b'role="progressbar"' in page.data
    assert b'role="combobox"' in page.data
    assert b'aria-live="polite"' in page.data
    assert b'aria-modal="true"' in page.data
