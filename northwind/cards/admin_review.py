"""Cross-card admin review queue for Credit Card Reconciliation.

The queue mirrors the way finance selects several card accounts in Xero, while
keeping every transaction and every mutation on its own ``cc_lines`` row.
"""
import os

from flask import (
    abort, flash, redirect, render_template, request, send_file, session, url_for,
)

from northwind.cards import ai as cc_ai
from northwind.cards import export as cc_export
from northwind.core import app
from northwind.data import database as db
from northwind.deductions.pagination import paginate
from northwind.services import storage


_STATUSES = {
    'unreconciled',
    'needs_cardholder',
    'needs_ai',
    'ready',
    'personal',
    'reconciled',
}

_STATUS_LABELS = {
    'unreconciled': 'All unreconciled',
    'needs_cardholder': 'Needs cardholder',
    'needs_ai': 'Needs AI review',
    'ready': 'Ready for Xero',
    'personal': 'Personal expenses',
    'reconciled': 'Reconciled',
}


def _requested_card_ids(source, valid_ids):
    values = source.getlist('card_id')
    if not values:
        if source.get('cards_present'):
            return []
        return sorted(valid_ids)
    selected = set()
    for value in values:
        try:
            card_id = int(value)
        except (TypeError, ValueError):
            continue
        if card_id in valid_ids:
            selected.add(card_id)
    return sorted(selected)


def _period(value):
    raw = (value or '').strip()
    try:
        year_text, month_text = raw.split('-', 1)
        year, month = int(year_text), int(month_text)
    except (TypeError, ValueError):
        return None, None, ''
    if not 2000 <= year <= 2100 or not 1 <= month <= 12:
        return None, None, ''
    return year, month, f'{year:04d}-{month:02d}'


def _redirect_to_review(source):
    cards = []
    for value in source.getlist('card_id'):
        try:
            cards.append(int(value))
        except (TypeError, ValueError):
            continue
    params = {
        'card_id': cards,
        # Carry the sentinel through. An empty selection emits no card_id at
        # all, so without it _requested_card_ids would read the redirect as
        # "no card filter given" and silently re-select every active card.
        'cards_present': 1 if source.get('cards_present') else None,
        'period': (source.get('period') or '').strip() or None,
        'status': (source.get('status') or '').strip() or None,
        'q': (source.get('q') or '').strip() or None,
        'page': source.get('page') or None,
        'per_page': source.get('per_page') or None,
    }
    return redirect(url_for('cc_review', **params))


def _review_selection(source):
    cards = db.list_cc_cards(active_only=True)
    valid_ids = {row['id'] for row in cards}
    selected_ids = _requested_card_ids(source, valid_ids)
    year, month, period = _period(source.get('period'))
    status = (source.get('status') or 'unreconciled').strip().lower()
    if status not in _STATUSES:
        status = 'unreconciled'
    search = (source.get('q') or '').strip()[:100]
    return cards, selected_ids, year, month, period, status, search


def _review_rows_with_receipts(selected_ids, year, month, status, search):
    rows = [
        dict(row) for row in db.list_cc_review_lines(
            selected_ids, year=year, month=month, status=status, search=search)
    ]
    receipts = [
        dict(receipt) for receipt in db.list_cc_review_receipts(
            [row['id'] for row in rows])
    ]
    return rows, receipts


def _review_rows(selected_ids, year, month, status, search):
    """Filtered row dictionaries without eagerly loading receipt metadata."""
    return [
        dict(row) for row in db.list_cc_review_lines(
            selected_ids, year=year, month=month, status=status, search=search)
    ]


def _cardholder_complete(row):
    """Python mirror of the ``cardholder_complete`` SQL in list_cc_review_lines.

    Kept in step with that fragment: receipt-or-personal, reason, location, no
    open VAT tax-invoice request, and submitted to finance.
    """
    return bool(
        (row['personal'] or row['has_receipt'])
        and row['has_reason']
        and row['has_location']
        and not row['vat_invoice_required']
        and row['submitted_at']
    )


def _review_counts(selected_ids, year, month, search):
    """Status tallies for the filter chips.

    Two fetches classified in Python rather than one heavy query per counter:
    every status except 'reconciled' is a subset of the unreconciled set, and
    the flags needed to split it are already columns on those rows.
    """
    kwargs = {'year': year, 'month': month, 'search': search}
    unreconciled = db.list_cc_review_lines(
        selected_ids, status='unreconciled', **kwargs)
    reconciled = db.list_cc_review_lines(
        selected_ids, status='reconciled', **kwargs)
    return {
        'unreconciled': len(unreconciled),
        'needs_cardholder': sum(
            1 for row in unreconciled if not _cardholder_complete(row)),
        'needs_ai': sum(
            1 for row in unreconciled if row['has_pending_suggestion']),
        'ready': sum(1 for row in unreconciled if row['ready_for_xero']),
        'reconciled': len(reconciled),
    }


def _panel_args(selected_ids, period, status, search):
    """Query args that re-state the current review scope for the drawer fetch.

    The drawer re-resolves its line through the SAME filtered query the table
    ran, so it needs the identical scope; it also re-emits these as the hidden
    fields its action forms post back (see _cc_review_filters.html).
    """
    return {
        'cards_present': 1,
        'card_id': sorted(selected_ids),
        'period': period or None,
        'status': status,
        'q': search or None,
        'page': request.args.get('page', type=int),
        'per_page': (request.args.get('per_page') or None),
    }


def _export_scope(cards, selected_ids, period, status, search):
    selected = set(selected_ids)
    return {
        'card_names': [
            row['display_name'] or row['card_name']
            for row in cards if row['id'] in selected
        ],
        'period': period,
        'status': status,
        'status_label': _STATUS_LABELS[status],
        'search': search,
    }


@app.route('/cards/review')
def cc_review():
    (cards, selected_ids, year, month, period,
     status, search) = _review_selection(request.args)
    all_rows = _review_rows(selected_ids, year, month, status, search)
    rows, pager = paginate(
        all_rows, noun='transactions', per_page=25, endpoint='cc_review')
    line_ids = [row['id'] for row in rows]
    review_receipts = [
        dict(receipt) for receipt in db.list_cc_review_receipts(line_ids)
    ]
    receipts = {}
    for receipt in review_receipts:
        receipts.setdefault(receipt['line_id'], []).append(receipt)
    suggestions = {}
    for suggestion in db.list_cc_review_suggestions(line_ids):
        suggestions.setdefault(suggestion['line_id'], []).append(suggestion)

    panel_args = _panel_args(selected_ids, period, status, search)
    for row in rows:
        row['receipts'] = receipts.get(row['id'], [])
        row['suggestions'] = suggestions.get(row['id'], [])
        # Where the row's drawer body comes from when it is opened. The table
        # itself no longer carries that markup.
        row['panel_url'] = url_for(
            'cc_review_line_panel', line_id=row['id'], **panel_args)

    counts = _review_counts(selected_ids, year, month, search)
    periods = db.list_cc_review_periods(selected_ids)
    return render_template(
        'cc_review.html',
        cards=cards,
        selected_ids=set(selected_ids),
        rows=rows,
        periods=periods,
        selected_period=period,
        selected_status=status,
        search=search,
        counts=counts,
        pager=pager,
        selected_page=pager['page'],
        selected_per_page=(request.args.get('per_page') or ''),
    )


@app.route('/cards/review/lines/<int:line_id>/panel')
def cc_review_line_panel(line_id):
    """One review drawer's contents, fetched only when that drawer is opened.

    Rendering all of them into /cards/review cost roughly 90 DOM elements and
    three <form>s per row — the bulk of that page's 11,000 elements and 766 KB,
    re-sent on every navigation because the page is Cache-Control: no-store.

    The line is re-resolved through the SAME filtered query the table ran rather
    than fetched by id, so a line id outside the selected cards/period/status is
    a 404 instead of a peek at a card the filters excluded — the same
    re-scoping rule the reconcile and suggestion actions follow.
    """
    (cards, selected_ids, year, month, period,
     status, search) = _review_selection(request.args)
    rows, _ = _review_rows_with_receipts(
        selected_ids, year, month, status, search)
    line = next((row for row in rows if row['id'] == line_id), None)
    if line is None:
        abort(404)
    line['receipts'] = [
        dict(receipt) for receipt in db.list_cc_review_receipts([line_id])
    ]
    line['suggestions'] = [
        dict(suggestion) for suggestion in db.list_cc_review_suggestions([line_id])
    ]
    return render_template(
        'cc_review_drawer.html',
        line=line,
        selected_ids=set(selected_ids),
        selected_period=period,
        selected_status=status,
        search=search,
        selected_page=request.args.get('page', type=int),
        selected_per_page=(request.args.get('per_page') or ''),
    )


@app.route('/cards/review/export.xlsx')
def cc_review_export_xlsx():
    """Download the currently filtered transaction index as a workbook."""
    (cards, selected_ids, year, month, period,
     status, search) = _review_selection(request.args)
    rows, receipts = _review_rows_with_receipts(
        selected_ids, year, month, status, search)
    scope = _export_scope(cards, selected_ids, period, status, search)
    workbook = cc_export.build_workbook(rows, receipts, scope)
    return send_file(
        workbook,
        mimetype=cc_export.XLSX_MIME,
        as_attachment=True,
        download_name=cc_export.export_filename(scope, 'xlsx'),
    )


@app.route('/cards/review/export.zip')
def cc_review_export_zip():
    """Download the filtered transaction index with each linked receipt."""
    (cards, selected_ids, year, month, period,
     status, search) = _review_selection(request.args)
    rows, receipts = _review_rows_with_receipts(
        selected_ids, year, month, status, search)
    scope = _export_scope(cards, selected_ids, period, status, search)
    pack, included, missing = cc_export.build_finance_pack(
        rows, receipts, scope, storage.read)
    response = send_file(
        pack,
        mimetype='application/zip',
        as_attachment=True,
        download_name=cc_export.export_filename(scope, 'zip'),
    )
    response.headers['X-NORTHWIND-Receipt-Files'] = str(included)
    response.headers['X-NORTHWIND-Missing-Receipt-Files'] = str(missing)
    return response


@app.route('/cards/review/reconcile', methods=['POST'])
def cc_review_reconcile():
    cards = db.list_cc_cards(active_only=True)
    valid_ids = {row['id'] for row in cards}
    selected_ids = _requested_card_ids(request.form, valid_ids)
    line_ids = []
    for value in request.form.getlist('line_id'):
        try:
            line_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    year, month, _ = _period(request.form.get('period'))
    done, skipped = db.reconcile_cc_review_lines(
        selected_ids, line_ids, year=year, month=month)
    if done:
        noun = 'transaction' if done == 1 else 'transactions'
        flash(f'Marked {done} {noun} reconciled in Xero.', 'success')
    if skipped:
        noun = 'transaction was' if skipped == 1 else 'transactions were'
        flash(
            f'{skipped} selected {noun} skipped because the transaction was '
            'incomplete, outside the selected cards, or already reconciled.',
            'warning',
        )
    if not line_ids:
        flash('Select at least one Ready for Xero transaction.', 'warning')
    return _redirect_to_review(request.form)


@app.route('/cards/review/lines/<int:line_id>/vat-invoice', methods=['POST'])
def cc_review_toggle_vat_invoice(line_id):
    """Open/clear a VAT tax-invoice request without leaving the review scope."""
    line = db.get_cc_line(line_id)
    active_ids = {row['id'] for row in db.list_cc_cards(active_only=True)}
    if (not line or line['card_id'] not in active_ids
            or line['category'] != 'spend'
            or line['status'] != 'outstanding'):
        abort(404)
    required = not bool(line['vat_invoice_required'])
    db.set_cc_line_vat_invoice_required(
        line_id, required, session.get('admin_username') or 'admin')
    flash(
        'VAT tax invoice requested from the cardholder.'
        if required else 'VAT tax invoice request cleared.',
        'warning' if required else 'success',
    )
    return _redirect_to_review(request.form)


@app.route('/cards/review/suggestions/<int:suggestion_id>/confirm', methods=['POST'])
def cc_review_confirm_suggestion(suggestion_id):
    suggestion = db.get_cc_suggestion(suggestion_id)
    active_ids = {row['id'] for row in db.list_cc_cards(active_only=True)}
    if (not suggestion or suggestion['status'] != 'suggested'
            or suggestion['card_id'] not in active_ids):
        abort(404)
    result = db.confirm_cc_suggestion(
        suggestion_id, actor=session.get('admin_username') or 'admin')
    if not result:
        flash('That receipt suggestion is no longer available.', 'warning')
        return _redirect_to_review(request.form)
    receipt_id, line_id = result
    line = db.get_cc_line(line_id)
    receipt = db.get_cc_receipt(receipt_id)
    if line and receipt:
        ext = os.path.splitext(receipt['file_path'])[1]
        extra = max(0, db.count_cc_receipt_links(receipt_id) - 1)
        db.set_cc_receipt_download_name(
            receipt_id,
            cc_ai.download_name_for(
                line['reference'], line['line_date'], line['amount_cents'], ext, extra),
        )
    flash('Receipt matched to this transaction.', 'success')
    return _redirect_to_review(request.form)


@app.route('/cards/review/suggestions/<int:suggestion_id>/dismiss', methods=['POST'])
def cc_review_dismiss_suggestion(suggestion_id):
    suggestion = db.get_cc_suggestion(suggestion_id)
    active_ids = {row['id'] for row in db.list_cc_cards(active_only=True)}
    if (not suggestion or suggestion['status'] != 'suggested'
            or suggestion['card_id'] not in active_ids):
        abort(404)
    db.reject_cc_suggestion(suggestion_id)
    flash('Receipt suggestion dismissed.', 'success')
    return _redirect_to_review(request.form)
