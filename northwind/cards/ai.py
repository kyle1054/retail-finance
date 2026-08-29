"""Credit-card receipt extraction, account coding, and matching.

Matching, tiering and filename logic are PURE Python (no network, no Flask/DB)
so they are unit-testable, and they are the part where the money risk lives —
which line a receipt gets attached to.

Extraction and account coding are LOCAL and deterministic in this build. There
is no hosted model and no network call anywhere in this module:

- **Extraction** reads a receipt's fields out of a library keyed by the file's
  own bytes (see `receipt_key`). A slip whose fields have been recorded — by a
  fixture, a demo seeder, or an operator who transcribed it once — extracts
  exactly the same way every time; anything else reports
  ``no_recorded_extraction`` and stays for a human, which is the same miss the
  worker already handles for an unreadable photo.
- **Coding** applies a keyword rule table over what is known about the charge
  (merchant, the cardholder's own reason, and any receipt detail), falling back
  to the chart's fallback account with a review flag rather than guessing.

Both are honest about being narrower than a vision model: they answer for what
they recognise and abstain loudly otherwise. Everything downstream — the tiered
amount agreement, the auto-link decision, merchant memory, self-labelling
download names — is unchanged and fully exercisable with no credentials at all.

Config (env):
  NW_CC_AI=1                enable the feature (worker + upload trigger)
  NW_RECEIPT_LIBRARY        JSON file of recorded extractions (see receipt_key)
"""
import os
import re
import json
import difflib
import hashlib
import logging
import datetime as _dt
from dataclasses import dataclass, field as _dc_field, fields as _dc_fields
from typing import Optional, List

log = logging.getLogger(__name__)


# ── Failure classification ───────────────────────────────────────────────────
# extract_receipt and suggest_account must never raise (the hourly worker has to keep
# going across a bad receipt), and they used to honour that by returning None from a
# bare `except Exception: return None` with no logging whatsoever. The cost of that
# was concrete: a slip nobody had recorded, a malformed record, a backend refusing
# work and a genuinely unreadable photo were all indistinguishable, both in the logs
# (there were none) and in the DB (one flat 'failed'). Diagnosing "the AI is kak"
# then required probing production by hand.
#
# So failures are still swallowed — but classified, logged, and returned to the caller
# so they can be persisted against the receipt.

def classify_ai_error(exc):
    """A short, stable reason string for an exception from extraction or coding.

    Matching on the message text rather than exception classes is deliberate: this
    has to classify whatever the configured backend throws, including exception
    hierarchies that are not importable here, and the useful distinctions (a
    malformed record vs a timeout vs a transport failure) surface in the message
    anyway. Unknown failures keep their exception type, which is still far more
    than the previous silence.

    The vocabulary is wider than the local implementations below can produce — it
    is the stored vocabulary of ``cc_receipts.ai_error`` (migration 0044), which
    outlives any one backend, and it is what the retry and fatal-pass rules key
    off.
    """
    name = type(exc).__name__
    text = "{}".format(exc).lower()

    # Exception TYPE first, because the most informative failures say nothing useful in
    # their message: a JSONDecodeError reads "Expecting value: line 1 column 1", which
    # contains no hint that it is a malformed model response.
    for needle, reason in (
        ("JSONDecodeError", "bad_model_response"),
        ("ValidationError", "bad_model_response"),
        ("TimeoutError", "timeout"),
        ("ConnectTimeout", "timeout"),
        ("ReadTimeout", "timeout"),
        ("ConnectionError", "network"),
        ("SSLError", "network"),
        ("PermissionError", "auth_denied"),
    ):
        if needle in name:
            return reason

    for needle, reason in (
        ("resource_exhausted", "quota_exhausted"),
        ("429", "quota_exhausted"),
        ("quota", "quota_exhausted"),
        ("rate limit", "quota_exhausted"),
        ("permission_denied", "auth_denied"),
        ("unauthenticated", "auth_denied"),
        ("api key not valid", "auth_invalid_key"),
        ("api_key_invalid", "auth_invalid_key"),
        ("401", "auth_denied"),
        ("403", "auth_denied"),
        ("timeout", "timeout"),
        ("timed out", "timeout"),
        ("deadline", "timeout"),
        ("unavailable", "service_unavailable"),
        ("503", "service_unavailable"),
        ("500", "service_error"),
        ("internal", "service_error"),
        ("connection", "network"),
        ("dns", "network"),
        ("ssl", "network"),
        ("unsupported", "unsupported_media"),
        ("mime", "unsupported_media"),
        ("safety", "blocked_by_safety"),
        ("blocked", "blocked_by_safety"),
        ("json", "bad_model_response"),
        ("validation", "bad_model_response"),
    ):
        if needle in text:
            return reason
    return "unknown:{}".format(name)

# ── Config ───────────────────────────────────────────────────────────────────
FEATURE_ENABLED = os.environ.get('NW_CC_AI', '0') == '1'

# Matching tolerances (tuneable). Philosophy: AMOUNT is the anchor, but it can
# legitimately differ from the receipt three ways — so we grade it rather than
# use one flat number, and always prefer an EXACT match before a fuzzy one:
#   exact  — within R1 (rounding).
#   drift  — a few Rand / a few % either way (Uber-style dynamic pricing,
#            fuel/surcharge rounding): charged amount ≠ invoice amount slightly.
#   tip    — charged MORE than the printed bill (a gratuity added at the table or
#            on the card machine, not shown on the slip). Asymmetric: UP only.
# Date and merchant stay forgiving — amount+date together pin down the line.
AMOUNT_TOLERANCE_CENTS = 100    # ±R1 = "exact" (rounding)
AMOUNT_DRIFT_CENTS = 500        # ±R5 …
AMOUNT_DRIFT_RATIO = 0.05       #   …or ±5%, whichever is larger = "drift" (Uber etc.)
TIP_MAX_RATIO = 0.15            # charged up to +15% over the printed bill = a "tip"
DATE_WINDOW_DAYS = 7            # purchase vs statement settlement lag (covers a weekend)
MERCHANT_MATCH_MIN = 0.55      # difflib ratio to count as a merchant-name match (lenient)
# Tiebreak thresholds, used only when several transactions are otherwise equally
# qualified (see _pick_unique). These decide by MARGIN over the runner-up rather than
# against an absolute bar, because the absolute bar is what was sending obvious
# matches to manual review ('Parkview Park Shopping C' scores 0.545 vs 'Advance Parkview
# Park' — clearly the right one, just under 0.55).
# 0.45 sits deliberately between the two things we measured: difflib scores ~0.39 for
# two genuinely unrelated short names ('Mystery Merchant' vs 'Some Other Shop'), and
# ~0.55 for a real partial match ('Parkview Park Shopping C' vs 'Advance Parkview Park').
# Below this floor the "winner" is noise, and a margin over an equally noisy
# runner-up means nothing.
MERCHANT_TIEBREAK_MIN = 0.45      # the winner still needs SOME real name signal
MERCHANT_TIEBREAK_MARGIN = 0.15   # …and must beat the next-best merchant by this much


@dataclass
class ReceiptExtract:
    vendor: Optional[str]
    date: Optional[_dt.date]
    total_cents: Optional[int]
    currency: Optional[str]
    confidence: float
    raw_json: str
    # Everything below is read off the SAME image in the SAME call, so it costs
    # nothing extra, and it exists for one reason: the coding model used to see
    # only "merchant + amount", which is why many lines landed on fallback
    # accounts. `summary` and `line_items` are what turn "SHOPFRONT R957" into
    # "office chair, desk mat" -> 6270. Defaults keep older callers/tests working.
    line_items: Optional[List[str]] = None
    summary: Optional[str] = None
    # Other names printed on the slip (operator, venue, parent company). Matching
    # tries all of them: an adversarial example had the model name the MALL ('Fairview Shopping
    # Centre') where the bank names the parking OPERATOR ('ADVANCE FAIRVIEW MALL'),
    # dropping that merchant score below the matching threshold. Whichever name the bank used,
    # one of these should hit.
    vendor_aliases: Optional[List[str]] = None
    subtotal_cents: Optional[int] = None
    vat_cents: Optional[int] = None
    vat_number: Optional[str] = None
    is_tax_invoice: Optional[bool] = None
    # Set when subtotal + VAT does not add up to the total (see _reconcile_total):
    # the amount is the matching anchor, so a misread total silently kills a match.
    total_disputed: bool = False

    def coding_context(self):
        """The receipt detail as a short text block for the account-coding prompt.

        Deliberately not the raw JSON: the coding model needs WHAT WAS BOUGHT, and
        feeding it a JSON blob full of cents integers and confidence scores buries
        that in noise. Returns '' when there is nothing useful to say, so the caller
        can skip the field entirely rather than pass 'None'.
        """
        bits = []
        if self.summary:
            bits.append(str(self.summary).strip())
        if self.line_items:
            items = [str(i).strip() for i in self.line_items if str(i or '').strip()]
            if items:
                bits.append("Items: " + "; ".join(items[:20]))
        if self.is_tax_invoice is False:
            bits.append("(not a valid tax invoice — no VAT number printed)")
        return " · ".join(bits)


@dataclass
class MatchResult:
    line_id: int
    score: float
    amount_ok: bool           # True only for an EXACT amount (±R1)
    amount_tier: Optional[str]  # 'exact' | 'drift' | 'tip' | None
    date_ok: bool
    merchant_ratio: float
    exact: bool
    # The statement's own merchant string. Carried so the auto-link decision can
    # tell "three charges from the SAME merchant" (interchangeable — pick any) from
    # "three different merchants that happen to share an amount" (must not guess).
    reference: Optional[str] = None
    # Signed days between the receipt date and the charge date (None if either is
    # unknown). Two R10 parking charges from one operator are only interchangeable on
    # the SAME day — a day apart they are separate trips, each with its own slip — so
    # the auto-link tiebreak needs the distance, not just "inside the window".
    date_delta: Optional[int] = None


# ── Extraction (local) ───────────────────────────────────────────────────────
# There is no hosted reader in this build, so a receipt's fields come out of a
# LIBRARY of recorded extractions, keyed by the SHA-256 of the file's own bytes.
#
# The bytes are the key rather than the filename or the receipt row id, for the
# same reason the upload path hashes them: the same photo re-uploaded under a
# different name, or attached to a second card, is the same slip and must extract
# identically, and a renamed file must not silently become a different receipt.
# It also means an entry cannot be pointed at the wrong file by accident.
#
# Entries come from fixtures, from a demo seeder, or from a JSON file named by
# NW_RECEIPT_LIBRARY: `{"<sha256>": {"vendor": …, "date": "2026-06-14",
# "total_cents": 34210, …}}`, using the field names of ReceiptFields below.
# A slip nobody has recorded reports 'no_recorded_extraction' and is left for a
# human — the same outcome the worker already handles for a photo too blurry to
# read, so nothing downstream needs a second failure path.

RECEIPT_LIBRARY_PATH = os.environ.get('NW_RECEIPT_LIBRARY', '').strip()

# {key: {field: value}}. The JSON file is merged in on first use and never
# re-read, so a lookup stays a dict hit rather than a stat + parse per receipt.
# Registrations win over the file: a test that records a slip must not be
# overridden by whatever a deployment happens to have on disk.
_RECORDED = {}
_library_loaded = False


@dataclass
class ReceiptFields:
    """One recorded extraction, before any of our own checks are applied.

    Deliberately not ReceiptExtract: this is what the record CLAIMS, and
    ReceiptExtract is what we are prepared to believe once the total has been
    cross-checked against the slip's own subtotal and VAT. Unknown keys in a
    record are ignored rather than raising, so a library written against a later
    field set still loads.
    """
    vendor: Optional[str] = None
    date: Optional[str] = None
    total_cents: Optional[int] = None
    currency: Optional[str] = None
    confidence: float = 0.0
    subtotal_cents: Optional[int] = None
    vat_cents: Optional[int] = None
    vat_number: Optional[str] = None
    is_tax_invoice: Optional[bool] = None
    line_items: List[str] = _dc_field(default_factory=list)
    summary: Optional[str] = None
    vendor_aliases: List[str] = _dc_field(default_factory=list)

    @classmethod
    def from_record(cls, record):
        known = {f.name for f in _dc_fields(cls)}
        return cls(**{k: v for k, v in dict(record).items() if k in known})

    def as_dict(self):
        return dict(vars(self))


def receipt_key(data):
    """The library key for one receipt's bytes."""
    return hashlib.sha256(data or b'').hexdigest()


def register_receipt(data, record):
    """Record what one slip says, keyed by its bytes. Returns the key.

    For fixtures and demo seeders: the pair (bytes, fields) is exactly what an
    operator transcribing a receipt by hand would supply.
    """
    key = receipt_key(data)
    _RECORDED[key] = dict(record)
    return key


def _library():
    global _library_loaded
    if not _library_loaded:
        # Set before reading, so a broken file is not re-parsed on every receipt.
        _library_loaded = True
        if RECEIPT_LIBRARY_PATH:
            try:
                with open(RECEIPT_LIBRARY_PATH, encoding='utf-8') as fh:
                    loaded = json.load(fh) or {}
                for key, record in loaded.items():
                    _RECORDED.setdefault(str(key), dict(record))
                log.info("receipt library %s: %d recorded extraction(s)",
                         RECEIPT_LIBRARY_PATH, len(loaded))
            except Exception as exc:
                # A misconfigured library is a deployment mistake, not a reason to
                # take the worker down: every receipt simply reports that nothing
                # was recorded. One loud line is what gets it fixed.
                log.warning("receipt library %s could not be read: %s: %s",
                            RECEIPT_LIBRARY_PATH, type(exc).__name__, exc)
    return _RECORDED


def _extract_fields(data, mime_type):
    """``(ReceiptFields | None, raw_record_text)`` for one file's bytes.

    The single seam every extraction goes through: replace this and the whole
    module reads receipts a different way, with the amount cross-check, the
    matching and the auto-link rules below untouched.

    ``mime_type`` is not part of the lookup — the bytes identify the slip on
    their own — but it stays in the signature because it is what a reader needs,
    and because the worker passing the WRONG stored type through was a real bug
    (HEIC uploads arriving as an untouched image/heic nothing could decode).
    """
    del mime_type
    record = _library().get(receipt_key(data))
    if record is None:
        return None, ''
    return ReceiptFields.from_record(record), json.dumps(record)


# SA standard VAT rate, used only to sanity-check a reconciling subtotal/VAT split.
_VAT_RECONCILE_SLACK_CENTS = 200   # ±R2 covers per-line VAT rounding on a long slip


def _reconcile_total(total, subtotal, vat):
    """True when subtotal + VAT agrees with the total (or can't be checked).

    A False here means the model read at least one of the three numbers wrong, and
    since the total is the anchor the whole match hangs on, that is worth knowing:
    the caller drops confidence and flags the receipt rather than letting a wrong
    amount quietly fail to match anything.
    """
    if total is None or subtotal is None or vat is None:
        return True                      # nothing to check against
    return abs((subtotal + vat) - total) <= _VAT_RECONCILE_SLACK_CENTS


def extract_receipt(data, mime_type):
    """Extract fields from one receipt image/PDF.

    Returns a ReceiptExtract, or None when nothing has been recorded for this
    slip or the record is unusable (the caller marks the receipt
    failed/unreadable). NEVER raises — the worker must keep going across a bad
    receipt.

    Prefer ``extract_receipt_with_error`` in new code: it returns the same value plus a
    classified reason for the failure, which is what makes an unrecorded slip
    distinguishable from a corrupt library. This wrapper is kept for callers that only
    want the value.
    """
    extract, _error = extract_receipt_with_error(data, mime_type)
    return extract


def extract_receipt_with_error(data, mime_type):
    """``(ReceiptExtract | None, error_reason | None)``.

    The error reason is one of the ``classify_ai_error`` strings, or
    ``'no_recorded_extraction'`` / ``'empty_extraction'``. It is short and stable so it
    can be stored in ``cc_receipts.ai_error`` and grouped over. Never raises.
    """
    try:
        parsed, raw = _extract_fields(data, mime_type)
    except Exception as exc:
        reason = classify_ai_error(exc)
        # exc_info only for genuinely unexpected failures: a malformed record is
        # operational noise and a stack trace per receipt would bury the signal.
        log.warning("cc extraction failed (%s, mime=%s): %s: %s", reason, mime_type,
                    type(exc).__name__, exc,
                    exc_info=reason.startswith("unknown:"))
        return None, reason

    if parsed is None:
        log.info("cc extraction: no recorded extraction for this receipt "
                 "(mime=%s, %d bytes)", mime_type, len(data or b""))
        return None, "no_recorded_extraction"
    if parsed.vendor is None and parsed.date is None and parsed.total_cents is None:
        # A record exists but says nothing to match on. Distinct from an
        # unrecorded slip, because the fix is different: this one was recorded
        # badly rather than not at all.
        log.warning("cc extraction: the recorded extraction has no vendor, date "
                    "or total (mime=%s)", mime_type)
        return None, "empty_extraction"

    confidence = float(parsed.confidence or 0.0)
    reconciles = _reconcile_total(
        parsed.total_cents, parsed.subtotal_cents, parsed.vat_cents)
    if not reconciles:
        # Cap rather than zero it: the total is still our best guess and may well
        # be the correct one, but it must not present as trustworthy.
        log.info("cc extraction: total %s does not reconcile with subtotal %s + "
                 "VAT %s for %r — capping confidence",
                 parsed.total_cents, parsed.subtotal_cents, parsed.vat_cents,
                 parsed.vendor)
        confidence = min(confidence, 0.4)
    return ReceiptExtract(
        vendor=(parsed.vendor or None),
        date=_parse_date(parsed.date),
        total_cents=parsed.total_cents,
        currency=(parsed.currency or None),
        confidence=confidence,
        raw_json=_raw_json_with_flags(raw, parsed, reconciles),
        line_items=(list(parsed.line_items or []) or None),
        summary=(parsed.summary or None),
        vendor_aliases=(list(parsed.vendor_aliases or []) or None),
        subtotal_cents=parsed.subtotal_cents,
        vat_cents=parsed.vat_cents,
        vat_number=(parsed.vat_number or None),
        is_tax_invoice=parsed.is_tax_invoice,
        total_disputed=(not reconciles),
    ), None


def _raw_json_with_flags(text, parsed, reconciles):
    """The stored extraction blob, with our own computed flags folded in.

    ``total_disputed`` is something WE work out, not something the record says, so it
    has to be written into the blob or it is lost the moment the dataclass goes out
    of scope — leaving nobody able to answer "why was this receipt not auto-linked?".
    Falls back to the parsed field dump if the raw text is not a JSON object.
    """
    try:
        data = json.loads(text or '')
        if not isinstance(data, dict):
            raise ValueError('not an object')
    except Exception:
        data = parsed.as_dict()
    data['total_disputed'] = not reconciles
    try:
        return json.dumps(data)
    except Exception:
        return json.dumps({'total_disputed': not reconciles})


def _parse_date(s):
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


# ── Matching (pure) ──────────────────────────────────────────────────────────

def _norm(s):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]', ' ', (s or '').lower())).strip()


def merchant_ratio(vendor, reference):
    """0..1 similarity between an extracted vendor and a statement reference.
    A substring hit (the statement reference usually embeds the merchant) scores
    1.0; otherwise a sequence ratio on the normalised strings."""
    v, r = _norm(vendor), _norm(reference)
    if not v or not r:
        return 0.0
    if v in r or r in v:
        return 1.0
    return difflib.SequenceMatcher(None, v, r).ratio()


def best_merchant_ratio(extract, reference):
    """The best merchant score across the extracted vendor AND its aliases.

    One slip legitimately carries several names — the parking operator, the mall it
    sits in, the registered company — and only the bank knows which one it printed.
    Committing to a single choice at extraction time is a coin flip: an adversarial example had
    the model returning 'Fairview Shopping Centre' where the statement said 'ADVANCE
    FAIRVIEW MALL', which took that receipt's merchant score below the matching threshold and would
    have cost it the tiebreak. Scoring every candidate and keeping the best removes
    the guess. Safe because the ratio is only ever used to CONFIRM a candidate the
    amount already selected — extra names can rescue a match, not invent one.
    """
    best = merchant_ratio(extract.vendor, reference)
    for alias in (extract.vendor_aliases or ()):
        if best >= 1.0:
            break
        best = max(best, merchant_ratio(alias, reference))
    return best


def amount_relation(receipt_cents, txn_cents):
    """How the charged amount relates to the receipt's printed total.

    Returns 'exact' | 'drift' | 'tip' | None. `receipt_cents` is the receipt
    total (positive); `txn_cents` is the statement charge (also passed positive).
    'tip' is asymmetric — only when the charge is HIGHER than the receipt (a
    gratuity the slip doesn't show); 'drift' is small and two-way (dynamic
    pricing / rounding)."""
    if receipt_cents is None or txn_cents is None:
        return None
    # A non-positive total is a FAILED read, not a real amount. Live receipt 95 was
    # stored as R0.00 with status 'processed', and because the drift band has a R5
    # floor, R0.00 registered as 'exact' against any charge up to R1 and 'drift' up to
    # R5 — so one unreadable slip could latch onto an unrelated small charge. The
    # amount is the anchor for the whole match; if we don't have one, there is no
    # match to make.
    if receipt_cents <= 0 or txn_cents <= 0:
        return None
    diff = txn_cents - receipt_cents          # + => charged more than the slip shows
    gap = abs(diff)
    if gap <= AMOUNT_TOLERANCE_CENTS:
        return 'exact'
    drift_band = max(AMOUNT_DRIFT_CENTS, int(round(receipt_cents * AMOUNT_DRIFT_RATIO)))
    if gap <= drift_band:
        return 'drift'
    if diff > 0 and diff <= int(round(receipt_cents * TIP_MAX_RATIO)):
        return 'tip'
    return None


def _date_delta(d1, d2_str):
    """Signed days from the charge date to the receipt date, or None if unknowable."""
    if not d1 or not d2_str:
        return None
    try:
        d2 = _dt.date.fromisoformat(str(d2_str)[:10])
    except Exception:
        return None
    return (d1 - d2).days


def _date_within(d1, d2_str, days):
    delta = _date_delta(d1, d2_str)
    return delta is not None and abs(delta) <= days


def match_receipt(extract, lines):
    """Score each candidate line against the receipt, best-first.

    `lines` are dict-like with: id, amount_cents (signed; spends are negative),
    line_date (str 'YYYY-MM-DD' or None), reference (merchant string). Returns
    MatchResults for lines with a real signal only (amount match, or date+merchant).
    `exact` means amount AND date AND merchant all agree — the auto-link gate.
    """
    # Score weight per amount tier — exact beats a tip/drift so an exact line
    # always sorts (and auto-links) ahead of a fuzzy one for the same receipt.
    _AMT_SCORE = {'exact': 0.6, 'drift': 0.5, 'tip': 0.45}
    results = []
    for ln in lines:
        amt_cents = abs(ln['amount_cents'])
        tier = amount_relation(extract.total_cents, amt_cents)
        amount_ok = (tier == 'exact')
        delta = _date_delta(extract.date, ln['line_date'])
        date_ok = delta is not None and abs(delta) <= DATE_WINDOW_DAYS
        ratio = best_merchant_ratio(extract, ln['reference'])
        merchant_ok = ratio >= MERCHANT_MATCH_MIN
        # Amount is the strong signal (graded by tier), then date, then merchant.
        # The date term is GRADED by closeness rather than a flat pass/fail: a
        # same-day charge should outrank one five days off, which is what decides the
        # ranking when a merchant bills the same amount repeatedly.
        if date_ok:
            date_score = 0.2 * (1.0 - abs(delta) / float(DATE_WINDOW_DAYS + 1))
        else:
            date_score = 0.0
        score = _AMT_SCORE.get(tier, 0.0) + date_score + 0.2 * ratio
        exact = amount_ok and date_ok and merchant_ok
        # Amount is the anchor: only surface a candidate when the amount is at
        # least plausibly related (exact, small drift, or a tip on top). A wrong
        # amount is dropped even if the name/date coincide — that stops a R90
        # parking slip landing on a R125 line that merely shares "Summit City".
        if tier is not None:
            results.append(MatchResult(
                line_id=ln['id'], score=round(score, 3), amount_ok=amount_ok,
                amount_tier=tier, date_ok=date_ok, merchant_ratio=round(ratio, 3),
                exact=exact, reference=ln['reference'], date_delta=delta))
    # line_id breaks score ties so the ranking is stable run to run — without it two
    # identical charges could swap places between runs and the "best" match would
    # depend on row order.
    results.sort(key=lambda r: (-r.score, r.line_id))
    return results


def choose_auto_match(matches):
    """Pick the ONE transaction to auto-link a receipt to, or None to leave it
    as a suggestion for review.

    Amount + date are the reliable signals, so if they pin down exactly one
    transaction we auto-link it — the merchant name is a bonus, not a
    requirement (bank labels rarely match the receipt, e.g. 'AIRPORT CO CIA Westport'
    vs 'AIRPORT SERVICES'). If several transactions share that amount+date, use
    the merchant name to break the tie; if that's still ambiguous, don't guess.

    An EXACT amount always wins first, and for an exact amount+date the merchant
    name is optional (bank labels rarely match). Only when no exact-amount line
    matches the date do we fall back to a 'tip' or 'drift' line (charged a bit
    more/less than the slip) — and because the amount no longer agrees exactly,
    a fuzzy match ALSO requires the merchant name to agree, so a receipt whose
    real line simply wasn't uploaded can't latch onto an unrelated same-date
    charge. Ambiguity at either tier returns None for a human to confirm.
    """
    exact = [m for m in matches if m.amount_tier == 'exact' and m.date_ok]
    if exact:
        return _pick_unique(exact)   # ambiguous exact → None, don't fall to fuzzy
    fuzzy = [m for m in matches
             if m.amount_tier in ('tip', 'drift') and m.date_ok
             and m.merchant_ratio >= MERCHANT_MATCH_MIN]
    pick = _pick_unique(fuzzy)
    if pick is not None:
        return pick
    # Last resort: the receipt carries NO readable date at all (a faded thermal slip).
    # Both branches above require date_ok, so such a receipt could never auto-link
    # even on a perfect amount + name match — it went to manual review for want of a
    # date we don't actually need to identify it.
    #
    # The test is `date_delta is None` on every candidate, NOT "nothing matched the
    # date". Those are different: a receipt legibly dated January against a June
    # statement is a wrong-month problem and MUST still be reviewed, whereas a receipt
    # with no date is merely missing one signal out of three. Requiring every delta to
    # be unknown means we only take this path when there was never a date to compare.
    if not all(m.date_delta is None for m in matches):
        return None
    dateless = [m for m in matches
                if m.amount_tier == 'exact' and m.merchant_ratio >= MERCHANT_MATCH_MIN]
    return _pick_unique(dateless)


def _pick_unique(cands):
    """The one candidate to auto-link from an equally-qualified set, or None.

    One candidate is easy. For several, the old rule demanded exactly one clearing
    the absolute merchant threshold, which threw away two very common real cases:

      1. THE SAME MERCHANT, TWICE. Three R10 'SUMMIT CITY PARKING' charges on one
         day, three matching R10 parking slips. The charges are interchangeable —
         every assignment is equally correct for the money, the month and the
         account — so refusing to choose is pure friction. We take the closest-dated
         one and let the caller's one-receipt-per-line guard spread the rest.
         Closest-dated, NOT lowest id: two R10 charges from one operator on DIFFERENT
         days are separate trips with their own slips, and picking the earliest would
         attach a 3 July slip to a 1 July charge.
      2. A CLEAR WINNER JUST UNDER THE BAR. Seven R10 parking charges from seven
         different operators; the slip reads 'Parkview Park Shopping C' and scores
         0.545 against 'Advance Parkview Park' — barely under the 0.55 threshold, yet
         far ahead of every other operator. What matters is the MARGIN over the
         runner-up, not the absolute score.

    So: group by merchant, then require the best group to beat the next-best group
    by a clear margin. Genuine ambiguity (several different merchants scoring alike,
    or a receipt whose vendor was unreadable) still returns None.
    """
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]

    groups = {}
    for m in cands:
        groups.setdefault(_norm(m.reference), []).append(m)

    def _best(group):
        return max(g.merchant_ratio for g in group)

    def _closest(group):
        """The charge nearest the receipt's own date; line_id only breaks a real tie."""
        return min(group, key=lambda m: (abs(m.date_delta) if m.date_delta is not None
                                         else DATE_WINDOW_DAYS + 1, m.line_id))

    ranked = sorted(groups.values(),
                    key=lambda g: (-_best(g), min(x.line_id for x in g)))
    top = ranked[0]
    # Case 1: every candidate is the same merchant → interchangeable. Still require
    # the name to actually agree; with an unreadable vendor we have no evidence that
    # this merchant is the right one at all, so we must not pick arbitrarily.
    if len(ranked) == 1:
        if _best(top) >= MERCHANT_MATCH_MIN:
            return _closest(top)
        return None
    # Case 2: a clear winner among several merchants.
    if (_best(top) >= MERCHANT_TIEBREAK_MIN
            and _best(top) - _best(ranked[1]) >= MERCHANT_TIEBREAK_MARGIN):
        return _closest(top)
    return None


# ── Self-labelling download filename ─────────────────────────────────────────

def _slug_merchant(s):
    s = re.sub(r'[^A-Za-z0-9]+', ' ', s or '').strip()
    return ''.join(w.capitalize() for w in s.split())[:40] or 'Receipt'


def download_name_for(reference, line_date, amount_cents, ext, extra_count=0):
    """Build a self-labelling download filename from a matched transaction, e.g.
    '2026-06-14_Greenfields_R342.10.pdf'. `extra_count` > 0 (one receipt covering
    several transactions) appends '_+Nmore'."""
    date = (str(line_date)[:10] if line_date else 'nodate')
    merchant = _slug_merchant(reference)
    name = f"{date}_{merchant}_R{abs(amount_cents) / 100.0:.2f}"
    if extra_count > 0:
        name += f"_+{extra_count}more"
    if ext and not ext.startswith('.'):
        ext = '.' + ext
    return name + (ext or '')


# ── Merchant memory: what an admin already decided ───────────────────────────

# Tokens dropped when building a merchant-memory key: transaction-noise words and
# common SA place/mall words, so "DL RIDECO WST" and "UBER SUMMIT" both key to
# "UBER". Over-stripping is safe — it just merges branches of one merchant.
_MERCHANT_NOISE = {
    'DL', 'FX', 'POS', 'PMT', 'CARD', 'PAYMENT', 'PURCHASE', 'ZA', 'RSA', 'SA',
    'PTY', 'LTD', 'CC', 'THE', 'AND',
}
_MERCHANT_PLACES = {
    'EASTPORT', 'WESTPORT', 'NORTHGATE', 'SOUTHBANK', 'MILLFORD', 'HARTLEY',
    'SUMMIT', 'CENTRALIA', 'CAMDEN', 'SOMERTON', 'BELMONT', 'CLARENDON',
    'ROSEWOOD', 'CROSSROADS', 'PARKVIEW', 'OAKVALE', 'HARBOUR', 'POINT',
    'ASHFORD', 'BROOKFIELD', 'FAIRVIEW', 'RIVERBEND', 'WELLSTONE', 'ELMWOOD', 'RAVINE',
    'WEST', 'EAST', 'NORTH', 'SOUTH', 'WESTERN', 'EASTERN', 'NORTHERN', 'CENTRAL',
    'MALL', 'CITY', 'SQUARE', 'CENTRE', 'CENTER', 'PLAZA', 'PARK',
    'BRANCH', 'STORE', 'AIRPORT', 'EPT', 'WPT', 'NGT', 'SBK', 'MLF',
}


def _merchant_tokens(reference):
    s = re.sub(r'[^A-Za-z ]+', ' ', (reference or '').upper())
    return [t for t in s.split()
            if len(t) > 1 and t not in _MERCHANT_NOISE and t not in _MERCHANT_PLACES]


# Two tokens, not three. The place-word list above is a hardcoded allowlist, so any
# unlisted branch name survives into the key: three tokens gave us stored keys like
# 'FUELSTOP MAIN ROAD', which the next Fuelstop on a different street could never match.
# The memory had too few reusable rows because of this. Two tokens
# still separates the cases that genuinely differ ('PIXELWORKS ADS' -> 6000 vs
# 'PIXELWORKS CLOUD' -> 6120), and the brand-level fallback below catches the rest.
_MERCHANT_KEY_TOKENS = 2


def normalize_merchant(reference):
    """Reduce a statement reference to a stable merchant key for the memory.

    Uppercase, drop digits/punctuation, strip transaction-noise + place words,
    keep the leading brand tokens. Heuristic (tunable) — a miss just means the
    AI is asked instead, which is safe.
    """
    return ' '.join(_merchant_tokens(reference)[:_MERCHANT_KEY_TOKENS]).strip()


def merchant_brand(reference):
    """Just the leading brand token ('FUELSTOP' from 'FUELSTOP MAIN ROAD'), or ''.

    Used to look up every remembered key for the same brand so a new branch can
    inherit a decision — see ``resolve_remembered_account``.
    """
    tokens = _merchant_tokens(reference)
    return tokens[0] if tokens else ''


def _field(row, name, default=None):
    """Read one column from a dict OR a ``sqlite3.Row``.

    Rows arrive straight from the DB helpers and have no ``.get()``, while the unit
    tests (and any future caller) pass plain dicts.
    """
    try:
        value = row[name]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def resolve_remembered_account(exact, family):
    """Pick a remembered account code, or None to ask the AI instead.

    ``exact`` is the row stored under this reference's own key (or None); ``family``
    is every remembered row for the same brand — 'FUELSTOP MAIN ROAD',
    'FUELSTOP CONVENIENCE', and so on.

    An exact key wins. Otherwise the brand's rows apply ONLY if they all agree on
    one account: a brand that has been coded two different ways ('PIXELWORKS ADS' -> 6000,
    'PIXELWORKS CLOUD' -> 6120) is genuinely ambiguous at brand level, and guessing there
    would be worse than the current miss, because a wrong remembered code is applied
    with 'high' confidence and no review flag. Abstaining just costs one trip
    through the coding rules.
    """
    if exact is not None and _field(exact, 'account_code'):
        return _field(exact, 'account_code'), _field(exact, 'account_name'), 'exact'
    rows = [r for r in (family or []) if _field(r, 'account_code')]
    codes = {_field(r, 'account_code') for r in rows}
    if len(codes) == 1:
        row = max(rows, key=lambda r: _field(r, 'hits', 0))
        return _field(row, 'account_code'), _field(row, 'account_name'), 'brand'
    return None, None, None


@dataclass
class AccountSuggestion:
    account_code: Optional[str]     # None => not codeable (e.g. personal)
    account_name: Optional[str]
    confidence: str                 # 'high' | 'medium' | 'low'
    needs_review: bool
    rationale: Optional[str]


def suggest_account(reference, amount_cents, reason, *, receipt_text=None):
    """Suggest the expense account for one transaction.

    Returns an AccountSuggestion, or None if the coding pass failed (caller
    leaves the line for the next run). Never raises.

    The suggested account_code is validated against cc_accounts — a rule can
    never propose a code that is not in the chart, and its name is replaced with
    our canonical one.

    Prefer ``suggest_account_with_error`` in new code (same value, plus a classified
    failure reason).
    """
    suggestion, _error = suggest_account_with_error(
        reference, amount_cents, reason, receipt_text=receipt_text)
    return suggestion


def suggest_account_with_error(reference, amount_cents, reason, *, receipt_text=None):
    """``(AccountSuggestion | None, error_reason | None)``. Never raises.

    A one-item call into ``suggest_accounts_batch``.
    """
    results, error = suggest_accounts_batch(
        [{'reference': reference, 'amount_cents': amount_cents, 'reason': reason,
          'receipt_text': receipt_text}])
    return (results[0] if results else None), error


# Failures that mean the REST of this pass is doomed too: there is no point coding
# batch 2 of 13 through a backend that just refused batch 1. The caller stops and
# lets the next scheduled run try again. A per-batch failure ('bad_model_response',
# 'empty_model_response') is deliberately absent — that is about the one batch's
# content, so the pass should carry on with the others.
#
# Deliberately wider than the local coder below can produce: this is the worker's
# contract with whatever does the coding, and the vocabulary is the stored one
# (migration 0044).
FATAL_PASS_ERRORS = frozenset({
    'auth_denied', 'auth_invalid_key', 'quota_exhausted', 'service_unavailable',
})


# How many transactions to code in one pass. The whole batch shares one commit in
# the worker, so this is what bounds how much work a crash can lose and how long a
# writer holds the database — 20 keeps both small.
CODING_BATCH_SIZE = 20


# ── Account coding (local) ───────────────────────────────────────────────────
# Coding is a keyword rule table over everything already known about a charge: the
# statement's merchant string, the cardholder's own reason, and any detail read off
# a receipt attached to the line. First match wins, and the order below is the
# priority — a charge that reads as two categories at once is a judgement call, and
# an ordered table makes which way it went reviewable instead of mysterious.
#
# Rules match WORDS OF A PURCHASE, never merchant names. A merchant list is a list
# of shops, and the one it does not contain is always the next one; merchant-level
# knowledge belongs in merchant memory (resolve_remembered_account), which learns
# from what an admin actually confirmed and is consulted before this ever runs.
#
# Some rules carry a second code for when the amount clears the small-asset
# threshold, because the same purchase genuinely codes differently either side of
# it: a R900 desk is an expense and a R9,000 one is an asset.
SMALL_ASSET_LIMIT_CENTS = 500_000        # R5,000

CODING_RULES = (
    # (keywords, account code, code when the amount is capitalised)
    (('hotel', 'lodge', 'guest house', 'accommodation', 'nights'), '6390', None),
    (('taxi', 'shuttle', 'ride', 'trip', 'flight', 'airline', 'airport',
      'bus', 'train', 'car hire', 'car rental', 'toll'), '6410', None),
    (('parking', 'parkade'), '6400', None),
    (('fuel', 'petrol', 'diesel'), '6230', None),
    (('courier', 'waybill', 'freight'), '6160', None),
    (('stationery', 'printing', 'paper', 'pens', 'toner', 'ink',
      'cartridge'), '6240', None),
    (('laptop', 'computer', 'monitor', 'keyboard', 'printer', 'ssd',
      'hard drive', 'tablet'), '6270', '6480'),
    (('chair', 'desk', 'shelving', 'shelf', 'cabinet', 'mat'), '6270', '6470'),
    (('software', 'subscription', 'saas', 'hosting', 'domain'), '6290', '6450'),
    (('airtime', 'data bundle', 'telephone', 'internet', 'fibre', 'sim'),
     '6310', None),
    (('electricity', 'municipal', 'rates and taxes'), '6030', None),
    (('cleaning', 'detergent', 'consumables'), '6180', None),
    (('packaging', 'boxes', 'bubble wrap'), '6100', None),
    (('repair', 'repairs', 'maintenance', 'plumber', 'electrician'), '6340', None),
    (('security', 'alarm', 'armed response'), '6260', None),
    (('insurance',), '6190', None),
    (('legal', 'attorney', 'advocate'), '6280', None),
    (('audit',), '6060', None),
    (('accounting', 'bookkeeping'), '6080', None),
    (('training', 'course', 'workshop', 'seminar'), '6070', None),
    (('advertising', 'marketing', 'billboard', 'signage'), '6000', None),
    (('licence', 'license', 'permit'), '6370', None),
    (('bank charges', 'bank fee', 'bank fees'), '6040', None),
    # Last: these words turn up inside longer item lists, and a specific item
    # ('office chair') should win over an incidental 'coffee' further down a slip.
    (('coffee', 'lunch', 'refreshments', 'snacks', 'groceries', 'catering',
      'milk'), '6330', None),
)

_RULE_PATTERNS = None


def _rule_patterns():
    """CODING_RULES with each keyword group compiled to one word-boundary regex.

    Word boundaries, not substrings: 'car' inside 'card' and 'ride' inside a
    merchant's registered name are exactly the false positives that make a rule
    table untrustworthy.
    """
    global _RULE_PATTERNS
    if _RULE_PATTERNS is None:
        _RULE_PATTERNS = [
            (re.compile(r'\b(?:%s)\b' % '|'.join(re.escape(k) for k in keywords)),
             code, capital_code)
            for keywords, code, capital_code in CODING_RULES]
    return _RULE_PATTERNS


@dataclass
class _Proposal:
    """What a rule proposes, before validation against the chart of accounts."""
    account_code: Optional[str] = None
    account_name: Optional[str] = None
    confidence: str = 'low'
    needs_review: bool = True
    rationale: Optional[str] = None


def _code_transaction(item, cc_accounts):
    """The account for one charge. Returns an AccountSuggestion, never None.

    Confidence tracks WHERE the keyword was found, because that is what a reviewer
    needs to know. A word in the cardholder's reason or on the receipt describes
    what was actually bought; the same word inside a bank merchant string is an
    inference from an abbreviation, so it is offered with a review flag rather than
    quietly applied.

    An unmatched charge lands on the fallback account flagged for review rather
    than being requeued: a charge nobody can categorise is still one finance has
    to see, and a line that keeps returning to the queue is invisible.
    """
    described = ' '.join([str(item.get('reason') or ''),
                          str(item.get('receipt_text') or '')]).lower()
    merchant = str(item.get('reference') or '').lower()
    amount = abs(item.get('amount_cents') or 0)

    for pattern, code, capital_code in _rule_patterns():
        hit = pattern.search(described)
        from_description = hit is not None
        if hit is None:
            hit = pattern.search(merchant)
        if hit is None:
            continue
        word = hit.group(0)
        if capital_code and amount >= SMALL_ASSET_LIMIT_CENTS:
            code = capital_code
            note = ("'%s' over the R%d small-asset threshold, so capitalised"
                    % (word, SMALL_ASSET_LIMIT_CENTS // 100))
        else:
            note = "'%s'" % word
        return _suggestion_from(
            _Proposal(account_code=code,
                      confidence='medium' if from_description else 'low',
                      needs_review=not from_description,
                      rationale='Coded from %s in %s.'
                                % (note, 'the purchase description'
                                   if from_description else 'the merchant name')),
            item.get('reference'), cc_accounts)

    return _suggestion_from(
        _Proposal(account_code=cc_accounts.FALLBACK_CODE,
                  rationale='Nothing in the merchant name, the reason or the '
                            'receipt matched a coding rule.'),
        item.get('reference'), cc_accounts)


def suggest_accounts_batch(items):
    """Code up to ``CODING_BATCH_SIZE`` transactions in one pass.

    ``items`` is a list of dicts with ``reference``, ``amount_cents``, ``reason``,
    and optionally ``line_date`` / ``receipt_text``.

    Returns ``(results, error_reason)`` where ``results`` is a list the SAME length
    as ``items``, positionally aligned. On a failure it is all-``None`` plus a
    classified reason — the caller re-queues those lines. Never raises.

    All-or-nothing on purpose: a half-applied batch would leave the caller unable
    to tell a line that was deliberately left alone from one that was lost.
    """
    items = list(items or [])
    if not items:
        return [], None
    from northwind.cards import accounts as cc_accounts
    try:
        return [_code_transaction(item, cc_accounts) for item in items], None
    except Exception as exc:
        error = classify_ai_error(exc)
        log.warning("cc coding failed (%s) for a batch of %d: %s: %s", error,
                    len(items), type(exc).__name__, exc,
                    exc_info=error.startswith("unknown:"))
        return [None] * len(items), error


def _suggestion_from(p, reference, cc_accounts):
    """Validate one proposed coding into an AccountSuggestion.

    The gate every suggestion passes through: the code must be one of ours, and
    the name stored is our canonical one, not whatever the proposer called it.
    """
    conf = (p.confidence or 'low').strip().lower()
    if conf not in ('high', 'medium', 'low'):
        conf = 'low'
    code = (p.account_code or '').strip() or None
    if code is None:
        return AccountSuggestion(None, None, conf, True, p.rationale)
    name = cc_accounts.name_for(code)
    if name is None:
        # Retired or mistyped code (e.g. 6205) → the fallback account + review.
        # Worth a log line: a rule that proposes codes which do not exist is a
        # chart-of-accounts drift, and it was previously silent.
        log.info("cc coding: unknown account code %r proposed for %r; "
                 "falling back to %s", code, reference, cc_accounts.FALLBACK_CODE)
        fb = cc_accounts.FALLBACK_CODE
        return AccountSuggestion(fb, cc_accounts.name_for(fb), 'low', True,
                                 (p.rationale or '') + " [code not recognised]")
    return AccountSuggestion(code, name, conf, bool(p.needs_review), p.rationale)
