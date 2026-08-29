"""Per-transaction submit (individual + bulk) for the cardholder portal.

Covers the DB helper (soft submit/undo marker), the route's card-scoping (a
posted line id from another card is ignored), and the cardholder-session gate.
"""
import io
import time
import datetime as dt
from pathlib import Path

import pytest

from northwind.data import database as db
from northwind.services import storage
from northwind.cards.parser import CardSnapshot, StatementLine


def _line(ref, cents, fp):
    return StatementLine(line_date=dt.date(2026, 5, 10), reference=ref,
                         amount_cents=cents, category='spend', reconciled=False,
                         fingerprint=fp, occurrence=0)


def _snapshot(card_name, lines):
    return CardSnapshot(
        card_name=card_name, display_name=card_name.split()[0],
        period_start=dt.date(2026, 5, 1), period_end=dt.date(2026, 5, 31),
        as_at=dt.date(2026, 5, 31), statement_balance_cents=None,
        lines=list(lines), duplicates_removed_by_xero=0, source_filename='pytest.xlsx')


def _make_card(name):
    snap = _snapshot(name, [_line('AUTOSTOP', -5000, name + '-a'),
                            _line('GREENFIELDS', -12345, name + '-b'),
                            _line('CHICKEN CO', -8000, name + '-c')])
    r = db.import_card_snapshot(snap)
    return r['card_id']


def _line_ids(conn, card_id):
    return [row['id'] for row in conn.execute(
        "SELECT id FROM cc_lines WHERE card_id=? ORDER BY id", (card_id,)).fetchall()]


def _statement_id(conn, card_id):
    return conn.execute("SELECT id FROM cc_statements WHERE card_id=?", (card_id,)).fetchone()['id']


def _make_ready(conn, card_id, line_ids):
    """Make two lines submit-ready through both supported paths."""
    sid = _statement_id(conn, card_id)
    db.set_cc_line_personal(line_ids[0], True)
    db.set_cc_line_reason(line_ids[0], 'Personal lunch')
    db.set_cc_line_location(line_ids[0], 'HQ')
    rid = db.add_cc_receipt(card_id, sid, f'{card_id}/test/receipt.jpg', 'receipt.jpg',
                            'image/jpeg', 'pytest', content_hash=f'ready-{card_id}')
    db.link_cc_receipt(rid, line_ids[1])
    db.set_cc_line_reason(line_ids[1], 'Store supplies')
    db.set_cc_line_location(line_ids[1], 'HQ')
    return sid


# ── DB helper ─────────────────────────────────────────────────────────────────

def test_set_submitted_marks_and_undoes(conn):
    cid = _make_card('Pytest Submit1 Credit Card')
    ids = _line_ids(conn, cid)

    assert db.set_cc_lines_submitted(ids[:2], 'sam@test.co') == 2
    rows = {r['id']: r for r in conn.execute(
        "SELECT id, submitted_at, submitted_by FROM cc_lines WHERE card_id=?", (cid,))}
    assert rows[ids[0]]['submitted_at'] and rows[ids[0]]['submitted_by'] == 'sam@test.co'
    assert rows[ids[1]]['submitted_at']
    assert rows[ids[2]]['submitted_at'] is None      # untouched

    # Undo clears the marker again.
    assert db.set_cc_lines_submitted([ids[0]], 'sam@test.co', submitted=False) == 1
    r0 = conn.execute("SELECT submitted_at, submitted_by FROM cc_lines WHERE id=?",
                      (ids[0],)).fetchone()
    assert r0['submitted_at'] is None and r0['submitted_by'] is None


def test_set_submitted_empty_is_noop(conn):
    assert db.set_cc_lines_submitted([], 'x@test.co') == 0


# ── Route: cardholder session, individual + bulk, cross-card scoping ───────────

@pytest.fixture
def cardholder_client(db_copy):
    import app as a
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False
    c = a.app.test_client()
    return c


def _login_cardholder(client, email):
    if db.get_user(email) is None:
        db.set_cc_user_password(email, 'test-only-password-hash')
    identity = db.get_user(email)
    with client.session_transaction() as sess:
        sess['cc_user'] = email
        sess['uid'] = identity['id']
        sess['auth_version'] = identity['auth_version']
        sess['cc_last_active'] = time.time()


def test_portal_offers_list_and_one_at_a_time_views(cardholder_client, conn):
    """List view is the default and points at the first line still needing work;
    focus mode is the "one at a time" view and offers the way back."""
    email = 'zzdesign@test.co'
    card_id = _make_card('Pytest Design Credit Card')
    db.add_cc_card_user(card_id, email, 'ZZ Design', None)
    db.set_cc_user_password(email, 'x')
    _login_cardholder(cardholder_client, email)
    statement_id = _statement_id(conn, card_id)
    first_id = _line_ids(conn, card_id)[0]

    listing = cardholder_client.get(
        f'/portal/cards/{card_id}?statement_id={statement_id}')
    assert listing.status_code == 200
    assert b'One at a time</a>' in listing.data
    assert f'focus={first_id}'.encode() in listing.data
    # Rows arrive collapsed so the page is a scannable list, not 25 open forms.
    assert b'cc-collapsible is-collapsed' in listing.data

    one = cardholder_client.get(
        f'/portal/cards/{card_id}?statement_id={statement_id}&focus={first_id}')
    assert one.status_code == 200
    assert b'List</a>' in one.data
    assert b'cc-collapsible is-collapsed' not in one.data

    # The retired design preview is gone from both.
    assert b'cc-studio.css' not in listing.data
    assert b'design=studio' not in listing.data


def _two_month_card(name):
    """A card whose NEWEST month is fully closed and whose older month still owes
    everything — the shape that used to strand a cardholder on an empty August.

    `as_at=None` on both snapshots matters: an "as at" import is treated as the
    whole truth for the card and would clear the older month's lines.
    """
    def snap(period, lines):
        return CardSnapshot(
            card_name=name, display_name=name.split()[0],
            period_start=period[0], period_end=period[1], as_at=None,
            statement_balance_cents=None, lines=list(lines),
            duplicates_removed_by_xero=0, source_filename='pytest.xlsx')

    def line(day, ref, cents, fp, reconciled=False):
        return StatementLine(line_date=day, reference=ref, amount_cents=cents,
                             category='spend', reconciled=reconciled,
                             fingerprint=fp, occurrence=0)

    old = snap((dt.date(2026, 7, 1), dt.date(2026, 7, 31)),
               [line(dt.date(2026, 7, 4), 'GREENFIELDS', -9900, name + '-jul-a'),
                line(dt.date(2026, 7, 9), 'AUTOSTOP', -4500, name + '-jul-b')])
    # Every August line arrives already reconciled in Xero, so the month is
    # closed off and nothing on it is outstanding.
    new = snap((dt.date(2026, 8, 1), dt.date(2026, 8, 31)),
               [line(dt.date(2026, 8, 3), 'CHICKEN CO', -3000, name + '-aug-a', True)])
    card_id = db.import_card_snapshot(old)['card_id']
    db.import_card_snapshot(new)
    return card_id


def _sid_for(conn, card_id, year, month):
    return conn.execute(
        "SELECT id FROM cc_statements WHERE card_id=? AND year=? AND month=?",
        (card_id, year, month)).fetchone()['id']


def test_portal_lands_on_the_oldest_month_with_work(cardholder_client, conn):
    """Defaulting to the newest statement hid outstanding work: finance loads
    August, and July's transactions vanish unless the cardholder happens to
    change the dropdown."""
    email = 'zzmonth@test.co'
    card_id = _two_month_card('Pytest Month Credit Card')
    db.add_cc_card_user(card_id, email, 'ZZ Month', None)
    db.set_cc_user_password(email, 'x')
    _login_cardholder(cardholder_client, email)

    july = _sid_for(conn, card_id, 2026, 7)
    august = _sid_for(conn, card_id, 2026, 8)
    assert db.count_cc_outstanding_by_statement(card_id) == {july: 2, august: 0}

    landing = cardholder_client.get(f'/portal/cards/{card_id}')
    assert landing.status_code == 200
    assert f'<option value="{july}" selected'.encode() in landing.data
    # The dropdown advertises the work so other months are discoverable too.
    assert b'2 to do' in landing.data

    # An explicit month still wins — this only changes where they arrive.
    chosen = cardholder_client.get(f'/portal/cards/{card_id}?statement_id={august}')
    assert chosen.status_code == 200
    assert f'<option value="{august}" selected'.encode() in chosen.data


def test_portal_falls_back_to_newest_when_nothing_outstanding(
        cardholder_client, conn):
    email = 'zzclear@test.co'
    card_id = _make_card('Pytest Clear Credit Card')
    db.add_cc_card_user(card_id, email, 'ZZ Clear', None)
    db.set_cc_user_password(email, 'x')
    _login_cardholder(cardholder_client, email)

    sid = _statement_id(conn, card_id)
    for line_id in _line_ids(conn, card_id):
        db.set_cc_line_personal(line_id, True)
        db.set_cc_line_reason(line_id, 'Personal')
        db.set_cc_line_location(line_id, 'HQ')
    assert db.count_cc_outstanding_by_statement(card_id) == {sid: 0}

    landing = cardholder_client.get(f'/portal/cards/{card_id}')
    assert landing.status_code == 200
    assert f'<option value="{sid}" selected'.encode() in landing.data
    assert b'to do' not in landing.data


def test_route_submits_and_scopes_to_card(cardholder_client, conn):
    email = 'zzsubmit@test.co'
    mine = _make_card('Pytest Mine Credit Card')
    other = _make_card('Pytest Other Credit Card')
    db.add_cc_card_user(mine, email, 'ZZ Submit', None)
    db.set_cc_user_password(email, 'x')  # a login exists for this email
    _login_cardholder(cardholder_client, email)

    my_ids = _line_ids(conn, mine)
    other_ids = _line_ids(conn, other)
    sid = _make_ready(conn, mine, my_ids)

    # Bulk submit two of my lines + sneak in one of another card's lines.
    resp = cardholder_client.post(
        f'/portal/cards/{mine}/submit-lines',
        data={'statement_id': sid, 'line_id': [my_ids[0], my_ids[1], other_ids[0]]},
        headers={'X-Requested-With': 'fetch'})
    assert resp.status_code == 200
    assert resp.get_json()['count'] == 2          # the foreign line was dropped

    def submitted(lid):
        return conn.execute("SELECT submitted_at FROM cc_lines WHERE id=?",
                            (lid,)).fetchone()['submitted_at'] is not None

    assert submitted(my_ids[0]) and submitted(my_ids[1])
    assert not submitted(my_ids[2])
    assert not submitted(other_ids[0])            # never crossed card boundary

    # Undo one of mine.
    resp = cardholder_client.post(
        f'/portal/cards/{mine}/submit-lines',
        data={'statement_id': sid, 'line_id': my_ids[0], 'undo': '1'},
        headers={'X-Requested-With': 'fetch'})
    assert resp.get_json()['count'] == 1
    assert not submitted(my_ids[0])


def test_route_rejects_foreign_cardholder(cardholder_client, conn):
    mine = _make_card('Pytest Guarded Credit Card')
    ids = _line_ids(conn, mine)
    _login_cardholder(cardholder_client, 'nobody@test.co')  # no access to this card
    resp = cardholder_client.post(
        f'/portal/cards/{mine}/submit-lines',
        data={'statement_id': 0, 'line_id': ids[0]},
        headers={'X-Requested-With': 'fetch'})
    assert resp.status_code == 403
    assert conn.execute("SELECT submitted_at FROM cc_lines WHERE id=?",
                        (ids[0],)).fetchone()['submitted_at'] is None


def test_route_refuses_incomplete_transaction(cardholder_client, conn):
    email = 'zzincomplete@test.co'
    mine = _make_card('Pytest Incomplete Credit Card')
    db.add_cc_card_user(mine, email, 'Incomplete', None)
    _login_cardholder(cardholder_client, email)
    lid = _line_ids(conn, mine)[0]
    sid = _statement_id(conn, mine)

    resp = cardholder_client.post(
        f'/portal/cards/{mine}/submit-lines',
        data={'statement_id': sid, 'line_id': lid},
        headers={'X-Requested-With': 'fetch'})

    assert resp.status_code == 400
    assert resp.get_json()['incomplete_ids'] == [lid]
    assert conn.execute("SELECT submitted_at FROM cc_lines WHERE id=?",
                        (lid,)).fetchone()['submitted_at'] is None


def test_cardholder_can_confirm_own_match_suggestion(cardholder_client, conn):
    email = 'zzsuggest@test.co'
    mine = _make_card('Pytest Suggest Credit Card')
    db.add_cc_card_user(mine, email, 'Suggestion', None)
    _login_cardholder(cardholder_client, email)
    lid = _line_ids(conn, mine)[0]
    sid = _statement_id(conn, mine)
    rid = db.add_cc_receipt(mine, sid, f'{mine}/test/suggest.jpg', 'suggest.jpg',
                            'image/jpeg', email, content_hash='suggest-own')
    db.add_cc_suggestion(lid, rid, 0.88)
    suggestion_id = db.list_cc_suggestions_for_statement(sid)[0]['id']

    page = cardholder_client.get(f'/portal/cards/{mine}?statement_id={sid}')
    assert page.status_code == 200
    assert b'Suggested receipt matches' in page.data

    resp = cardholder_client.post(
        f'/portal/cards/{mine}/suggestions/{suggestion_id}/confirm')
    assert resp.status_code == 302
    assert db.cc_line_has_receipt(lid) is True


def test_submit_ready_requires_location_and_no_open_ai_suggestion(conn):
    cid = _make_card('Pytest Complete Gate Credit Card')
    lid = _line_ids(conn, cid)[0]
    sid = _statement_id(conn, cid)
    rid = db.add_cc_receipt(cid, sid, f'{cid}/test/gate.pdf', 'gate.pdf',
                            'application/pdf', 'pytest', content_hash=f'gate-{cid}')
    db.link_cc_receipt(rid, lid)
    db.set_cc_line_reason(lid, 'Store display materials')

    assert lid not in db.get_cc_ready_line_ids([lid])  # location is mandatory
    db.set_cc_line_location(lid, 'HQ')
    assert lid in db.get_cc_ready_line_ids([lid])

    suggestion_receipt = db.add_cc_receipt(
        cid, sid, f'{cid}/test/suggested.pdf', 'suggested.pdf',
        'application/pdf', 'pytest', content_hash=f'gate-suggestion-{cid}')
    db.add_cc_suggestion(lid, suggestion_receipt, 0.84)
    suggestion = db.list_cc_suggestions_for_statement(sid)[0]
    assert lid not in db.get_cc_ready_line_ids([lid])

    db.reject_cc_suggestion(suggestion['id'])
    assert lid in db.get_cc_ready_line_ids([lid])  # rejected does not block
    db.add_cc_suggestion(lid, suggestion_receipt, 0.95, status='confirmed')
    assert lid in db.get_cc_ready_line_ids([lid])  # confirmed does not block

    conn.execute("UPDATE cc_lines SET status='cleared' WHERE id=?", (lid,))
    conn.commit()
    assert lid not in db.get_cc_ready_line_ids([lid])
    conn.execute("UPDATE cc_lines SET status='outstanding', category='fee' WHERE id=?", (lid,))
    conn.commit()
    assert lid not in db.get_cc_ready_line_ids([lid])


def test_manual_link_of_suggested_pair_clears_effective_ai_block(conn):
    cid = _make_card('Pytest Manual Match Credit Card')
    lid = _line_ids(conn, cid)[0]
    sid = _statement_id(conn, cid)
    rid = db.add_cc_receipt(cid, sid, f'{cid}/test/manual.pdf', 'manual.pdf',
                            'application/pdf', 'pytest', content_hash=f'manual-{cid}')
    db.set_cc_line_reason(lid, 'Display materials')
    db.set_cc_line_location(lid, 'HQ')
    db.add_cc_suggestion(lid, rid, 0.82)
    assert lid not in db.get_cc_ready_line_ids([lid])

    db.link_cc_receipt(rid, lid)
    assert lid in db.get_cc_ready_line_ids([lid])


def test_month_bucket_upload_rejects_every_posted_line_target(
        cardholder_client, conn, monkeypatch):
    email = 'zzdirect-upload@test.co'
    mine = _make_card('Pytest Direct Mine Credit Card')
    other = _make_card('Pytest Direct Other Credit Card')
    db.add_cc_card_user(mine, email, 'Direct Upload', None)
    _login_cardholder(cardholder_client, email)
    mine_sid = _statement_id(conn, mine)
    mine_line = _line_ids(conn, mine)[0]
    foreign_line = _line_ids(conn, other)[0]
    monkeypatch.setattr(storage, 'save', lambda _path, _data: None)
    monkeypatch.setattr(storage, 'delete', lambda _path: None)
    pdf = b'%PDF-1.4\n%%EOF\n'

    response = cardholder_client.post(
        f'/portal/cards/{mine}/upload',
        data={'statement_id': mine_sid, 'line_id': foreign_line,
              'receipts': (io.BytesIO(pdf), 'wrong.pdf')},
        content_type='multipart/form-data',
        headers={'X-Requested-With': 'fetch'})
    assert response.status_code == 400
    assert db.list_cc_receipts(mine_sid) == []

    response = cardholder_client.post(
        f'/portal/cards/{mine}/upload',
        data={'statement_id': mine_sid, 'line_id': mine_line,
              'receipts': (io.BytesIO(pdf), 'right.pdf')},
        content_type='multipart/form-data',
        headers={'X-Requested-With': 'fetch'})
    assert response.status_code == 400
    assert db.list_cc_receipts(mine_sid) == []
    assert db.cc_line_has_receipt(mine_line) is False


def test_obsolete_direct_month_upload_never_creates_an_unmatched_fallback(
        cardholder_client, conn, monkeypatch):
    email = 'zzdirect-partial@test.co'
    cid = _make_card('Pytest Direct Partial Credit Card')
    db.add_cc_card_user(cid, email, 'Direct Partial', None)
    _login_cardholder(cardholder_client, email)
    sid = _statement_id(conn, cid)
    lid = _line_ids(conn, cid)[0]
    monkeypatch.setattr(storage, 'save', lambda _path, _data: None)
    monkeypatch.setattr(storage, 'delete', lambda _path: None)
    response = cardholder_client.post(
        f'/portal/cards/{cid}/upload',
        data={'statement_id': sid, 'line_id': lid,
              'receipts': (io.BytesIO(b'%PDF-1.4\n%%EOF\n'), 'partial.pdf')},
        content_type='multipart/form-data',
        headers={'X-Requested-With': 'fetch'})

    assert response.status_code == 400
    assert len(db.list_cc_receipts(sid)) == 0
    assert db.cc_line_has_receipt(lid) is False


def test_portal_mutations_require_posted_statement_to_match_line(
        cardholder_client, conn):
    email = 'zzstrict-scope@test.co'
    mine = _make_card('Pytest Strict Scope Credit Card')
    other = _make_card('Pytest Strict Other Credit Card')
    db.add_cc_card_user(mine, email, 'Strict Scope', None)
    _login_cardholder(cardholder_client, email)
    lid = _line_ids(conn, mine)[0]
    wrong_sid = _statement_id(conn, other)

    response = cardholder_client.post(
        f'/portal/cards/{mine}/reason',
        data={'statement_id': wrong_sid, 'line_id': lid, 'reason': 'must not save'},
        headers={'X-Requested-With': 'fetch'})

    assert response.status_code == 400
    assert db.get_cc_line(lid)['reason'] in (None, '')


def test_cardholder_typed_reason_replaces_old_value_and_is_rendered_exactly(
        cardholder_client, client, conn):
    """The cardholder's text is authoritative; merchant-history suggestions
    must never replace it after the save or on the next page render."""
    email = 'zzreason-save@test.co'
    cid = _make_card('Pytest Reason Save Credit Card')
    db.add_cc_card_user(cid, email, 'Reason Save', None)
    _login_cardholder(cardholder_client, email)
    sid = _statement_id(conn, cid)
    lid = _line_ids(conn, cid)[0]
    db.set_cc_line_reason(lid, 'VM checklist print')

    typed = 'Rideco to Westgate for visual-merchandising setup'
    response = cardholder_client.post(
        f'/portal/cards/{cid}/reason',
        data={'statement_id': sid, 'line_id': lid, 'reason': typed},
        headers={'X-Requested-With': 'fetch'})

    assert response.status_code == 200
    assert db.get_cc_line(lid)['reason'] == typed

    page = cardholder_client.get(
        f'/portal/cards/{cid}?statement_id={sid}&focus={lid}')
    html = page.get_data(as_text=True)
    assert f'value="{typed}"' in html
    assert 'data-apply-reason=' not in html

    # Finance reads the same cc_lines.reason value in both of its working views.
    card_page = client.get(
        f'/cards/{cid}?statement_id={sid}').get_data(as_text=True)
    review_page = client.get(
        '/cards/review',
        query_string=[('cards_present', '1'), ('card_id', str(cid)),
                      ('period', '2026-05'), ('status', 'unreconciled')]
    ).get_data(as_text=True)
    assert typed in card_page
    assert typed in review_page


def test_reason_autosave_updates_card_in_place_without_reordering():
    """A successful background save must not move/hide the active row or
    re-run filters, which previously jumped the viewport to the page bottom."""
    js = (Path(__file__).parents[1] / 'static' / 'cc-portal.js').read_text()
    start = js.index('window.__ccReplace = function (card)')
    end = js.index('\n } catch (e)', start)
    hook = js[start:end]

    assert 'syncActions(card, true)' in hook
    assert 'place(card)' not in hook
    assert 'refresh()' not in hook
    assert 'apply()' not in hook


def test_focus_mode_suggests_prior_location_but_not_prior_reason(
        cardholder_client, conn):
    email = 'zzfocus@test.co'
    cid = _make_card('Pytest Focus Credit Card')
    db.add_cc_card_user(cid, email, 'Focus', None)
    _login_cardholder(cardholder_client, email)
    sid = _statement_id(conn, cid)
    first, _, current = _line_ids(conn, cid)
    conn.execute(
        "UPDATE cc_lines SET reference='GREENFIELDS SUMMIT 123' WHERE id=?",
        (first,))
    conn.execute(
        "UPDATE cc_lines SET reference='GREENFIELDS ROSEWOOD 456' WHERE id=?",
        (current,))
    conn.commit()
    db.set_cc_line_reason(first, 'Team refreshments')
    db.set_cc_line_location(first, 'HQ')

    page = cardholder_client.get(
        f'/portal/cards/{cid}?statement_id={sid}&focus={current}')
    assert page.status_code == 200
    assert b'All transactions' in page.data          # the focus nav is present
    assert b'Team refreshments' not in page.data
    assert b'Use previous:' in page.data
    assert b'>HQ</span>' in page.data
    assert b'data-apply-reason=' not in page.data
    assert b'name="focus"' in page.data
    assert b'Next missing' in page.data


def test_merchant_location_suggestions_use_only_strictly_earlier_rows(conn):
    """Merchant location memory only ever teaches forwards in time.

    Guards the scoped/limited history read: for each line the suggestion is the
    most recent *strictly earlier* transaction for the same normalised merchant,
    while reasons are never returned as presets.
    """
    from northwind.cards import routes as cc_routes

    cid = _make_card('Pytest Merchant Memory Credit Card')
    oldest, middle, newest = _line_ids(conn, cid)
    # Digits are stripped by normalize_merchant, so all three share one key.
    for lid, ref, day in ((oldest, 'GREENFIELDS 001', '2026-05-01'),
                          (middle, 'GREENFIELDS 002', '2026-05-05'),
                          (newest, 'GREENFIELDS 003', '2026-05-09')):
        conn.execute("UPDATE cc_lines SET reference=?, line_date=? WHERE id=?",
                     (ref, day, lid))
    conn.commit()
    db.set_cc_line_location(oldest, 'HQ')
    db.set_cc_line_location(newest, 'Rosewood')

    lines = conn.execute(
        "SELECT id, line_date, reference FROM cc_lines WHERE card_id=? ORDER BY id",
        (cid,)).fetchall()
    got = cc_routes._merchant_field_suggestions(cid, lines)

    assert got[oldest] == {'reason': None, 'location': None}
    # The middle line must not learn from the later one.
    assert got[middle] == {'reason': None, 'location': 'HQ'}
    assert got[newest] == {'reason': None, 'location': 'HQ'}


def test_merchant_suggestions_handle_unnormalisable_references(conn):
    """A reference that normalises to nothing suggests nothing (and never 500s)."""
    from northwind.cards import routes as cc_routes

    cid = _make_card('Pytest Merchant Blank Credit Card')
    lid = _line_ids(conn, cid)[0]
    conn.execute("UPDATE cc_lines SET reference='4029 8811' WHERE id=?", (lid,))
    conn.commit()

    lines = conn.execute(
        "SELECT id, line_date, reference FROM cc_lines WHERE id=?", (lid,)).fetchall()

    assert cc_routes._merchant_field_suggestions(cid, lines) == {
        lid: {'reason': None, 'location': None}}


def test_portal_labels_pending_and_rejected_ai_states(cardholder_client, conn):
    email = 'zzai-state@test.co'
    cid = _make_card('Pytest AI State Credit Card')
    db.add_cc_card_user(cid, email, 'AI State', None)
    _login_cardholder(cardholder_client, email)
    sid = _statement_id(conn, cid)
    lid = _line_ids(conn, cid)[0]
    rid = db.add_cc_receipt(cid, sid, f'{cid}/test/ai.pdf', 'ai.pdf',
                            'application/pdf', email, content_hash=f'ai-state-{cid}')
    db.add_cc_suggestion(lid, rid, 0.71)
    suggestion = db.list_cc_suggestions_for_statement(sid)[0]

    page = cardholder_client.get(
        f'/portal/cards/{cid}?statement_id={sid}&focus={lid}')
    assert b'Suggested match \xe2\x80\x94 please confirm' in page.data
    db.reject_cc_suggestion(suggestion['id'])
    page = cardholder_client.get(
        f'/portal/cards/{cid}?statement_id={sid}&focus={lid}')
    assert b'AI match rejected \xe2\x80\x94 link manually if needed' in page.data


def test_list_view_actions_do_not_switch_the_cardholder_into_focus_view(
        cardholder_client, conn):
    """`focus` now selects the one-at-a-time VIEW, not just a scroll target.

    Mutations used to force `focus=<line>` into the redirect, which threw a
    list-view cardholder into a single-transaction page every time they marked
    something personal. The edited line must come back as a fragment anchor
    instead, leaving the view mode alone.
    """
    email = 'zzview@test.co'
    card_id = _make_card('Pytest View Credit Card')
    db.add_cc_card_user(card_id, email, 'ZZ View', None)
    db.set_cc_user_password(email, 'x')
    _login_cardholder(cardholder_client, email)
    statement_id = _statement_id(conn, card_id)
    line_id = _line_ids(conn, card_id)[0]

    for path, extra in (
            ('personal', {}),
            ('reason', {'reason': 'Store supplies'}),
            ('location', {'location': 'HQ'}),
    ):
        data = {'statement_id': statement_id, 'line_id': line_id}
        data.update(extra)
        response = cardholder_client.post(f'/portal/cards/{card_id}/{path}', data=data)
        assert response.status_code == 302
        location = response.headers['Location']
        assert 'focus=' not in location, f'{path} dragged the user into focus view'
        assert location.endswith(f'#txn-{line_id}'), \
            f'{path} lost the cardholder\'s place in the list'


def test_focus_view_actions_stay_in_focus_view(cardholder_client, conn):
    """The mirror image: while genuinely in one-at-a-time, the posted `focus`
    field must carry the view through the redirect."""
    email = 'zzviewkeep@test.co'
    card_id = _make_card('Pytest ViewKeep Credit Card')
    db.add_cc_card_user(card_id, email, 'ZZ ViewKeep', None)
    db.set_cc_user_password(email, 'x')
    _login_cardholder(cardholder_client, email)
    statement_id = _statement_id(conn, card_id)
    line_id = _line_ids(conn, card_id)[0]

    response = cardholder_client.post(
        f'/portal/cards/{card_id}/reason',
        data={'statement_id': statement_id, 'line_id': line_id,
              'reason': 'Still focused', 'focus': line_id})
    assert response.status_code == 302
    assert f'focus={line_id}' in response.headers['Location']
    assert '#txn-' not in response.headers['Location']
