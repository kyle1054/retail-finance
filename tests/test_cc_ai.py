"""Tests for cc_ai — the pure matcher, merchant similarity, and self-labelling
download-name logic, plus the local extraction and coding this build uses in
place of a hosted model. Nothing here touches a network, and the weight is on
the logic that decides auto-links and renames, which is where the money/UX
risk lives.
"""
import datetime as dt

from northwind.cards import ai as cc_ai
from northwind.cards.ai import ReceiptExtract


def _extract(vendor, date, total_cents):
    return ReceiptExtract(vendor=vendor, date=date, total_cents=total_cents,
                          currency='ZAR', confidence=0.9, raw_json='{}')


def _line(id, amount_cents, line_date, reference):
    return {'id': id, 'amount_cents': amount_cents,
            'line_date': line_date, 'reference': reference}


def test_exact_match_flags_exact():
    ex = _extract('Greenfields', dt.date(2026, 6, 14), 34210)
    lines = [_line(1, -34210, '2026-06-14', 'GREENFIELDS WESTPORT')]
    res = cc_ai.match_receipt(ex, lines)
    assert res and res[0].line_id == 1
    assert res[0].amount_ok and res[0].date_ok
    assert res[0].merchant_ratio == 1.0  # vendor is a substring of the reference
    assert res[0].exact is True


def test_amount_only_is_candidate_not_exact():
    # Amount matches but merchant and date do not → surfaced, but not auto-linkable.
    ex = _extract('Some Cafe', dt.date(2026, 1, 1), 34210)
    lines = [_line(1, -34210, '2026-06-14', 'GREENFIELDS WESTPORT')]
    res = cc_ai.match_receipt(ex, lines)
    assert res and res[0].amount_ok is True
    assert res[0].exact is False


def test_wrong_amount_dropped_even_if_merchant_and_date_match():
    # Real case: a R90 parking slip vs a R125 line that merely shares the words
    # "Summit City". Merchant (substring) and date match, but amount does not —
    # must NOT be surfaced (amount is the anchor).
    ex = _extract('Summit City', dt.date(2026, 6, 24), 9000)
    lines = [_line(3, -12500, '2026-06-24', 'Photo Point Summit City')]
    assert cc_ai.match_receipt(ex, lines) == []


def test_no_signal_is_dropped():
    ex = _extract('Some Cafe', dt.date(2026, 1, 1), 9999)
    lines = [_line(1, -34210, '2026-06-14', 'GREENFIELDS WESTPORT')]
    assert cc_ai.match_receipt(ex, lines) == []


def test_amount_tolerance_and_date_window():
    # 50c under, 3 days off — still within tolerance/window.
    ex = _extract('Greenfields', dt.date(2026, 6, 11), 34160)
    lines = [_line(1, -34210, '2026-06-14', 'GREENFIELDS WESTPORT')]
    res = cc_ai.match_receipt(ex, lines)
    assert res[0].amount_ok and res[0].date_ok and res[0].exact


def test_best_match_sorts_first():
    ex = _extract('Greenfields', dt.date(2026, 6, 14), 34210)
    lines = [
        _line(1, -34210, '2026-02-01', 'MARKET CO'),   # amount only
        _line(2, -34210, '2026-06-14', 'GREENFIELDS CT'),  # exact
    ]
    res = cc_ai.match_receipt(ex, lines)
    assert res[0].line_id == 2 and res[0].exact
    assert res[0].score >= res[-1].score


def test_download_name_readable():
    name = cc_ai.download_name_for('GREENFIELDS WESTPORT', '2026-06-14', -34210, '.pdf')
    assert name == '2026-06-14_GreenfieldsWestport_R342.10.pdf'


def test_download_name_multi_link_suffix():
    name = cc_ai.download_name_for('Greenfields', '2026-06-14', -34210, 'pdf', extra_count=2)
    assert name.endswith('_+2more.pdf')


def test_download_name_handles_missing_date():
    name = cc_ai.download_name_for('Greenfields', None, -34210, '.jpg')
    assert name.startswith('nodate_Greenfields_R342.10')


def test_merchant_ratio_substring_is_one():
    assert cc_ai.merchant_ratio('Greenfields', 'GREENFIELDS WESTPORT') == 1.0
    assert cc_ai.merchant_ratio('Greenfields', 'MARKET CO') < cc_ai.MERCHANT_MATCH_MIN


def test_auto_link_on_amount_and_date_even_if_merchant_differs():
    # The AIRPORT CO case: receipt reads 'AIRPORT SERVICES', bank line 'AIRPORT CO CIA Cape
    # Town' — same R710 + date. Amount+date uniquely identify it, so auto-link.
    ex = _extract('AIRPORT SERVICES', dt.date(2026, 6, 21), 71000)
    lines = [_line(1, -71000, '2026-06-21', 'AIRPORT CO CIA Westport')]
    pick = cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines))
    assert pick is not None and pick.line_id == 1


def test_no_auto_link_when_two_share_amount_and_date():
    # Two transactions, same amount+date, neither merchant matches → don't guess.
    ex = _extract('Mystery Merchant', dt.date(2026, 6, 21), 71000)
    lines = [_line(1, -71000, '2026-06-21', 'AIRPORT CO CIA Westport'),
             _line(2, -71000, '2026-06-21', 'Some Other Shop')]
    assert cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines)) is None


def test_tie_broken_by_merchant():
    # Same amount+date on two lines, but the merchant name disambiguates.
    ex = _extract('Rideco', dt.date(2026, 6, 21), 71000)
    lines = [_line(1, -71000, '2026-06-21', 'AIRPORT CO CIA Westport'),
             _line(2, -71000, '2026-06-21', 'RIDECO TRIP NTH')]
    pick = cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines))
    assert pick is not None and pick.line_id == 2


def test_no_auto_link_when_date_far_off():
    # Amount matches but date is well outside the window → suggestion, not auto-link.
    ex = _extract('Airport Services', dt.date(2026, 1, 1), 71000)
    lines = [_line(1, -71000, '2026-06-21', 'AIRPORT CO CIA Westport')]
    assert cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines)) is None


def test_tip_charge_higher_than_bill_auto_links():
    # Restaurant: slip shows R850, cardholder tipped ~14% so the card was charged
    # R970 (same day). A tip only pushes the charge UP. Because the amount no
    # longer matches exactly, the merchant name must also agree ('Slate' is a
    # substring of the bank line) → auto-links.
    ex = _extract('Slate', dt.date(2026, 6, 20), 85000)
    lines = [_line(1, -97000, '2026-06-20', 'SLATE NTH')]
    res = cc_ai.match_receipt(ex, lines)
    assert res and res[0].amount_tier == 'tip' and res[0].amount_ok is False
    pick = cc_ai.choose_auto_match(res)
    assert pick is not None and pick.line_id == 1


def test_fuzzy_match_requires_merchant_name():
    # H1 guard: a tip-band amount on the same date but a merchant that does NOT
    # match must NOT auto-link (the real line may simply not be uploaded yet).
    ex = _extract('Slate', dt.date(2026, 6, 20), 85000)
    lines = [_line(1, -97000, '2026-06-20', 'SOME UNRELATED SHOP')]
    res = cc_ai.match_receipt(ex, lines)
    assert res and res[0].amount_tier == 'tip'   # still surfaced as a suggestion
    assert cc_ai.choose_auto_match(res) is None   # but not auto-linked


def test_tip_too_large_is_dropped():
    # A charge 2x the slip is not a plausible tip → no candidate.
    ex = _extract('Some Cafe', dt.date(2026, 6, 20), 10000)
    lines = [_line(1, -20000, '2026-06-20', 'SOME CAFE')]
    assert cc_ai.match_receipt(ex, lines) == []


def test_rideco_dynamic_pricing_small_drift_auto_links():
    # Rideco invoice R150.00 but charged R153.00 (dynamic pricing), same date.
    ex = _extract('Rideco', dt.date(2026, 6, 20), 15000)
    lines = [_line(1, -15300, '2026-06-20', 'RIDECO TRIP')]
    res = cc_ai.match_receipt(ex, lines)
    assert res and res[0].amount_tier == 'drift'
    pick = cc_ai.choose_auto_match(res)
    assert pick is not None and pick.line_id == 1


def test_drift_works_both_directions():
    # Charged slightly LESS than the invoice is still a drift match.
    ex = _extract('Rideco', dt.date(2026, 6, 20), 15300)
    lines = [_line(1, -15000, '2026-06-20', 'RIDECO TRIP')]
    assert cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines)) is not None


def test_exact_amount_wins_over_tip_candidate():
    # Two same-date lines, both merchant-matching: one exact, one within the tip
    # band. Exact wins, and the fuzzy alternative must NOT make it ambiguous.
    ex = _extract('Grillhouse', dt.date(2026, 6, 20), 100000)
    lines = [_line(1, -100000, '2026-06-20', 'GRILLHOUSE SUMMIT'),   # exact
             _line(2, -110000, '2026-06-20', 'GRILLHOUSE ROSEWOOD')]  # +10% = tip band
    pick = cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines))
    assert pick is not None and pick.line_id == 1


def test_amount_relation_tiers():
    assert cc_ai.amount_relation(10000, 10050) == 'exact'   # 50c
    assert cc_ai.amount_relation(15000, 15300) == 'drift'   # R3 on R150
    assert cc_ai.amount_relation(85000, 97000) == 'tip'     # +14% gratuity
    assert cc_ai.amount_relation(85000, 100000) is None     # +18% — beyond tip ceiling
    assert cc_ai.amount_relation(10000, 20000) is None      # 2x — not plausible


def test_extract_receipt_for_an_unrecorded_slip_returns_none():
    # Nothing recorded for these bytes: a safe no-op, never a raise.
    assert cc_ai.extract_receipt(b'\x00', 'image/png') is None


def test_a_recorded_slip_is_found_by_its_own_bytes():
    """The library is keyed by content, so the same photo re-uploaded under any
    name is the same slip — and a different file is never mistaken for it."""
    data = b'a till slip somebody transcribed once'
    cc_ai.register_receipt(data, {'vendor': 'Shopfront',
                                  'date': '2026-06-14',
                                  'total_cents': 34210,
                                  'confidence': 0.9})
    ex, error = cc_ai.extract_receipt_with_error(data, 'image/png')
    assert error is None
    assert (ex.vendor, ex.total_cents) == ('Shopfront', 34210)
    assert ex.date == dt.date(2026, 6, 14)
    assert cc_ai.extract_receipt(data + b'!', 'image/png') is None


def test_coding_capitalises_the_same_purchase_above_the_asset_threshold():
    """A R900 desk is an expense and a R9,000 one is an asset. The rule that
    reads the words has to read the amount too, or half the suggestions are
    wrong in a way a reviewer has to catch every time."""
    small, _ = cc_ai.suggest_account_with_error(
        'ZZZ FURNITURE', -90000, 'desk for the back office')
    large, _ = cc_ai.suggest_account_with_error(
        'ZZZ FURNITURE', -900000, 'desk for the back office')
    assert small.account_code == '6270'      # Minor Equipment
    assert large.account_code == '6470'      # Fixtures & Fittings


def test_an_uncodeable_charge_is_flagged_rather_than_requeued():
    """A charge nobody can categorise is still one finance has to see. Leaving
    it to come back round the queue forever makes it invisible."""
    from northwind.cards import accounts as cc_accounts
    sug, error = cc_ai.suggest_account_with_error(
        'ZZZ 4419 8871 PMT', -12345, None)
    assert error is None
    assert sug.account_code == cc_accounts.FALLBACK_CODE
    assert sug.needs_review is True and sug.confidence == 'low'


# ── Enriched extraction: the total cross-check ────────────────────────────────

def test_total_reconciles_against_subtotal_plus_vat():
    assert cc_ai._reconcile_total(11500, 10000, 1500) is True
    assert cc_ai._reconcile_total(11500, 10000, 1450) is True    # R0.50 rounding
    assert cc_ai._reconcile_total(11500, 10000, 500) is False    # VAT misread
    # Unverifiable is not the same as wrong: a slip with no VAT breakdown passes.
    assert cc_ai._reconcile_total(11500, None, None) is True
    assert cc_ai._reconcile_total(None, 10000, 1500) is True


def test_coding_context_describes_the_purchase():
    ex = ReceiptExtract(
        vendor='Shopfront', date=dt.date(2026, 6, 14), total_cents=95700,
        currency='ZAR', confidence=0.9, raw_json='{}',
        summary='office furniture', line_items=['Office chair', 'Desk mat'])
    ctx = ex.coding_context()
    assert 'office furniture' in ctx and 'Office chair' in ctx and 'Desk mat' in ctx


def test_coding_context_is_empty_when_nothing_was_read():
    ex = ReceiptExtract(vendor='X', date=None, total_cents=100, currency='ZAR',
                        confidence=0.5, raw_json='{}')
    assert ex.coding_context() == ''


def test_coding_context_flags_a_non_tax_invoice():
    ex = ReceiptExtract(vendor='X', date=None, total_cents=100, currency='ZAR',
                        confidence=0.5, raw_json='{}', summary='parking',
                        is_tax_invoice=False)
    assert 'not a valid tax invoice' in ex.coding_context()


# ── Batched coding: prompt shape and index alignment ─────────────────────────

class _FakeResult:
    def __init__(self, index, account_code, confidence='high', needs_review=False):
        self.index = index
        self.account_code = account_code
        self.account_name = 'whatever the proposer called it'
        self.confidence = confidence
        self.needs_review = needs_review
        self.rationale = 'because'


def test_suggestion_from_replaces_the_proposed_name_with_our_canonical_one():
    from northwind.cards import accounts as cc_accounts
    sug = cc_ai._suggestion_from(_FakeResult(0, '6400'), 'DL RIDECO WST', cc_accounts)
    assert sug.account_code == '6400'
    assert sug.account_name == cc_accounts.name_for('6400')   # not the proposed string


def test_suggestion_from_falls_back_when_an_unknown_code_is_proposed():
    from northwind.cards import accounts as cc_accounts
    # 493 is retired and is not in the chart. It must not reach the DB.
    sug = cc_ai._suggestion_from(_FakeResult(0, '6320'), 'SKYHOP AIR', cc_accounts)
    assert sug.account_code == cc_accounts.FALLBACK_CODE
    assert sug.needs_review is True
    assert 'not recognised' in (sug.rationale or '')


def test_suggestion_from_passes_through_a_personal_charge():
    from northwind.cards import accounts as cc_accounts
    sug = cc_ai._suggestion_from(_FakeResult(0, None), 'SOMETHING', cc_accounts)
    assert sug.account_code is None and sug.needs_review is True


def test_batch_with_no_items_makes_no_call():
    assert cc_ai.suggest_accounts_batch([]) == ([], None)


def test_a_failed_batch_reports_why_for_every_item(monkeypatch):
    """A half-applied batch would leave the caller unable to tell a line that
    was deliberately left alone from one that was lost."""
    def _boom(item, cc_accounts):
        raise TimeoutError('coding took too long')

    monkeypatch.setattr(cc_ai, '_code_transaction', _boom)
    results, error = cc_ai.suggest_accounts_batch(
        [{'reference': 'A', 'amount_cents': -100, 'reason': None},
         {'reference': 'B', 'amount_cents': -200, 'reason': None}])
    assert error == 'timeout'
    assert results == [None, None]      # aligned, so both lines are requeued


def test_single_suggest_account_still_works_through_the_batch_path():
    sug, error = cc_ai.suggest_account_with_error('DL RIDECO WST', -5000,
                                                  'taxi to the airport')
    assert error is None and sug.account_code == '6410'


# ── Merchant memory: keys that actually get reused ───────────────────────────

def test_merchant_key_is_short_enough_for_a_different_branch_to_share():
    # The old 3-token key stored 'FUELSTOP MAIN ROAD', which no other Fuelstop matched.
    assert cc_ai.normalize_merchant('FUELSTOP MAIN ROAD') == 'FUELSTOP MAIN'
    assert cc_ai.merchant_brand('FUELSTOP MAIN ROAD') == 'FUELSTOP'
    assert cc_ai.merchant_brand('FUELSTOP CONVENIENCE 4021') == 'FUELSTOP'


def test_merchant_key_still_separates_genuinely_different_spend():
    # Two tokens keeps these apart — they code to different accounts (400 vs 487).
    assert cc_ai.normalize_merchant('PIXELWORKS ADS') != cc_ai.normalize_merchant('PIXELWORKS CLOUD')


def test_merchant_brand_is_empty_for_an_unusable_reference():
    assert cc_ai.merchant_brand('') == ''
    assert cc_ai.merchant_brand('12345 ***') == ''


def test_remembered_account_prefers_the_exact_key():
    exact = {'account_code': '6230', 'account_name': 'Motor Vehicle Expenses'}
    family = [{'account_code': '6170', 'account_name': 'General', 'hits': 9}]
    code, name, how = cc_ai.resolve_remembered_account(exact, family)
    assert (code, how) == ('6230', 'exact')


def test_remembered_account_inherits_from_another_branch_of_the_same_brand():
    family = [{'account_code': '6230', 'account_name': 'Motor Vehicle Expenses', 'hits': 3},
              {'account_code': '6230', 'account_name': 'Motor Vehicle Expenses', 'hits': 1}]
    code, name, how = cc_ai.resolve_remembered_account(None, family)
    assert (code, how) == ('6230', 'brand')
    assert name == 'Motor Vehicle Expenses'


def test_remembered_account_abstains_when_a_brand_was_coded_two_ways():
    """PIXELWORKS ADS -> 6000 and PIXELWORKS CLOUD -> 6120 is real ambiguity. Memory writes
    'high' confidence with no review flag, so guessing here would be worse than
    spending one AI call."""
    family = [{'account_code': '6000', 'account_name': 'Marketing', 'hits': 5},
              {'account_code': '6290', 'account_name': 'Subscriptions', 'hits': 2}]
    assert cc_ai.resolve_remembered_account(None, family) == (None, None, None)


def test_remembered_account_abstains_with_nothing_remembered():
    assert cc_ai.resolve_remembered_account(None, []) == (None, None, None)


# ── Extraction end to end, with the recorded fields stubbed ──────────────────

def _install_fake_extract(monkeypatch, parsed, text='{}'):
    """Stub the extraction seam, and nothing above it.

    Deliberately narrow: only `_extract_fields` is replaced, so what the module
    DOES with a record — the subtotal+VAT cross-check, the confidence cap, the
    flags folded into the stored blob — all stays real. Captures the arguments
    so a caller can assert on what was asked for.
    """
    sent = {}

    def _fields(data, mime_type):
        sent.update(data=data, mime_type=mime_type)
        return parsed, text

    monkeypatch.setattr(cc_ai, '_extract_fields', _fields)
    return sent


class _FakeExtract:
    def __init__(self, **kw):
        self.vendor = kw.get('vendor')
        self.date = kw.get('date')
        self.total_cents = kw.get('total_cents')
        self.currency = kw.get('currency', 'ZAR')
        self.confidence = kw.get('confidence', 0.95)
        self.subtotal_cents = kw.get('subtotal_cents')
        self.vat_cents = kw.get('vat_cents')
        self.vat_number = kw.get('vat_number')
        self.is_tax_invoice = kw.get('is_tax_invoice')
        self.line_items = kw.get('line_items', [])
        self.summary = kw.get('summary')
        self.vendor_aliases = kw.get('vendor_aliases', [])

    def as_dict(self):
        return dict(vars(self))


def test_extraction_maps_every_enriched_field(monkeypatch):
    _install_fake_extract(monkeypatch, _FakeExtract(
        vendor='Shopfront', date='2026-06-14', total_cents=95700,
        subtotal_cents=83217, vat_cents=12483, vat_number='4123456789',
        is_tax_invoice=True, line_items=['Office chair', 'Desk mat'],
        summary='office furniture'))
    ex, error = cc_ai.extract_receipt_with_error(
        b'\x00', 'image/jpeg')
    assert error is None
    assert ex.vendor == 'Shopfront'
    assert ex.date == dt.date(2026, 6, 14)
    assert ex.total_cents == 95700
    assert ex.vat_number == '4123456789'
    assert ex.is_tax_invoice is True
    assert ex.line_items == ['Office chair', 'Desk mat']
    assert ex.summary == 'office furniture'
    assert ex.total_disputed is False
    assert ex.confidence == 0.95           # reconciles, so nothing is capped


def test_extraction_caps_confidence_when_the_total_does_not_add_up(monkeypatch):
    """The total is the anchor the whole match hangs on. A subtotal+VAT that
    disagrees with it means a number was misread, and the receipt must not present
    as trustworthy."""
    _install_fake_extract(monkeypatch, _FakeExtract(
        vendor='Greenfields', date='2026-06-14', total_cents=95700,
        subtotal_cents=83217, vat_cents=500, confidence=0.99))
    ex, error = cc_ai.extract_receipt_with_error(
        b'\x00', 'image/jpeg')
    assert error is None
    assert ex.total_disputed is True
    assert ex.confidence <= 0.4
    assert ex.total_cents == 95700         # still our best guess, just not trusted


def test_extraction_without_a_vat_breakdown_is_not_penalised(monkeypatch):
    """Most till slips print no subtotal/VAT split. Unverifiable is not wrong."""
    _install_fake_extract(monkeypatch, _FakeExtract(
        vendor='Parkplus Parking', date='2026-06-14', total_cents=1000,
        confidence=0.9, summary='parking'))
    ex, _error = cc_ai.extract_receipt_with_error(
        b'\x00', 'image/jpeg')
    assert ex.total_disputed is False and ex.confidence == 0.9


def test_extraction_reports_a_slip_it_has_no_record_of(monkeypatch):
    _install_fake_extract(monkeypatch, None)
    ex, error = cc_ai.extract_receipt_with_error(
        b'\x00', 'image/jpeg')
    assert ex is None and error == 'no_recorded_extraction'


def test_a_badly_recorded_slip_is_not_the_same_as_an_unrecorded_one(monkeypatch):
    """A record with no vendor, date or total was recorded wrong rather than
    not at all, and the fix is different, so the reason has to differ too."""
    _install_fake_extract(monkeypatch, _FakeExtract())
    ex, error = cc_ai.extract_receipt_with_error(
        b'\x00', 'image/jpeg')
    assert ex is None and error == 'empty_extraction'


def test_extraction_with_no_items_leaves_line_items_unset(monkeypatch):
    _install_fake_extract(monkeypatch, _FakeExtract(
        vendor='X', date=None, total_cents=100, line_items=[]))
    ex, _error = cc_ai.extract_receipt_with_error(
        b'\x00', 'image/jpeg')
    assert ex.line_items is None
    assert ex.coding_context() == ''


def test_the_disputed_flag_is_persisted_into_the_stored_blob(monkeypatch):
    """total_disputed is computed by US, not returned by the model, so it has to be
    written into the stored JSON or nobody can later answer "why wasn't this
    auto-linked?"."""
    _install_fake_extract(monkeypatch, _FakeExtract(
        vendor='Greenfields', date='2026-06-14', total_cents=95700,
        subtotal_cents=83217, vat_cents=500), text='{"vendor": "Greenfields"}')
    ex, _error = cc_ai.extract_receipt_with_error(
        b'\x00', 'image/jpeg')
    import json as _json
    assert _json.loads(ex.raw_json)['total_disputed'] is True


def test_a_reconciling_receipt_is_marked_undisputed_in_the_blob(monkeypatch):
    _install_fake_extract(monkeypatch, _FakeExtract(
        vendor='Greenfields', date='2026-06-14', total_cents=11500,
        subtotal_cents=10000, vat_cents=1500), text='{"vendor": "Greenfields"}')
    ex, _error = cc_ai.extract_receipt_with_error(
        b'\x00', 'image/jpeg')
    import json as _json
    assert _json.loads(ex.raw_json)['total_disputed'] is False


def test_a_non_json_raw_record_still_yields_a_usable_blob(monkeypatch):
    _install_fake_extract(monkeypatch, _FakeExtract(
        vendor='X', date=None, total_cents=100), text='not json at all')
    ex, _error = cc_ai.extract_receipt_with_error(
        b'\x00', 'image/jpeg')
    import json as _json
    assert _json.loads(ex.raw_json)['vendor'] == 'X'


# ── Auto-link tiebreaks: the 36%-of-receipts-to-manual-review problem ─────────
#
# Measured against a body of human-confirmed links, the old rules sent about a
# third of receipts to manual confirmation. Most of those declines were "several
# transactions share this exact amount and date" — overwhelmingly repeat parking and
# fuel charges, where the information to decide was right there.

def test_two_identical_charges_from_one_merchant_are_interchangeable():
    """Two R10 Summit City parking charges on one day, two matching slips. Every
    assignment is equally correct for the money, the month and the account, so
    refusing to choose was pure friction."""
    ex = _extract('Summit City', dt.date(2026, 7, 9), 1000)
    lines = [_line(1, -1000, '2026-07-09', 'SUMMIT CITY PARKING SUMMIT'),
             _line(2, -1000, '2026-07-09', 'SUMMIT CITY PARKING SUMMIT')]
    pick = cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines))
    assert pick is not None and pick.line_id in (1, 2)


def test_the_same_merchant_on_different_days_is_not_interchangeable():
    """Two R50 charges from one parking operator a day apart are separate trips with
    their own slips. Picking the wrong one files the receipt against the wrong day."""
    ex = _extract('Summit City', dt.date(2026, 7, 10), 5000)
    lines = [_line(1, -5000, '2026-07-09', 'SUMMIT CITY PARKING SUMMIT'),
             _line(2, -5000, '2026-07-10', 'SUMMIT CITY PARKING SUMMIT')]
    pick = cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines))
    assert pick is not None and pick.line_id == 2      # the same-date charge


def test_interchangeable_charges_still_need_the_merchant_to_agree():
    """With an unreadable vendor there is no evidence this merchant is the right one
    at all, so identical amounts must not be assigned arbitrarily."""
    ex = _extract(None, dt.date(2026, 7, 1), 25000)
    lines = [_line(1, -25000, '2026-07-01', 'CASH ADV 00000000 XXXXXXXXXXXX'),
             _line(2, -25000, '2026-07-01', 'CASH ADV 00000000 XXXXXXXXXXXX')]
    assert cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines)) is None


def test_a_clear_name_winner_just_under_the_threshold_still_links():
    """Real case: seven R10 parking charges on one day from different operators. The
    slip reads 'Parkview Park Shopping C', which scores ~0.55 against 'Advance Parkview
    Park' — clearly the right one, yet the old absolute threshold declined it."""
    ex = _extract('Parkview Park Shopping C', dt.date(2026, 6, 29), 1000)
    lines = [_line(1, -1000, '2026-06-29', 'Parkplus Parkview Park CENTRAL'),
             _line(2, -1000, '2026-06-29', 'PARKPOINT ROSEWOOD Northport'),
             _line(3, -1000, '2026-06-29', 'FACILITEQ MARKET SQUARE WESTPORT')]
    pick = cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines))
    assert pick is not None and pick.line_id == 1


def test_no_link_when_every_candidate_name_is_equally_weak():
    """The margin rule must not fire on difflib noise — two unrelated short names can
    still score ~0.39 against each other."""
    ex = _extract('Mystery Merchant', dt.date(2026, 6, 21), 71000)
    lines = [_line(1, -71000, '2026-06-21', 'AIRPORT CO CIA Westport'),
             _line(2, -71000, '2026-06-21', 'Some Other Shop')]
    assert cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines)) is None


def test_a_receipt_with_no_readable_date_can_still_auto_link():
    """A faded thermal slip whose date the model can't read used to be undecidable:
    both gates required date_ok, so a perfect amount + name match went to review."""
    ex = _extract('Fuelstop Westbrook', None, 143140)
    lines = [_line(1, -143140, '2026-06-29', 'FUELSTOP WESTBROOK Ashton'),
             _line(2, -50000, '2026-06-29', 'GREENFIELDS BELLROSE')]
    pick = cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines))
    assert pick is not None and pick.line_id == 1


def test_a_dateless_receipt_does_not_link_on_amount_alone():
    """No date AND no name agreement is not enough — that is a guess."""
    ex = _extract('Something Else Entirely', None, 143140)
    lines = [_line(1, -143140, '2026-06-29', 'FUELSTOP WESTBROOK Ashton')]
    assert cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines)) is None


def test_a_dateless_receipt_declines_when_two_merchants_both_match():
    ex = _extract('Fuelstop', None, 143140)
    lines = [_line(1, -143140, '2026-06-29', 'FUELSTOP WESTBROOK Ashton'),
             _line(2, -143140, '2026-07-02', 'FUELSTOP MAIN ROAD Fairlands')]
    # Same merchant, no date to separate them -> genuinely undecidable.
    assert cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines)) is None


def test_a_usable_date_is_never_overridden_by_the_dateless_fallback():
    """The fallback is only for receipts with NO usable date anywhere. A receipt
    dated well outside the window must still go to review, not silently latch on."""
    ex = _extract('Fuelstop Westbrook', dt.date(2026, 1, 1), 143140)
    lines = [_line(1, -143140, '2026-06-29', 'FUELSTOP WESTBROOK Ashton')]
    assert cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines)) is None


def test_ranking_prefers_the_closer_date():
    """Ranking drove a large share of receipts to the wrong #1 suggestion because every date
    inside the 7-day window scored identically."""
    ex = _extract('Fuelstop Main Road', dt.date(2026, 7, 6), 60000)
    lines = [_line(1, -60000, '2026-07-01', 'FUELSTOP MAIN ROAD Fairlands Ext 1'),
             _line(2, -60000, '2026-07-06', 'FUELSTOP MAIN ROAD Fairlands Ext 1')]
    res = cc_ai.match_receipt(ex, lines)
    assert res[0].line_id == 2 and res[0].score > res[1].score


def test_ranking_is_stable_for_genuinely_identical_candidates():
    """Equal scores must break by line_id, or the "best" match would depend on row
    order and could differ between runs."""
    ex = _extract('Summit City', dt.date(2026, 7, 9), 1000)
    lines = [_line(7, -1000, '2026-07-09', 'SUMMIT CITY PARKING SUMMIT'),
             _line(3, -1000, '2026-07-09', 'SUMMIT CITY PARKING SUMMIT')]
    assert [m.line_id for m in cc_ai.match_receipt(ex, lines)] == [3, 7]
    assert [m.line_id for m in cc_ai.match_receipt(ex, list(reversed(lines)))] == [3, 7]


def test_match_results_carry_the_reference_and_date_delta():
    ex = _extract('Greenfields', dt.date(2026, 6, 14), 34210)
    lines = [_line(1, -34210, '2026-06-12', 'GREENFIELDS WESTPORT')]
    res = cc_ai.match_receipt(ex, lines)
    assert res[0].reference == 'GREENFIELDS WESTPORT'
    assert res[0].date_delta == 2


# ── Guardrails found by auditing real matching runs ──────────────────────

def test_a_zero_total_is_never_an_amount_match():
    """Live receipt 95 was stored as R0.00 with status 'processed'. Because the drift
    band has a R5 floor, R0.00 read as 'exact' against any charge up to R1 and 'drift'
    up to R5 — one unreadable slip could latch onto an unrelated small charge."""
    assert cc_ai.amount_relation(0, 50) is None
    assert cc_ai.amount_relation(0, 100) is None
    assert cc_ai.amount_relation(0, 500) is None


def test_a_negative_total_is_never_an_amount_match():
    assert cc_ai.amount_relation(-1000, 1000) is None
    assert cc_ai.amount_relation(1000, -1000) is None


def test_a_zero_total_receipt_produces_no_candidates_at_all():
    ex = _extract('Acme Agencies', dt.date(2026, 7, 1), 0)
    lines = [_line(1, -100, '2026-07-01', 'ACME AGENCIES'),
             _line(2, -500, '2026-07-01', 'SOMETHING SMALL')]
    assert cc_ai.match_receipt(ex, lines) == []
    assert cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines)) is None


def test_a_real_small_charge_still_matches():
    """The zero guard must not break genuine small amounts — a R10 parking meter is
    the single most common charge in this data."""
    ex = _extract('Summit City', dt.date(2026, 7, 1), 1000)
    lines = [_line(1, -1000, '2026-07-01', 'SUMMIT CITY PARKING SUMMIT')]
    pick = cc_ai.choose_auto_match(cc_ai.match_receipt(ex, lines))
    assert pick is not None and pick.line_id == 1


# ── Vendor aliases: one slip, several names the bank might have used ─────────

def _extract_aliased(vendor, aliases, date, total_cents):
    return ReceiptExtract(vendor=vendor, date=date, total_cents=total_cents,
                          currency='ZAR', confidence=0.9, raw_json='{}',
                          vendor_aliases=aliases)


def test_an_alias_rescues_a_match_the_primary_vendor_would_lose():
    """Adversarial example: the model named the mall while the statement named the parking
    operator, taking the merchant score below the matching threshold."""
    ex = _extract_aliased('Fairview Shopping Centre', ['Advance', 'Fairview Mall'],
                          dt.date(2026, 7, 10), 2500)
    assert cc_ai.merchant_ratio(ex.vendor, 'PARKPLUS FAIRVIEW MALL Northport') < 0.55
    assert cc_ai.best_merchant_ratio(ex, 'PARKPLUS FAIRVIEW MALL Northport') == 1.0
    lines = [_line(1, -2500, '2026-07-10', 'PARKPLUS FAIRVIEW MALL Northport')]
    res = cc_ai.match_receipt(ex, lines)
    assert res[0].merchant_ratio == 1.0 and res[0].exact is True


def test_aliases_do_not_invent_a_match_where_the_amount_disagrees():
    """Aliases can only confirm a candidate the amount already selected."""
    ex = _extract_aliased('Fairview Shopping Centre', ['Advance'],
                          dt.date(2026, 7, 10), 2500)
    lines = [_line(1, -9900, '2026-07-10', 'PARKPLUS FAIRVIEW MALL Northport')]
    assert cc_ai.match_receipt(ex, lines) == []


def test_best_merchant_ratio_without_aliases_matches_the_plain_ratio():
    ex = _extract('Greenfields', dt.date(2026, 6, 14), 34210)
    assert cc_ai.best_merchant_ratio(ex, 'GREENFIELDS WESTPORT') == \
        cc_ai.merchant_ratio('Greenfields', 'GREENFIELDS WESTPORT')


def test_best_merchant_ratio_ignores_empty_and_none_aliases():
    ex = _extract_aliased('Greenfields', ['', None, 'Greenfields'],
                          dt.date(2026, 6, 14), 34210)
    assert cc_ai.best_merchant_ratio(ex, 'GREENFIELDS WESTPORT') == 1.0
    assert cc_ai.best_merchant_ratio(ex, '') == 0.0
