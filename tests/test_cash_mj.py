"""Unit tests for cash_mj — the store-expenses Xero MJ builder.

The figures are invented; what they pin down is the rounding. A 15% VAT
back-out is a division, so every one of these is a case where naive
rounding and half-up rounding disagree by a cent — which is the whole
reason the journal balances or does not.
"""
import csv
import io

from northwind.cash import mj as cash_mj


def test_net_cents_standard_rate_matches_real_csv():
    # gross -> net (ex 15% VAT), half-up.
    assert cash_mj.net_cents(4500, 'standard') == 3913     # 45.00 -> 39.13
    assert cash_mj.net_cents(12000, 'standard') == 10435   # 120.00 -> 104.35
    assert cash_mj.net_cents(25500, 'standard') == 22174   # 255.00 -> 221.74
    assert cash_mj.net_cents(99900, 'standard') == 86870   # 999.00 -> 868.70
    assert cash_mj.net_cents(200000, 'standard') == 173913  # 2000.00 -> 1739.13


def test_net_cents_novat_is_unchanged():
    assert cash_mj.net_cents(5000, 'novat') == 5000       # Milk 50.00
    assert cash_mj.net_cents(45000, 'novat') == 45000     # Cleaning 450.00
    assert cash_mj.net_cents(13000, None) == 13000        # default = no VAT


def test_tax_rate_label():
    assert cash_mj.tax_rate_label('standard') == 'Standard Rate Purchases'
    assert cash_mj.tax_rate_label('novat') == 'No VAT (0%)'
    assert cash_mj.tax_rate_label(None) == 'No VAT (0%)'


def test_build_description_composes_category_date_note():
    d = cash_mj.build_description(
        'Printing & Stationery (specify in notes)', '2026-06-01', 'printing paper for store')
    assert d == 'Printing & Stationery (specify in notes) | 01/06/2026 printing paper for store'
    # No note -> just the date stamp
    assert cash_mj.build_description('Milk', '2026-06-16', '') == 'Milk | 16/06/2026'


def test_build_rows_and_csv_match_xero_columns():
    lines = [
        {'category': 'Printing & Stationery (specify in notes)', 'date': '2026-06-01',
         'note': 'printing paper for store', 'xero_code': '6240', 'vat_type': 'standard',
         'gross_cents': 8290},
        {'category': 'Cleaning Services (specify in notes)', 'date': '2026-06-01',
         'note': 'cleaning services', 'xero_code': '6180', 'vat_type': 'novat', 'gross_cents': 30000},
        {'category': 'Store Expense Other (specify)', 'date': '2026-06-16', 'note': '',
         'xero_code': '', 'vat_type': 'novat', 'gross_cents': 3990},
    ]
    rows = cash_mj.build_rows('Camden June 2026', '2026-06-30', 'Camden - STH', lines)

    r0 = rows[0]
    assert r0['*Narration'] == 'Camden June 2026'
    assert r0['*Date'] == '30/06/2026'   # DD/MM/YYYY — the finance Xero import format
    assert r0['*AccountCode'] == '6240'
    assert r0['*TaxRate'] == 'Standard Rate Purchases'
    assert r0['*Amount'] == '72.09'
    assert r0['Gross (incl VAT) check'] == '82.90'
    assert r0['TrackingName1'] == 'Store' and r0['TrackingOption1'] == 'Camden - STH'
    assert r0['TrackingName2'] == 'Department' and r0['TrackingOption2'] == 'Retail'

    # No-VAT line: amount == gross
    assert rows[1]['*Amount'] == '300.00' and rows[1]['*TaxRate'] == 'No VAT (0%)'
    # Unmapped account -> '####'
    assert rows[2]['*AccountCode'] == '####'

    # CSV round-trips with the exact header order
    out = cash_mj.to_csv(rows)
    parsed = list(csv.DictReader(io.StringIO(out)))
    assert list(parsed[0].keys()) == cash_mj.CSV_HEADER
    assert len(parsed) == 3


def test_build_rows_uses_explicit_description_override():
    lines = [{'description': 'Hand-edited narration', 'xero_code': '6240',
              'vat_type': 'standard', 'gross_cents': 8290}]
    rows = cash_mj.build_rows('N', '2026-06-30', 'T', lines)
    assert rows[0]['Description'] == 'Hand-edited narration'


# A month of store expenses (gross, vat_type). Invented, but deliberately
# mixed: the standard-rated lines make the VAT back-out land on a fraction
# of a cent, which is what forces the rounding line below.
_JOURNAL = [
    12000, 45000, 7500, 25500, 30000, 60000, 5000, 3300, 18000, 900, 40000,
    11100, 2400, 99900, 8800, 15000, 13300, 20000, 10000,
]
_JOURNAL_VAT = [
    'standard', 'novat', 'standard', 'standard', 'novat', 'novat', 'novat',
    'standard', 'standard', 'standard', 'novat', 'standard', 'standard',
    'standard', 'novat', 'standard', 'standard', 'novat', 'novat',
]


def _journal_lines():
    return [{'description': f'line{i}', 'xero_code': '6240', 'vat_type': v, 'gross_cents': g}
            for i, (g, v) in enumerate(zip(_JOURNAL, _JOURNAL_VAT))]


def test_journal_summary_reconciles():
    s = cash_mj.journal_summary(_journal_lines())
    assert s['net_total'] == 400453     # R4,004.53 subtotal
    assert s['vat_total'] == 27248      # R272.48 total VAT at 15%
    assert s['contra_cents'] == 427700  # R4,277.00 gross -> POS credit
    assert s['rounding_cents'] == 1     # R0.01 rounding plug


def test_build_balanced_rows_is_balanced_and_has_contra_and_rounding():
    lines = _journal_lines()
    rows = cash_mj.build_balanced_rows(
        'Camden June 2026', '2026-06-30', 'Camden - STH',
        'Camden', 'SC-112', lines)
    contra = rows[-2]
    rounding = rows[-1]
    assert contra['Description'] == 'POS expenses Camden'
    assert contra['*AccountCode'] == 'SC-112' and contra['*Amount'] == '-4277.00'
    assert rounding['Description'] == 'rounding'
    assert rounding['*AccountCode'] == '8600' and rounding['*Amount'] == '-0.01'

    # Xero balance: Σ(*Amount) + VAT it computes from the rates == 0.
    s = cash_mj.journal_summary(lines)
    total = sum(round(float(r['*Amount']) * 100) for r in rows) + s['vat_total']
    assert total == 0


def test_build_balanced_rows_no_rounding_line_when_exact():
    # A single no-VAT line: gross == net, no VAT, no rounding drift.
    lines = [{'description': 'x', 'xero_code': '6330', 'vat_type': 'novat', 'gross_cents': 10000}]
    rows = cash_mj.build_balanced_rows('N', '2026-06-30', 'T', 'Store', 'SC-117', lines)
    assert len(rows) == 2  # expense + contra, no rounding line
    assert rows[-1]['Description'] == 'POS expenses Store'
    assert rows[-1]['*Amount'] == '-100.00'
