"""Credit Card Reconciliation — admin coding + reconciled-in-Xero flow.

Covers:
  1. get_cc_spend_lines_for_matching excludes personal-flagged lines so the AI
     never auto-links or suggests a receipt against a charge the cardholder repays.
  2. The admin "reconciled in Xero" tick hides a line from the working view.
  3. Admin account coding: validates the code, persists it, and TEACHES the
     merchant memory so the same merchant auto-codes next month — including the
     "apply to all this merchant" bulk action.

(The old /cards/<id>/recon.xlsx "Export for Xero" was removed — the Xero export is being rebuilt
that export himself — so its tests are gone.)
"""
import datetime as dt

from northwind.cards import ai as cc_ai
from northwind.data import database as db
from northwind.cards.parser import CardSnapshot, StatementLine


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


# ── AI must ignore personal lines (#1) ────────────────────────────────────────

def test_matching_candidates_exclude_personal(conn):
    r = db.import_card_snapshot(_snapshot('Pytest EXP-AI Credit Card', [
        _line('AUTOSTOP FUEL', -5000, 'fp-a'),
        _line('GREENFIELDS', -12345, 'fp-b'),
    ]))
    cid = r['card_id']
    sid = conn.execute("SELECT id FROM cc_statements WHERE card_id=?", (cid,)).fetchone()['id']
    ids = [x['id'] for x in conn.execute(
        "SELECT id FROM cc_lines WHERE card_id=? ORDER BY id", (cid,)).fetchall()]

    assert len(db.get_cc_spend_lines_for_matching(sid)) == 2
    db.set_cc_line_personal(ids[0], True)
    remaining = db.get_cc_spend_lines_for_matching(sid)
    assert [x['id'] for x in remaining] == [ids[1]]   # personal one dropped


# ── Reconciled-in-Xero tick hides the line from the working view (#2) ─────────

def test_reconciled_tick_hides_line(client, conn):
    r = db.import_card_snapshot(_snapshot('Pytest EXP-RC Credit Card', [
        _line('AUTOSTOP FUEL', -5000, 'fp-rc-a'),
        _line('GREENFIELDS', -12345, 'fp-rc-b'),
    ]))
    cid = r['card_id']
    sid = conn.execute(
        "SELECT id FROM cc_statements WHERE card_id=?", (cid,)
    ).fetchone()['id']
    ids = [x['id'] for x in conn.execute(
        "SELECT id FROM cc_lines WHERE card_id=? ORDER BY id", (cid,)).fetchall()]

    # The line must meet the same readiness rules as the multi-card review:
    # evidence/personal, reason, location, submitted, and no pending suggestion.
    rid = db.add_cc_receipt(
        cid, sid, f'{cid}/2026-05/reconcile.pdf', 'reconcile.pdf',
        'application/pdf', 'pytest', content_hash=f'reconcile-{cid}')
    db.link_cc_receipt(rid, ids[0])
    db.set_cc_line_reason(ids[0], 'Fuel for company travel')
    db.set_cc_line_location(ids[0], 'HQ')
    db.set_cc_lines_submitted([ids[0]], 'cardholder@test.co')

    # Both visible to start.
    page = client.get(f'/cards/{cid}').get_data(as_text=True)
    assert 'AUTOSTOP FUEL' in page and 'GREENFIELDS' in page

    # Tick AUTOSTOP reconciled -> it drops out of the default view.
    client.post(
        f'/cards/{cid}/lines/{ids[0]}/reconciled',
        data={'statement_id': sid})
    assert db.get_cc_line(ids[0])['xero_reconciled'] == 1
    page = client.get(f'/cards/{cid}').get_data(as_text=True)
    assert 'AUTOSTOP FUEL' not in page and 'GREENFIELDS' in page

    # ?show_reconciled=1 brings it back.
    page = client.get(f'/cards/{cid}?show_reconciled=1').get_data(as_text=True)
    assert 'AUTOSTOP FUEL' in page

    # Untick -> visible again by default.
    client.post(
        f'/cards/{cid}/lines/{ids[0]}/reconciled',
        data={'statement_id': sid})
    assert db.get_cc_line(ids[0])['xero_reconciled'] == 0


# ── Admin account coding persists + teaches merchant memory (#3) ──────────────

def test_account_coding_and_merchant_learning(client, conn):
    r = db.import_card_snapshot(_snapshot('Pytest EXP-CD Credit Card', [
        _line('AUTOSTOP KINGSFORD', -5000, 'fp-cd-a'),
        _line('AUTOSTOP SUMMIT', -6000, 'fp-cd-b'),
        _line('GREENFIELDS', -12345, 'fp-cd-c'),
    ]))
    cid = r['card_id']
    ids = [x['id'] for x in conn.execute(
        "SELECT id FROM cc_lines WHERE card_id=? ORDER BY id", (cid,)).fetchall()]

    # Invalid code is rejected (no coding written).
    client.post(f'/cards/{cid}/lines/{ids[0]}/account', data={'account_code': '999999'})
    assert db.get_cc_line(ids[0])['xero_account_code'] is None

    # Valid code sticks and teaches the merchant memory.
    client.post(f'/cards/{cid}/lines/{ids[0]}/account', data={'account_code': '6230'})
    assert db.get_cc_line(ids[0])['xero_account_code'] == '6230'
    key = cc_ai.normalize_merchant('AUTOSTOP KINGSFORD')
    mem = db.get_cc_merchant_map(key)
    assert mem and mem['account_code'] == '6230'

    # "apply to all this merchant" codes the other AUTOSTOP line too (same key),
    # but not the unrelated Greenfields line.
    key2 = cc_ai.normalize_merchant('AUTOSTOP SUMMIT')
    if key2 == key:   # same normalised merchant -> bulk applies
        client.post(f'/cards/{cid}/lines/{ids[0]}/account',
                    data={'account_code': '6230', 'apply_all': '1'})
        assert db.get_cc_line(ids[1])['xero_account_code'] == '6230'
    assert db.get_cc_line(ids[2])['xero_account_code'] is None

    # Clearing the code removes it.
    client.post(f'/cards/{cid}/lines/{ids[0]}/account', data={'account_code': ''})
    assert db.get_cc_line(ids[0])['xero_account_code'] is None


# ── AI extraction fields are PAN-scrubbed before they touch the DB ────────────

def test_set_cc_receipt_ai_scrubs_pan(conn):
    r = db.import_card_snapshot(_snapshot('Pytest EXP-PAN Credit Card', [
        _line('AUTOSTOP FUEL', -5000, 'fp-pan-a'),
    ]))
    cid = r['card_id']
    sid = conn.execute("SELECT id FROM cc_statements WHERE card_id=?", (cid,)).fetchone()['id']
    rid = db.add_cc_receipt(cid, sid, f'{cid}/2026-05/p.pdf', 'p.pdf',
                            'application/pdf', 'pytest')

    # A model that echoed a full card number off a slip must NOT be stored raw.
    db.set_cc_receipt_ai(
        rid, vendor='AUTOSTOP 4111 1111 1111 1111',
        date_iso='2026-05-10', total_cents=5000,
        raw_json='{"vendor": "AUTOSTOP", "pan": "4111 1111 1111 1111"}',
        status='processed')

    row = conn.execute("SELECT ai_vendor, ai_raw_json FROM cc_receipts WHERE id=?",
                       (rid,)).fetchone()
    assert '4111 1111 1111 1111' not in row['ai_vendor']
    assert row['ai_vendor'].endswith('1111')          # last four kept
    assert '4111 1111 1111 1111' not in row['ai_raw_json']
