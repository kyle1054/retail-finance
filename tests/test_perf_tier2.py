"""Client-weight guardrails: bounded lists and a review page without a form per row.

These pages are ``Cache-Control: no-store``, so every byte and every DOM node is
paid on every navigation, on shop-floor tablets. The assertions here are about
*shape* — they lock in that the weight cannot creep back, and, more importantly,
that making the pages lighter did not lose a row, a total, or an action.
"""
import datetime as dt
import re

import pytest

from northwind.data import database as db
from northwind.cards.parser import CardSnapshot, StatementLine
from northwind.deductions.pagination import DEFAULT_PER_PAGE, MAX_PER_PAGE


LIST_PAGES = ('/employees', '/uniforms', '/laybys', '/undercharges')


def _count(pattern, page):
    return len(re.findall(pattern, page))


def _row_links(page):
    """Every employee link in the table body, in order.

    Scoped to <tbody> so the duplicate-employee banner above the table (which is
    identical on every page) cannot mask a dropped row.
    """
    start = page.find('<tbody')
    end = page.find('</tbody>', start)
    body = page[start:end] if start != -1 else ''
    return re.findall(r'href="/employees/([A-Za-z0-9_.-]+)"', body)


# ── Pagination ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize('url', LIST_PAGES)
def test_list_pages_render_a_bounded_number_of_rows(client, url):
    """Whatever the table holds, one navigation ships at most one window."""
    page = client.get(url).get_data(as_text=True)
    assert page.count('<tr') <= DEFAULT_PER_PAGE + 40   # + header/footer/group rows


@pytest.mark.parametrize('url', LIST_PAGES)
def test_paging_never_drops_a_row(client, url):
    """Walking every page must reach exactly the rows ?per_page=all renders.

    This is the failure that would actually matter: a windowed financial list
    that silently omits an employee who still owes money.
    """
    everything = client.get(url, query_string={'per_page': 'all'})
    assert everything.status_code == 200
    expected = _row_links(everything.get_data(as_text=True))

    walked, page_number = [], 1
    while page_number <= 200:
        body = client.get(url, query_string={'page': page_number}).get_data(as_text=True)
        walked.extend(_row_links(body))
        if 'rel="next"' not in body:
            break
        page_number += 1

    assert walked == expected, url
    assert expected, 'fixture DB has no rows on ' + url


@pytest.mark.parametrize('url,filters', [
    ('/employees', {'status': 'terminated', 'q': 'a'}),
    ('/uniforms', {'status': 'all'}),
    ('/laybys', {'status': 'all'}),
    ('/undercharges', {'status': 'all', 'type': 'undercharge'}),
])
def test_pager_links_carry_the_active_filters(client, url, filters):
    """A page-2 link that forgets ?status= would quietly widen the result set."""
    page = client.get(url, query_string=dict(filters, per_page=5)).get_data(as_text=True)
    links = re.findall(r'href="([^"]*page=2[^"]*)"', page)
    if not links:                      # fewer than one window of rows for this filter
        pytest.skip('not enough rows to paginate')
    for key, value in filters.items():
        assert all(f'{key}={value}' in link for link in links), (key, links)
    assert all('per_page=5' in link for link in links)


def test_show_all_is_reachable_and_returns_the_whole_set(client):
    page = client.get('/employees').get_data(as_text=True)
    assert 'per_page=all' in page
    everything = client.get('/employees', query_string={'per_page': 'all'})
    assert everything.status_code == 200
    assert 'Showing all' in everything.get_data(as_text=True)


def test_totals_describe_the_filtered_set_not_the_visible_page(client):
    """Money and counts on these pages must not shrink as you page through.

    They are payroll figures; a per-page 'Total Outstanding' would understate
    what staff owe, which is a worse bug than a heavy page.
    """
    for url in LIST_PAGES:
        first = client.get(url, query_string={'per_page': 5}).get_data(as_text=True)
        second = client.get(url, query_string={'per_page': 5, 'page': 2})
        if second.status_code != 200 or 'page=2' not in first:
            continue
        stats = re.findall(r'class="stat-value"[^>]*>([^<]+)<', first)
        assert stats == re.findall(
            r'class="stat-value"[^>]*>([^<]+)<', second.get_data(as_text=True)), url


def test_list_search_reaches_the_server_so_it_can_see_later_pages(client):
    """The name boxes used to filter only the rendered rows.

    With the list windowed that would report 'no results' for a plan sitting on
    page 4, so each box is now wired to its filter form and honoured server-side.
    """
    for url, form_id in (('/employees', 'empFilterForm'),
                         ('/uniforms', 'uniformFilterForm'),
                         ('/laybys', 'laybyFilterForm'),
                         ('/undercharges', 'ucFilterForm')):
        page = client.get(url).get_data(as_text=True)
        assert f'form="{form_id}"' in page, url
        assert f'id="{form_id}"' in page, url
        # And the server actually narrows on it.
        narrowed = client.get(url, query_string={'q': 'zzz-no-such-name-zzz'})
        assert narrowed.status_code == 200
        assert narrowed.get_data(as_text=True).count('<tr') < page.count('<tr'), url


def test_paginate_bounds_a_hand_typed_per_page(client):
    """?per_page= is an escape hatch, not a way to demand an unbounded DOM."""
    page = client.get('/employees', query_string={'per_page': '99999'})
    assert page.status_code == 200
    assert len(_row_links(page.get_data(as_text=True))) <= MAX_PER_PAGE


def test_hq_lists_still_render_without_a_pager(client):
    """hq_laybys/hq_employees share these templates and are not windowed yet."""
    for url in ('/hq/employees', '/hq/laybys'):
        assert client.get(url).status_code == 200


# ── /cards/review: no form per row ───────────────────────────────────────────


def _review_card(name='ZZZ Perf Tier2 Review Credit Card', year=2026, month=9,
                 lines=6):
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
                reference=f'PERF MERCHANT {index}',
                amount_cents=-1000 - index,
                category='spend',
                reconciled=False,
                fingerprint=f'{name}-{year:04d}{month:02d}-{index}',
                occurrence=0,
            )
            for index in range(lines)
        ],
        duplicates_removed_by_xero=0,
        source_filename='perf-tier2.xlsx',
    )
    card_id = db.import_card_snapshot(snapshot)['card_id']
    conn = db.get_db()
    try:
        statement_id = conn.execute(
            "SELECT id FROM cc_statements WHERE card_id=? AND year=? AND month=?",
            (card_id, year, month)).fetchone()['id']
    finally:
        conn.close()
    line_ids = [row['id'] for row in db.get_cc_statement_lines(statement_id)]
    return card_id, statement_id, line_ids


def _scope(card_id, period='2026-09'):
    return [('cards_present', '1'), ('card_id', str(card_id)),
            ('period', period), ('status', 'unreconciled')]


def _ready_review_card(name='ZZZ Perf Tier2 Ready Credit Card'):
    """A card whose first line satisfies every readiness leg and whose second
    satisfies none — the two states the panel's close action branches on."""
    card_id, statement_id, line_ids = _review_card(name, lines=2)
    ready_id, incomplete_id = line_ids[0], line_ids[1]
    receipt_id = db.add_cc_receipt(
        card_id, statement_id, f'{card_id}/perf-ready.pdf', 'perf-ready.pdf',
        'application/pdf', 'pytest', content_hash=f'perf-ready-{card_id}')
    db.link_cc_receipt(receipt_id, ready_id)
    db.set_cc_line_reason(ready_id, 'Reviewed business expense')
    db.set_cc_line_location(ready_id, 'HQ')
    db.set_cc_lines_submitted([ready_id], 'cardholder@test.co')
    return card_id, ready_id, incomplete_id


def test_review_does_not_emit_a_form_per_row(client):
    card_id, _, line_ids = _review_card(lines=6)
    page = client.get('/cards/review', query_string=_scope(card_id)).get_data(as_text=True)

    assert page.count('PERF MERCHANT') >= 6          # the rows really are there
    # Filters, two exports, the hidden filter-state form and the reconcile form.
    assert page.count('<form') <= 6
    # The filter state used to be re-stamped into every per-row form; a page with
    # N rows must not carry N copies of it.
    assert _count(rf'name="card_id" value="{card_id}"', page) <= 6


def test_review_row_actions_and_drawer_survive(client):
    card_id, statement_id, line_ids = _review_card(lines=3)
    line_id = line_ids[0]
    page = client.get('/cards/review', query_string=_scope(card_id)).get_data(as_text=True)

    # The row still opens a drawer, and still falls back to the card month.
    assert f'data-bs-target="#ccReviewLine{line_id}"' in page
    assert f'id="ccReviewLine{line_id}"' in page
    assert f'href="/cards/{card_id}?statement_id={statement_id}"' in page
    # Per-row actions post through the one shared filter-state form.
    assert 'id="ccReviewFilterState"' in page
    assert f'formaction="/cards/review/lines/{line_id}/vat-invoice"' in page


def test_review_drawer_panel_carries_every_action_and_the_filter_scope(client):
    card_id, _, line_ids = _review_card(lines=3)
    line_id = line_ids[0]
    panel = client.get(f'/cards/review/lines/{line_id}/panel',
                       query_string=_scope(card_id))
    body = panel.get_data(as_text=True)

    assert panel.status_code == 200
    assert f'action="/cards/review/lines/{line_id}/vat-invoice"' in body
    assert 'Request VAT tax invoice' in body
    assert 'Open this card month' in body
    # The scope the page was filtered by is re-posted, so an action redirects
    # back to the same working view.
    assert f'name="card_id" value="{card_id}"' in body
    assert 'name="period" value="2026-09"' in body
    assert 'name="cards_present" value="1"' in body
    assert 'name="csrf_token"' in body


def test_review_queue_row_carries_what_the_split_workspace_needs(client):
    """The queue row is the workspace's only handle on a transaction.

    static/cc-review.js finds rows by `data-cc-row`, fetches the stage's contents
    from that row's `data-cc-panel`, and matches the panel's "Reconcile and open
    next" button back to the row's line checkbox by value. If any of those come
    apart the stage silently stops filling, so they are asserted together with
    the offcanvas fallback the same row still carries at phone widths.
    """
    card_id, _, line_ids = _review_card(lines=3)
    line_id = line_ids[0]
    page = client.get('/cards/review', query_string=_scope(card_id)).get_data(as_text=True)

    assert 'data-cc-workspace' in page and 'data-cc-stage' in page
    assert f'data-cc-row data-cc-line-id="{line_id}"' in page
    assert f'data-cc-panel="/cards/review/lines/{line_id}/panel' in page
    # The close button matches `[data-cc-line][value="<id>"]`, so the row's
    # checkbox must carry both on the one element.
    assert re.search(
        rf'name="line_id"[^>]*value="{line_id}"[^>]*data-cc-line', page,
        re.S), 'the queue checkbox no longer joins line_id to data-cc-line'
    # The stage is empty markup until JavaScript fills it, so it must not be the
    # only way in: the row keeps its drawer target and its no-JS destination.
    assert f'data-bs-target="#ccReviewLine{line_id}"' in page


def test_review_panel_offers_closing_one_transaction_only_when_it_is_ready(client):
    """"Reconcile and open next" is the panel's primary action, and it must not
    appear for a line the reconcile endpoint would refuse."""
    card_id, ready_id, incomplete_id = _ready_review_card()

    ready_panel = client.get(
        f'/cards/review/lines/{ready_id}/panel',
        query_string=_scope(card_id)).get_data(as_text=True)
    incomplete_panel = client.get(
        f'/cards/review/lines/{incomplete_id}/panel',
        query_string=_scope(card_id)).get_data(as_text=True)

    assert f'data-cc-close-line="{ready_id}"' in ready_panel
    assert 'Reconcile and open next' in ready_panel
    assert 'data-cc-close-line' not in incomplete_panel


def test_review_drawer_panel_refuses_a_line_outside_the_filtered_scope(client):
    mine, _, mine_lines = _review_card('ZZZ Perf Tier2 Mine Credit Card', month=9)
    other, _, other_lines = _review_card('ZZZ Perf Tier2 Other Credit Card', month=9)

    # A line id from a card the current filter excludes is not viewable.
    assert client.get(f'/cards/review/lines/{other_lines[0]}/panel',
                      query_string=_scope(mine)).status_code == 404
    # ...and the same id inside its own scope is.
    assert client.get(f'/cards/review/lines/{other_lines[0]}/panel',
                      query_string=_scope(other)).status_code == 200
    assert mine_lines


def test_review_drawer_panel_is_super_only(db_copy):
    """It is a cc_ endpoint, so a scoped admin must not reach it by URL."""
    from northwind.core import admin_endpoint_allowed
    assert admin_endpoint_allowed('cc_review_line_panel', 'super')
    assert not admin_endpoint_allowed('cc_review_line_panel', 'retail')
    assert not admin_endpoint_allowed('cc_review_line_panel', 'hq')


def test_review_vat_toggle_round_trips_from_the_row(client):
    """What #ccReviewFilterState posts must still flip the flag and come back."""
    card_id, _, line_ids = _review_card(lines=3)
    line_id = line_ids[0]

    response = client.post(
        f'/cards/review/lines/{line_id}/vat-invoice',
        data={'cards_present': '1', 'card_id': str(card_id),
              'period': '2026-09', 'status': 'unreconciled'})

    assert response.status_code == 302
    assert '/cards/review?' in response.headers['Location']
    assert f'card_id={card_id}' in response.headers['Location']
    assert db.get_cc_line(line_id)['vat_invoice_required'] == 1

    client.post(f'/cards/review/lines/{line_id}/vat-invoice',
                data={'cards_present': '1', 'card_id': str(card_id),
                      'period': '2026-09', 'status': 'unreconciled'})
    assert db.get_cc_line(line_id)['vat_invoice_required'] == 0


# ── Scroll-time compositing / listener count ─────────────────────────────────


def test_no_backdrop_filter_on_a_sticky_or_fixed_element():
    """backdrop-filter on a sticky bar re-blurs the page behind it every frame."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / 'static'
    for sheet in root.glob('*.css'):
        source = re.sub(r'/\*.*?\*/', '', sheet.read_text(encoding='utf-8'),
                        flags=re.S)
        for declaration in re.findall(r'backdrop-filter\s*:\s*([^;}]+)', source):
            # `backdrop-filter: none` is a defensive override, not a cost.
            assert declaration.strip().startswith('none'), f'{sheet.name}: {declaration}'


def test_form_loading_feedback_is_delegated_and_still_defers_its_checks():
    """One listener on document — but the deferred check order is load-bearing.

    defaultPrevented / data-noLoading are read inside setTimeout(...,0) so a form
    another handler intercepts is left alone; reading them synchronously would
    put a permanent spinner on cancelled submits.
    """
    import pathlib
    source = (pathlib.Path(__file__).resolve().parents[1]
              / 'static' / 'app-ui.js').read_text(encoding='utf-8')
    assert "document.querySelectorAll('form')" not in source
    listener = source.index("document.addEventListener('submit'")
    timeout = source.index('window.setTimeout', listener)
    guard = source.index('event.defaultPrevented', timeout)
    no_loading = source.index("form.dataset.noLoading === 'true'", guard)
    assert listener < timeout < guard < no_loading


def test_pager_preserves_repeated_query_parameters():
    """A multi-select filter must survive paging intact.

    ``args.to_dict()`` keeps only the FIRST value of a repeated key. The four
    deduction lists have no repeated parameter today, but /cards/review carries
    one ``card_id`` per selected card — so reusing this macro there would have
    silently narrowed the selection to a single card the moment someone paged,
    with a shorter list and no explanation. Guarded here rather than discovered
    there.
    """
    from werkzeug.datastructures import MultiDict

    import app as a
    from northwind.deductions.pagination import _url

    with a.app.test_request_context('/undercharges'):
        single = _url('undercharges_list',
                      MultiDict([('store', 'Westgate'), ('status', 'all')]), page=3)
        assert 'store=Westgate' in single and 'status=all' in single
        assert 'page=3' in single

        repeated = _url('undercharges_list',
                        MultiDict([('card_id', '6'), ('card_id', '7'),
                                   ('card_id', '8')]), page=2)
        assert repeated.count('card_id=') == 3, (
            f'paging dropped repeated values: {repeated}')
        for value in ('6', '7', '8'):
            assert f'card_id={value}' in repeated
