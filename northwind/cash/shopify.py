"""Pure parser for Shopify's monthly retail-payments CSV export."""
import csv
import hashlib
import io
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


REQUIRED_HEADERS = (
    'POS location name', 'Payment gateway', 'Order name', 'Transactions',
    'Gross payments', 'Refunded payments', 'Net payments',
)
# The app-wide Flask cap is 150 MB (northwind/core.py), so this is the real ceiling for a
# Shopify upload — the caller has already buffered the body by the time we see it.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# Sanity ceilings. Anything past these is a typo or a corrupt export, never a real
# month of retail cash, and they keep the parsed values inside the signed 64-bit
# range SQLite stores — a bare Decimal('1e400') would sail straight past it.
MAX_AMOUNT = Decimal('100000000')       # R100 million on one CSV row
MAX_TRANSACTIONS = 10000000
MAX_EXPONENT = 30                       # see _number: guards decimal.Overflow


def _number(value):
    """Return a CSV numeric cell as a finite Decimal, or None if it is not one.

    Infinity/NaN survive Decimal() and only blow up later inside quantize() or
    int() — as OverflowError, or as a ValueError worded for a programmer — so
    they are rejected here and reported like any other unreadable cell.

    The exponent check is not redundant with the caller's MAX_AMOUNT test:
    `Decimal('1e1000000000')` is finite, so it reaches the caller, and merely
    taking `abs()` of it raises decimal.Overflow. adjusted() reads the exponent
    without doing any arithmetic, so nothing can raise before we have judged it.
    """
    raw = str(value or '').strip().replace(',', '').replace('R', '').strip()
    if raw in ('', '-'):
        return Decimal(0)
    try:
        number = Decimal(raw)
    except (ArithmeticError, ValueError):
        return None
    if not number.is_finite() or abs(number.adjusted()) > MAX_EXPONENT:
        return None
    return number


def _cents(value, label, row_number):
    number = _number(value)
    if number is None or abs(number) > MAX_AMOUNT:
        raise ValueError(f'Row {row_number}: {label} is not a valid amount.')
    try:
        return int((number * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, OverflowError, ValueError):
        raise ValueError(f'Row {row_number}: {label} is not a valid amount.')


def parse_shopify_cash_csv(data):
    """Return normalized cash-gateway rows and the source SHA-256 digest."""
    if not data:
        raise ValueError('Choose a Shopify CSV to upload.')
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError('The Shopify CSV is larger than 10 MB.')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('The Shopify CSV must be UTF-8 encoded.')
    reader = csv.DictReader(io.StringIO(text))
    headers = tuple((h or '').strip() for h in (reader.fieldnames or ()))
    missing = [h for h in REQUIRED_HEADERS if h not in headers]
    if missing:
        raise ValueError('Shopify CSV is missing: ' + ', '.join(missing) + '.')

    rows = []
    for row_number, raw in enumerate(reader, start=2):
        location = (raw.get('POS location name') or '').strip()
        gateway = (raw.get('Payment gateway') or '').strip()
        if not location and not gateway:
            continue
        if gateway.casefold() != 'cash':
            continue
        if not location:
            raise ValueError(f'Row {row_number}: POS location name is required.')
        count = _number(raw.get('Transactions'))
        if count is None or count != count.to_integral_value():
            raise ValueError(f'Row {row_number}: Transactions must be a whole number.')
        if count < 0 or count > MAX_TRANSACTIONS:
            raise ValueError(
                f'Row {row_number}: Transactions must be a non-negative whole number '
                f'below {MAX_TRANSACTIONS:,}.')
        transactions = int(count)
        gross_cents = _cents(raw.get('Gross payments'), 'Gross payments', row_number)
        refunded_cents = _cents(raw.get('Refunded payments'), 'Refunded payments', row_number)
        net_cents = _cents(raw.get('Net payments'), 'Net payments', row_number)
        if gross_cents + refunded_cents != net_cents:
            raise ValueError(
                f'Row {row_number}: Gross payments plus refunds does not equal net payments.')
        rows.append({
            'source_row': row_number,
            'pos_location_name': location,
            'payment_gateway': gateway,
            'order_name': (raw.get('Order name') or '').strip(),
            'transactions': transactions,
            'gross_cents': gross_cents,
            'refunded_cents': refunded_cents,
            'net_cents': net_cents,
        })
    if not rows:
        raise ValueError('The Shopify CSV contains no cash-payment rows.')
    return rows, hashlib.sha256(data).hexdigest()
