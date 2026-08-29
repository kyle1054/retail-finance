"""Xero expense accounts a credit-card charge can be coded to.

Single source of truth for (a) validating the AI's suggested account_code — it
can never invent a code that isn't here — and (b) the admin's account dropdown.
Kept broad (wider than the shortlist the matcher suggests from) so the
admin can override to any plausible expense account.

Codes are strings (some are dotted, e.g. '6000.1'). Names are illustrative; align them with your own ledger.
"""

ACCOUNTS = {
    # ── Travel (NEW split — an older code retired) ──────────────────────────────────────
    '6390': 'Local Travel - Accommodation',
    '6400': 'Local Travel - Other',
    '6410': 'Local Travel - Transport',
    '6420': 'Overseas Travel - Accommodation',
    '6430': 'Overseas Travel - Transport',
    '6440': 'Overseas Travel - Other',
    # ── Operating expenses ────────────────────────────────────────────────────
    '6000': 'Advertising',
    '6000.1': 'Advertising - Sponsorship',
    '6000.2': 'Advertising - In-store',
    '6010': 'Creative Services',
    '6020': 'Merchandising Supplies',
    '6030': 'Utilities',
    '6040': 'Bank Charges',
    '6050': 'Card Processing Fees',
    '6060': 'External Audit',
    '6070': 'Training & Development',
    '6080': 'Accounting Services',
    '6090': 'Ecommerce Fees',
    '6100': 'Packaging',
    '6110': 'Media Production',
    '6120': 'Website Maintenance',
    '6130': 'Delivery - Customer Orders',
    '6140': 'Delivery - Stock Transfers',
    '6150': 'Charitable Giving',
    '6160': 'Delivery - Domestic Courier',
    '6170': 'Sundry Expenses',
    '6180': 'Cleaning & Consumables',
    '6190': 'Insurance',
    '6200': 'Equipment Rental',
    '6210': 'Professional Fees',
    '6220': 'Delivery - International Courier',
    '6230': 'Vehicle Costs',
    '6240': 'Printing & Stationery',
    '6250': 'Warehousing',
    '6260': 'Security Services',
    '6270': 'Minor Equipment',
    '6280': 'Legal Services',
    '6290': 'Software Subscriptions',
    '6300': 'Agency Fees',
    '6310': 'Telephone & Internet',
    '6330': 'Staff Amenities',
    '6340': 'Repairs & Maintenance',
    '6350': 'Staff Housing',
    '6360': 'Recruitment',
    '6370': 'Licence Fees',
    # ── Fixed assets (capitalised — over the R5,000 small-asset threshold) ─────
    '6450': 'Software Licences',
    '6460': 'Office Equipment',
    '6470': 'Fixtures & Fittings',
    '6480': 'Computer Hardware',
}

# Safe fallback when nothing else fits.
FALLBACK_CODE = '6170'


def name_for(code):
    """Canonical Xero name for a code, or None if the code isn't one of ours.
    Used to validate + normalise the AI's output (we trust our name, not its)."""
    return ACCOUNTS.get((code or '').strip())


def is_valid(code):
    return (code or '').strip() in ACCOUNTS


def choices():
    """(code, name) pairs sorted by code, for the admin dropdown."""
    def _key(c):
        try:
            return (0, float(c))
        except ValueError:
            return (1, c)
    return [(c, ACCOUNTS[c]) for c in sorted(ACCOUNTS, key=_key)]
