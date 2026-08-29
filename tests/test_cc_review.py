"""Cross-card Xero review queue and transaction-level readiness."""
import datetime as dt
from pathlib import Path

from northwind.data import database as db
from northwind.cards.parser import CardSnapshot, StatementLine
from tools.csp_inventory import scan_file


def _make_card(name, year=2026, month=7):
    snapshot = CardSnapshot(
        card_name=name,
        display_name=name.replace(' Credit Card', ''),
        period_start=dt.date(year, month, 1),
        period_end=dt.date(year, month, 28),
        as_at=dt.date(year, month, 28),
        statement_balance_cents=None,
        lines=[
            StatementLine(
                line_date=dt.date(year, month, 10),
                reference=f'{name} READY MERCHANT',
                amount_cents=-12345,
                category='spend',
                reconciled=False,
                fingerprint=f'{name}-{year:04d}-{month:02d}-ready',
                occurrence=0,
            ),
            StatementLine(
                line_date=dt.date(year, month, 11),
                reference=f'{name} INCOMPLETE MERCHANT',
                amount_cents=-6789,
                category='spend',
                reconciled=False,
                fingerprint=f'{name}-{year:04d}-{month:02d}-incomplete',
                occurrence=0,
            ),
        ],
        duplicates_removed_by_xero=0,
        source_filename='review-test.xlsx',
    )
    card_id = db.import_card_snapshot(snapshot)['card_id']
    conn = db.get_db()
    try:
        statement_id = conn.execute(
            "SELECT id FROM cc_statements "
            "WHERE card_id=? AND year=? AND month=?",
            (card_id, year, month),
        ).fetchone()['id']
    finally:
        conn.close()
    lines = db.get_cc_statement_lines(statement_id)
    return card_id, statement_id, [line['id'] for line in lines]


def _make_ready(card_id, statement_id, line_id, suffix):
    receipt_id = db.add_cc_receipt(
        card_id,
        statement_id,
        f'{card_id}/review-{suffix}.pdf',
        f'review-{suffix}.pdf',
        'application/pdf',
        'pytest',
        content_hash=f'review-{suffix}-{card_id}',
    )
    db.link_cc_receipt(receipt_id, line_id)
    db.set_cc_line_reason(line_id, 'Reviewed business expense')
    db.set_cc_line_location(line_id, 'HQ')
    db.set_cc_lines_submitted([line_id], 'cardholder@test.co')
    return receipt_id


def test_ready_for_xero_rule_and_account_is_only_a_suggestion(db_copy):
    card_id, statement_id, (ready_id, pending_id) = _make_card(
        'ZZZ Review Readiness Credit Card')
    _make_ready(card_id, statement_id, ready_id, 'ready')
    db.set_cc_line_ai_coding(
        ready_id, '6230', 'Motor Vehicle Expenses', 'high', False,
        'Likely fuel purchase', 'ai')

    pending_receipt = _make_ready(card_id, statement_id, pending_id, 'pending-linked')
    suggested_receipt = db.add_cc_receipt(
        card_id,
        statement_id,
        f'{card_id}/review-suggested.pdf',
        'review-suggested.pdf',
        'application/pdf',
        'pytest',
        content_hash=f'review-suggested-{card_id}',
    )
    db.add_cc_suggestion(pending_id, suggested_receipt, .82)

    rows = {
        row['id']: row
        for row in db.list_cc_review_lines([card_id], status='unreconciled')
    }
    assert rows[ready_id]['ready_for_xero'] == 1
    assert rows[ready_id]['xero_account_code'] is None
    assert rows[ready_id]['ai_account_code'] == '6230'
    assert rows[pending_id]['ready_for_xero'] == 0
    assert rows[pending_id]['has_pending_suggestion'] == 1

    suggestion = db.list_cc_review_suggestions([pending_id])[0]
    db.reject_cc_suggestion(suggestion['id'])
    now_ready = db.list_cc_review_lines([card_id], status='ready')
    assert {row['id'] for row in now_ready} == {ready_id, pending_id}
    assert pending_receipt


def test_review_vat_invoice_request_preserves_scope_and_blocks_ready(client):
    card_id, statement_id, (line_id, _) = _make_card(
        'ZZZ Review VAT Invoice Credit Card')
    _make_ready(card_id, statement_id, line_id, 'vat-request')

    response = client.post(
        f'/cards/review/lines/{line_id}/vat-invoice',
        data={
            'cards_present': '1',
            'card_id': str(card_id),
            'period': '2026-07',
            'status': 'ready',
            'q': 'VAT merchant',
        },
    )

    assert response.status_code == 302
    assert '/cards/review?' in response.headers['Location']
    assert f'card_id={card_id}' in response.headers['Location']
    assert 'period=2026-07' in response.headers['Location']
    assert 'status=ready' in response.headers['Location']
    assert db.get_cc_line(line_id)['vat_invoice_required'] == 1
    assert line_id not in db.get_cc_xero_ready_line_ids([line_id])
    needs_cardholder = db.list_cc_review_lines(
        [card_id], year=2026, month=7, status='needs_cardholder')
    assert line_id in {row['id'] for row in needs_cardholder}

    scope = [
        ('cards_present', '1'),
        ('card_id', str(card_id)),
        ('period', '2026-07'),
        ('status', 'needs_cardholder'),
    ]
    page = client.get('/cards/review', query_string=scope).get_data(as_text=True)
    assert 'VAT tax invoice requested' in page

    # Asserted against the PANEL, not the page. The drawer body is fetched from
    # this endpoint now; when it was inlined, checking the page proved the drawer
    # showed the VAT state. It no longer does — the page would satisfy that string
    # even if the drawer were completely broken, because a summary of it also
    # renders on the row. Checking the panel keeps this assertion meaning what its
    # name says, and is the only coverage the drawer's contents have.
    panel = client.get(f'/cards/review/lines/{line_id}/panel', query_string=scope)
    assert panel.status_code == 200
    panel_body = panel.get_data(as_text=True)
    assert 'VAT tax invoice requested' in panel_body
    assert 'Clear VAT invoice request' in panel_body


def test_selected_reconcile_is_atomic_scoped_and_readiness_guarded(db_copy):
    mine, mine_statement, (ready_id, incomplete_id) = _make_card(
        'ZZZ Review Mine Credit Card')
    other, other_statement, (foreign_id, _) = _make_card(
        'ZZZ Review Foreign Credit Card')
    _make_ready(mine, mine_statement, ready_id, 'mine')
    _make_ready(other, other_statement, foreign_id, 'foreign')

    done, skipped = db.reconcile_cc_review_lines(
        [mine], [ready_id, incomplete_id, foreign_id, 999999999])

    assert done == 1
    assert skipped == 3
    assert db.get_cc_line(ready_id)['xero_reconciled'] == 1
    assert db.get_cc_line(incomplete_id)['xero_reconciled'] == 0
    assert db.get_cc_line(foreign_id)['xero_reconciled'] == 0


def test_selected_reconcile_rechecks_the_filtered_period(db_copy):
    name = 'ZZZ Review Period Scope Credit Card'
    card_id, july_statement, (july_id, _) = _make_card(name, month=7)
    # Preserve the July line as an evidenced carry-over before importing the
    # later "as at" statement, which correctly clears unseen unevidenced lines.
    _make_ready(card_id, july_statement, july_id, 'period-july')
    same_card_id, august_statement, (august_id, _) = _make_card(name, month=8)
    assert same_card_id == card_id
    _make_ready(card_id, august_statement, august_id, 'period-august')

    done, skipped = db.reconcile_cc_review_lines(
        [card_id], [july_id, august_id], year=2026, month=7)

    assert (done, skipped) == (1, 1)
    assert db.get_cc_line(july_id)['xero_reconciled'] == 1
    assert db.get_cc_line(august_id)['xero_reconciled'] == 0


def test_blank_legacy_fields_do_not_render_as_complete(db_copy, conn):
    card_id, statement_id, (line_id, _) = _make_card(
        'ZZZ Review Blank Fields Credit Card')
    _make_ready(card_id, statement_id, line_id, 'blank-fields')
    conn.execute(
        "UPDATE cc_lines SET reason='   ', location=' HQ ' WHERE id=?",
        (line_id,),
    )
    conn.commit()

    row = next(
        row for row in db.list_cc_review_lines([card_id], status='unreconciled')
        if row['id'] == line_id
    )
    assert row['has_reason'] == 0
    assert row['has_location'] == 1
    assert row['ready_for_xero'] == 0


def test_review_route_filters_cards_and_renders_atomic_drawers(client):
    chosen, statement_id, (ready_id, _) = _make_card(
        'ZZZ Review Route Chosen Credit Card')
    hidden, _, _ = _make_card('ZZZ Review Route Hidden Credit Card')
    _make_ready(chosen, statement_id, ready_id, 'route')

    response = client.get(
        '/cards/review',
        query_string=[
            ('cards_present', '1'),
            ('card_id', str(chosen)),
            ('period', '2026-07'),
            ('status', 'ready'),
            ('q', 'READY MERCHANT'),
        ],
    )
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'ZZZ Review Route Chosen' in page
    assert 'ZZZ Review Route Hidden Credit Card READY MERCHANT' not in page
    assert f'id="ccReviewLine{ready_id}"' in page
    assert 'Account coding is shown as a suggestion and does not block readiness' in page
    assert 'cc-review.css' in page and 'cc-review.js' in page
    assert 'data-cc-reconcile disabled' not in page
    assert f'href="/cards/{chosen}?statement_id={statement_id}"' in page
    assert (
        f'data-bs-target="#ccReviewLine{ready_id}"' in page
        and 'data-bs-toggle="offcanvas"' in page
    )
    assert all(value == 0 for value in scan_file(
        Path(__file__).parents[1] / 'templates' / 'cc_review.html').values())
    assert hidden != chosen


def test_review_post_only_reconciles_ready_rows_from_selected_cards(client):
    card_id, statement_id, (ready_id, incomplete_id) = _make_card(
        'ZZZ Review Route Reconcile Credit Card')
    _make_ready(card_id, statement_id, ready_id, 'route-reconcile')

    response = client.post(
        '/cards/review/reconcile',
        data={
            'card_id': str(card_id),
            'line_id': [str(ready_id), str(incomplete_id)],
            'period': '2026-07',
            'status': 'unreconciled',
        },
    )

    assert response.status_code == 302
    assert db.get_cc_line(ready_id)['xero_reconciled'] == 1
    assert db.get_cc_line(incomplete_id)['xero_reconciled'] == 0


def test_review_suggestion_actions_refuse_archived_cards(client):
    card_id, statement_id, (line_id, _) = _make_card(
        'ZZZ Review Archived Suggestion Credit Card')
    receipt_id = db.add_cc_receipt(
        card_id,
        statement_id,
        f'{card_id}/review-archived.pdf',
        'review-archived.pdf',
        'application/pdf',
        'pytest',
        content_hash=f'review-archived-{card_id}',
    )
    db.add_cc_suggestion(line_id, receipt_id, .91)
    suggestion = db.list_cc_review_suggestions([line_id])[0]
    db.set_cc_card_active(card_id, False)

    response = client.post(
        f"/cards/review/suggestions/{suggestion['id']}/confirm")

    assert response.status_code == 404
    assert db.get_cc_suggestion(suggestion['id'])['status'] == 'suggested'


def test_legacy_card_bulk_reconcile_uses_the_canonical_ready_rule(db_copy):
    card_id, statement_id, (ready_id, incomplete_id) = _make_card(
        'ZZZ Review Legacy Bulk Credit Card')
    _make_ready(card_id, statement_id, ready_id, 'legacy-bulk')

    # A confirmed Xero account is advisory, so this ready row can reconcile
    # without one; the incomplete row must still be skipped.
    assert db.get_cc_line(ready_id)['xero_account_code'] is None
    assert db.bulk_reconcile_cc_lines(
        card_id, statement_id, [ready_id, incomplete_id]) == 1
    assert db.get_cc_line(ready_id)['xero_reconciled'] == 1
    assert db.get_cc_line(incomplete_id)['xero_reconciled'] == 0


def test_legacy_card_single_reconcile_refuses_incomplete_row(client):
    card_id, statement_id, (ready_id, incomplete_id) = _make_card(
        'ZZZ Review Legacy Single Credit Card')
    _make_ready(card_id, statement_id, ready_id, 'legacy-single')

    refused = client.post(
        f'/cards/{card_id}/lines/{incomplete_id}/reconciled',
        data={'statement_id': statement_id},
        headers={'X-Requested-With': 'fetch'})
    assert refused.status_code == 400
    assert db.get_cc_line(incomplete_id)['xero_reconciled'] == 0

    accepted = client.post(
        f'/cards/{card_id}/lines/{ready_id}/reconciled',
        data={'statement_id': statement_id},
        headers={'X-Requested-With': 'fetch'})
    assert accepted.status_code == 200
    assert accepted.get_json()['reconciled'] is True
    assert db.get_cc_line(ready_id)['xero_reconciled'] == 1


def test_open_vat_invoice_request_blocks_both_reconcile_paths(db_copy):
    """An open VAT tax-invoice request must survive the mutation, not just the
    readiness display. Both reconcile paths re-check it server-side, so a stale
    page (or a hand-made POST) cannot reconcile a row finance has queried."""
    card_id, statement_id, (review_id, bulk_id) = _make_card(
        'ZZZ Review VAT Reconcile Credit Card')
    _make_ready(card_id, statement_id, review_id, 'vat-recon-review')
    _make_ready(card_id, statement_id, bulk_id, 'vat-recon-bulk')
    assert db.get_cc_xero_ready_line_ids(
        [review_id, bulk_id]) == {review_id, bulk_id}

    db.set_cc_line_vat_invoice_required(review_id, True, 'finance')
    db.set_cc_line_vat_invoice_required(bulk_id, True, 'finance')

    assert db.reconcile_cc_review_lines([card_id], [review_id]) == (0, 1)
    assert db.get_cc_line(review_id)['xero_reconciled'] == 0
    assert db.bulk_reconcile_cc_lines(card_id, statement_id, [bulk_id]) == 0
    assert db.get_cc_line(bulk_id)['xero_reconciled'] == 0

    # Clearing the request releases both again.
    db.set_cc_line_vat_invoice_required(review_id, False, 'finance')
    db.set_cc_line_vat_invoice_required(bulk_id, False, 'finance')
    assert db.reconcile_cc_review_lines([card_id], [review_id]) == (1, 0)
    assert db.bulk_reconcile_cc_lines(card_id, statement_id, [bulk_id]) == 1


def test_review_counts_match_the_per_status_queries(db_copy):
    """The chip tallies are now classified in Python from two fetches instead of
    one query per status — they must still agree with the SQL definitions."""
    from northwind.cards.admin_review import _review_counts

    card_id, statement_id, (ready_id, incomplete_id) = _make_card(
        'ZZZ Review Counts Credit Card')
    _make_ready(card_id, statement_id, ready_id, 'counts-ready')

    # A third state: complete but with an AI suggestion still awaiting a call.
    suggested_receipt = db.add_cc_receipt(
        card_id, statement_id, f'{card_id}/counts-sugg.pdf', 'counts-sugg.pdf',
        'application/pdf', 'pytest', content_hash=f'counts-sugg-{card_id}')
    db.add_cc_suggestion(incomplete_id, suggested_receipt, .77)

    def expected():
        return {
            key: len(db.list_cc_review_lines([card_id], status=key))
            for key in ('unreconciled', 'needs_cardholder', 'needs_ai',
                        'ready', 'reconciled')
        }

    assert _review_counts([card_id], None, None, '') == expected()

    # And again once a row has actually been reconciled, so the split moves.
    assert db.reconcile_cc_review_lines([card_id], [ready_id]) == (1, 0)
    assert _review_counts([card_id], None, None, '') == expected()


def test_zero_card_selection_survives_the_action_redirect(client):
    """Deselecting every card must stay deselected after a POST. The redirect
    carries cards_present, else the follow-up GET re-selects every card."""
    card_id, statement_id, (ready_id, _) = _make_card(
        'ZZZ Review Empty Selection Credit Card')
    _make_ready(card_id, statement_id, ready_id, 'empty-selection')
    merchant = 'ZZZ Review Empty Selection Credit Card READY MERCHANT'

    response = client.post(
        '/cards/review/reconcile', data={'cards_present': '1', 'status': 'ready'})
    assert response.status_code == 302
    location = response.headers['Location']
    assert 'cards_present=1' in location
    assert 'card_id=' not in location

    page = client.get(location)
    assert page.status_code == 200
    assert merchant not in page.get_data(as_text=True)

    # Sanity: with the card actually selected the row is there, so the
    # assertion above is about the selection and not about an empty fixture.
    selected = client.get(f'/cards/review?cards_present=1&card_id={card_id}')
    assert merchant in selected.get_data(as_text=True)
