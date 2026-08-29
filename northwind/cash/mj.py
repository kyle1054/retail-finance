"""Store-expenses Xero Manual-Journal builder.

Pure Python (no Flask / DB deps) so it's unit-testable against the finance
standard manual-journal CSV exports. Turns a store-month's cash-recon expense lines
into the exact Xero manual-journal import format:

    *Narration,*Date,Description,*AccountCode,*TaxRate,*Amount,
    TrackingName1,TrackingOption1,TrackingName2,TrackingOption2,Gross (incl VAT) check

Conventions (matched to a representative "Store - Month.csv" export):
  - Amounts are captured GROSS (VAT-inclusive cash out). The *Amount column is
    the NET (ex-VAT) figure — gross / 1.15 for standard-rated lines, unchanged
    for No-VAT lines. The last column repeats the gross as a check.
  - *Date is the month-end; the entry's own date rides in the Description.
  - Every line is tracked by Store=<tracking name> and Department=Retail.
  - An unmapped account code renders as "####" (admin fills it on the preview).
"""

import csv
import io

# VAT type -> Xero tax rate name. Only the two the store expenses use today.
TAX_RATES = {
    'standard': 'Standard Rate Purchases',
    'novat': 'No VAT (0%)',
}
DEFAULT_VAT = 'novat'
STANDARD_VAT_RATE = 0.15  # SA VAT

UNMAPPED_CODE = '####'
DEPARTMENT = 'Retail'
ROUNDING_CODE = '8600'  # Xero "Rounding" account for the balancing cent(s)

CSV_HEADER = [
    '*Narration', '*Date', 'Description', '*AccountCode', '*TaxRate', '*Amount',
    'TrackingName1', 'TrackingOption1', 'TrackingName2', 'TrackingOption2',
    'Gross (incl VAT) check',
]


def tax_rate_label(vat_type):
    """The Xero *TaxRate string for a vat_type ('standard'/'novat')."""
    return TAX_RATES.get(vat_type or DEFAULT_VAT, TAX_RATES[DEFAULT_VAT])


def net_cents(gross_cents, vat_type):
    """Net (ex-VAT) cents for a gross amount. Standard-rated lines divide out
    15% VAT (half-up); No-VAT lines are unchanged."""
    g = int(gross_cents)
    if vat_type == 'standard':
        return int(g / (1.0 + STANDARD_VAT_RATE) + 0.5)
    return g


def vat_cents(net, vat_type):
    """The VAT (in cents) Xero computes from a tax-exclusive line = round(net x
    15%) for standard-rated lines, 0 otherwise. Used to size the rounding plug."""
    if vat_type == 'standard':
        return int(int(net) * STANDARD_VAT_RATE + 0.5)
    return 0


def _iso_to_ddmmyyyy(iso):
    """'2026-06-01' -> '01/06/2026'; pass anything odd straight through."""
    parts = (iso or '').split('-')
    if len(parts) == 3 and all(parts):
        y, m, d = parts
        return f"{d}/{m}/{y}"
    return iso or ''


def build_description(category, entry_date_iso, note):
    """'{category} | {DD/MM/YYYY} {note}' — the note is optional."""
    stamp = _iso_to_ddmmyyyy(entry_date_iso)
    body = f"{stamp} {note}".strip() if note else stamp
    return f"{category} | {body}".rstrip()


def build_rows(narration, month_end_iso, tracking_name, lines):
    """Build ordered CSV row dicts (one per line) for the given store-month.

    `lines` is a list of dicts, each already reflecting any preview overrides:
        {category, note, date (entry ISO), xero_code, vat_type, gross_cents}
    Returns a list of dicts keyed by CSV_HEADER, with money as 2dp strings.
    """
    date_out = _iso_to_ddmmyyyy(month_end_iso)
    rows = []
    for ln in lines:
        gross = int(ln['gross_cents'])
        vt = ln.get('vat_type') or DEFAULT_VAT
        net = net_cents(gross, vt)
        # Use an explicit description if the caller supplied one (the editable
        # preview posts the final text); otherwise compose it from the parts.
        desc = ln.get('description')
        if not desc:
            desc = build_description(ln.get('category', ''), ln.get('date'), ln.get('note'))
        rows.append({
            '*Narration': narration,
            '*Date': date_out,
            'Description': desc,
            '*AccountCode': (ln.get('xero_code') or '').strip() or UNMAPPED_CODE,
            '*TaxRate': tax_rate_label(vt),
            '*Amount': f"{net / 100:.2f}",
            'TrackingName1': 'Store',
            'TrackingOption1': tracking_name or '',
            'TrackingName2': 'Department',
            'TrackingOption2': DEPARTMENT,
            'Gross (incl VAT) check': f"{gross / 100:.2f}",
        })
    return rows


def _plain_row(narration, date_out, description, code, tax_label, amount_cents,
               tracking_name=None, department=None, gross_check=None):
    """A single CSV row dict. Amount negative => credit. Tracking optional."""
    return {
        '*Narration': narration,
        '*Date': date_out,
        'Description': description,
        '*AccountCode': code,
        '*TaxRate': tax_label,
        '*Amount': f"{amount_cents / 100:.2f}",
        'TrackingName1': 'Store' if tracking_name else '',
        'TrackingOption1': tracking_name or '',
        'TrackingName2': 'Department' if department else '',
        'TrackingOption2': department or '',
        'Gross (incl VAT) check': '' if gross_check is None else f"{gross_check / 100:.2f}",
    }


def journal_summary(lines):
    """Totals (in cents) for a set of expense lines, plus the contra + rounding
    amounts needed to balance the journal in Xero.

    Xero grosses each tax-exclusive line back up by its VAT, so the journal
    balances when: Σnet + Σvat (debits) == Σgross (the POS contra credit).
    Per-line net rounding makes those differ by a cent or two — the rounding
    line absorbs it. Returns a dict of cents totals; `rounding_cents` is signed
    (debit_total − gross_total)."""
    gross_total = net_total = vat_total = 0
    for ln in lines:
        gross = int(ln['gross_cents'])
        vt = ln.get('vat_type') or DEFAULT_VAT
        n = net_cents(gross, vt)
        gross_total += gross
        net_total += n
        vat_total += vat_cents(n, vt)
    debit_total = net_total + vat_total
    return {
        'gross_total': gross_total,
        'net_total': net_total,
        'vat_total': vat_total,
        'debit_total': debit_total,
        'contra_cents': gross_total,          # credited to the POS account
        'rounding_cents': debit_total - gross_total,  # signed plug
    }


def build_balanced_rows(narration, month_end_iso, tracking_name, store_label,
                        dear_code, lines, rounding_code=ROUNDING_CODE):
    """The full, import-ready journal: the expense debit lines followed by the
    POS contra credit (= total gross) and a rounding credit/debit so the whole
    thing balances in Xero exactly as it posts."""
    rows = build_rows(narration, month_end_iso, tracking_name, lines)
    s = journal_summary(lines)
    date_out = _iso_to_ddmmyyyy(month_end_iso)
    novat = TAX_RATES['novat']

    # Contra: credit the store's Cash In/Out (POS) account for the gross spent.
    rows.append(_plain_row(
        narration, date_out, f"POS expenses {store_label}",
        (dear_code or '').strip() or UNMAPPED_CODE, novat, -s['contra_cents'],
        tracking_name=tracking_name, department=DEPARTMENT))

    # Rounding: absorb the net/VAT rounding drift so debits == credits.
    if s['rounding_cents']:
        rows.append(_plain_row(
            narration, date_out, 'rounding', rounding_code, novat,
            -s['rounding_cents']))
    return rows


def to_csv(rows):
    """Serialise build_rows() output to a Xero-import CSV string."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_HEADER)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


# ── Consolidated cash-sales manual journal ────────────────────────────────────
# the cash-split journal, not the balanced expense MJ above.
# The shape is fixed by the destination workbook's "To copy" sheet: SEVEN columns, one
# positive line per store (mapped zero-value stores included), then the control
# row. This journal carries NO tracking — the store identity is already in the
# POS account code — so TrackingName1 is present as a column but always blank.
#
# The control row DOES carry the balancing credit (−Σ stores). Be clear about
# the evidence: in the reference workbook that cell is EMPTY, and the
# cell below it (`=SUM` over the amount column) therefore reads the store total,
# not zero — it is a control total, not a proof that the row should be −Σ. The
# reason we emit −Σ anyway is simply that Xero rejects an unbalanced manual
# journal, and the requirement is that the file import. If it turns out
# finance keys the contra by hand inside Xero after importing, this is the one
# line to change — and `test_cash_sales_journal_balances_to_zero` with it.
#   Sign convention (as Xero imports): positive *Amount = debit, negative = credit.

CASH_SALES_HEADER = [
    '*Narration', '*Date', 'Description', '*AccountCode', '*TaxRate', '*Amount',
    'TrackingName1',
]
DEFAULT_CASH_SALES_CONTRA = '8990.9'   # "POS Cash Payment"


def _cash_sales_row(narration, date_out, code, amount_cents):
    return {
        '*Narration': narration,
        '*Date': date_out,
        'Description': narration,
        '*AccountCode': (code or '').strip() or UNMAPPED_CODE,
        '*TaxRate': TAX_RATES['novat'],
        '*Amount': f"{amount_cents / 100:.2f}",
        'TrackingName1': '',
    }


def build_cash_sales_rows(narration, date_out, contra_code, store_lines):
    """Full balanced cash-sales journal rows.

    `store_lines` — [{store_code, sales_cents}] (already summed per store). Each
    becomes a debit to its POS account; the control row credits `contra_code`
    with the grand total so the journal balances on import."""
    rows = []
    total = 0
    for ln in store_lines:
        cents = int(ln['sales_cents'])
        total += cents
        rows.append(_cash_sales_row(narration, date_out, ln.get('store_code'), cents))
    rows.append(_cash_sales_row(
        narration, date_out, contra_code or DEFAULT_CASH_SALES_CONTRA, -total))
    return rows


def cash_sales_to_csv(rows):
    """Serialise build_cash_sales_rows() output to a Xero-import CSV string."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CASH_SALES_HEADER)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()
