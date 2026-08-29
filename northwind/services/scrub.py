"""Redact card numbers (PANs) — and, for receipt-OCR output, other personal
data — from any text before it is stored, logged, or returned by the model.

PCI-DSS lets us keep at most the first six and last four digits of a card
number; we keep **only the last four** and mask everything before it. The last
four on its own is *not* cardholder data, so storing it is fine and keeps the
whole app out of PCI scope.

Two levels:
  - ``mask_pans`` / ``scrub_obj`` — PAN-only. Used on the free-text ``reference``
    of imported statement lines (belt-and-braces; Xero recon exports don't
    normally carry a PAN). Cheap and conservative, safe on merchant strings.
  - ``scrub_pii`` / ``scrub_pii_obj`` — PANs **plus** the personal data a
    scanned invoice/receipt can carry that the OCR model echoes back: email
    addresses, SA phone numbers and SA ID numbers. Used on everything the
    receipt-OCR model returns (``ai_vendor`` and the ``ai_raw_json`` blob)
    before it touches the DB. POPIA data-minimisation: we never persist model
    output containing this PII in the clear.

This module is pure-Python with no app dependencies, so it's trivially testable
(see tests/test_scrub.py).
"""

import re

__all__ = [
    "mask_pans", "scrub_obj", "contains_pan",
    "mask_pii", "scrub_pii", "scrub_pii_obj",
]

_MASK = "•"  # • — the masked-digit glyph

# A run of 13–19 digits, allowing at most one space or hyphen between digits.
# This matches the common groupings (4-4-4-4 Visa/MC, 4-6-5 Amex) and bare runs.
# We require ≥13 digits so dates, phone numbers and amounts don't match.
_CANDIDATE = re.compile(r"\d(?:[ -]?\d){12,18}")


def _luhn_ok(digits):
    """True if `digits` (a string of digits) passes the Luhn checksum — every
    real card number does. Used to avoid masking long invoice/order numbers
    that happen to have no separators."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _mask_token(token):
    """Keep the last four digits of `token`; mask every earlier digit; leave
    any spaces/hyphens in place so the shape stays readable."""
    digit_positions = [i for i, c in enumerate(token) if c.isdigit()]
    keep = set(digit_positions[-4:])  # the last four digits survive
    return "".join(
        _MASK if (c.isdigit() and i not in keep) else c
        for i, c in enumerate(token)
    )


def _replace(m):
    token = m.group(0)
    digits = [c for c in token if c.isdigit()]
    if not (13 <= len(digits) <= 19):
        return token
    has_separator = any(c in " -" for c in token)
    # Separator-grouped → card-shaped, mask on sight. A bare digit run is only
    # masked if it passes Luhn, so we don't clobber long reference numbers.
    if not has_separator and not _luhn_ok("".join(digits)):
        return token
    return _mask_token(token)


def mask_pans(text):
    """Return `text` with any card-number-shaped substring masked down to its
    last four digits. Non-strings (None, ints, floats) pass through unchanged."""
    if not isinstance(text, str):
        return text
    return _CANDIDATE.sub(_replace, text)


def contains_pan(text):
    """True if `text` contains something `mask_pans` would redact. Handy for
    tests and assertions ('this string must never be stored as-is')."""
    return isinstance(text, str) and mask_pans(text) != text


def scrub_obj(obj):
    """Recursively mask PANs through a JSON-ish structure (dict / list / str),
    e.g. the ai_raw_json blob returned by receipt OCR before it's persisted."""
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_obj(v) for v in obj]
    return mask_pans(obj)


# ── Wider PII (for receipt-OCR output) ────────────────────────────────────────
# A scanned tax invoice can carry the customer's email, phone or ID number, which
# the OCR model then echoes back into ai_vendor / ai_raw_json. mask_pans covers
# card numbers; these cover the rest. Kept deliberately SA-specific and anchored
# so they don't clobber amounts, dates or invoice refs.

# Email: mask the local part, keep the domain (the domain is usually a merchant,
# not PII, and keeping it leaves the text legible for debugging).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")

# SA phone: a local 0XXXXXXXXX (10 digits) or +27XXXXXXXXX, with optional spaces
# or hyphens between groups. Anchored so a longer digit run (a PAN/ID) or a bare
# amount can't match. Last 3 digits are kept so the shape stays recognisable.
_PHONE_RE = re.compile(r"(?<![\w+])(?:\+?27|0)(?:[ \-]?\d){9}(?![\w])")

# SA ID: exactly 13 digits. Only masked when the leading 6 form a plausible
# YYMMDD date AND the whole thing passes Luhn (real SA IDs do) — that pairing
# keeps false positives (a 13-digit reference) essentially nil. Fully masked:
# unlike a card, no digits of an ID number are worth keeping.
_SA_ID_RE = re.compile(r"(?<!\d)\d{13}(?!\d)")


def _mask_digits(token, keep_last):
    """Mask every digit in `token` except the final `keep_last`; leave separators
    (spaces/hyphens/@) in place so the shape stays readable. keep_last=0 masks
    all digits."""
    positions = [i for i, c in enumerate(token) if c.isdigit()]
    keep = set(positions[-keep_last:]) if keep_last else set()
    return "".join(
        _MASK if (c.isdigit() and i not in keep) else c
        for i, c in enumerate(token)
    )


def _mask_email(m):
    return _MASK * 3 + "@" + m.group(1)


def _mask_phone(m):
    return _mask_digits(m.group(0), keep_last=3)


def _looks_like_sa_id(digits):
    """A 13-digit run whose first 6 read as YYMMDD and which passes Luhn — the
    signature of a real South African ID number."""
    try:
        month, day = int(digits[2:4]), int(digits[4:6])
    except ValueError:
        return False
    return 1 <= month <= 12 and 1 <= day <= 31 and _luhn_ok(digits)


def _replace_sa_id(m):
    token = m.group(0)
    return _mask_digits(token, keep_last=0) if _looks_like_sa_id(token) else token


def mask_pii(text):
    """Return `text` with card numbers, email addresses, SA phone numbers and SA
    ID numbers masked. Non-strings pass through unchanged.

    Order matters: mask emails first (so the phone rule can't nibble digits out
    of an address), then SA IDs (exact 13-digit, fully masked), then PANs
    (13–19 digit, last-4 kept), then phones (10-digit, last-3 kept)."""
    if not isinstance(text, str):
        return text
    text = _EMAIL_RE.sub(_mask_email, text)
    text = _SA_ID_RE.sub(_replace_sa_id, text)
    text = mask_pans(text)
    text = _PHONE_RE.sub(_mask_phone, text)
    return text


# Alias: "scrub this string of all PII we redact" reads better at call sites.
scrub_pii = mask_pii


def scrub_pii_obj(obj):
    """Like ``scrub_obj`` but masks the wider PII set (see ``mask_pii``)
    recursively through a JSON-ish structure."""
    if isinstance(obj, dict):
        return {k: scrub_pii_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_pii_obj(v) for v in obj]
    return mask_pii(obj)
