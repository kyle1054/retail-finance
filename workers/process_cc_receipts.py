#!/usr/bin/env python3
"""Out-of-band worker: extract + match credit-card receipts.

Runs on demand, from the upload trigger, or from an hourly schedule.
It is a NO-OP unless NW_CC_AI=1, and it exits immediately when there are no
pending receipts — so a frequent schedule is cheap.

Policy:
  - Auto-link a receipt to a transaction ONLY on a single exact match
    (amount + date + merchant all agree). Ambiguity (0 or >1 exact) is never
    auto-linked.
  - Anything less certain is recorded as a suggestion for human review.
  - On an auto-link, rename the receipt's download name to the transaction so
    downloads/zips are self-labelling.

Deploy: set NW_CC_AI=1 and run this file from a scheduled task (or it is
spawned best-effort on upload).
"""
import logging
import os
import sys

# Make the repo root importable so `import database` etc. resolve when this
# worker is run directly (e.g. `python workers/process_cc_receipts.py`).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from northwind.data import database as db  # noqa: E402
from northwind.services import storage  # noqa: E402
from northwind.cards import ai as cc_ai  # noqa: E402

log = logging.getLogger(__name__)

MAX_SUGGESTIONS_PER_RECEIPT = 5


def _rename_to_transaction(receipt, line):
    ext = os.path.splitext(receipt['file_path'])[1] or '.pdf'
    # If this receipt already covers other transactions, label it '_+Nmore' so a
    # multi-charge invoice isn't mislabelled as belonging to only one line.
    extra = max(0, db.count_cc_receipt_links(receipt['id']) - 1)
    name = cc_ai.download_name_for(
        line['reference'], line['line_date'], line['amount_cents'], ext,
        extra_count=extra)
    db.set_cc_receipt_download_name(receipt['id'], name)


def _remembered_coding(line, conn):
    """A remembered (code, name, how) for one line, or (None, None, None).

    Tries the line's own merchant key first, then every remembered key for the same
    brand — so a new Fuelstop branch inherits the decision made for another one instead
    of going back to the AI. The remembered code is re-validated: a retired or
    invalid code must never be applied blindly, since memory hits are written with
    'high' confidence and no review flag.
    """
    from northwind.cards import accounts as cc_accounts
    key = cc_ai.normalize_merchant(line['reference'])
    if len(key) < 3:
        return None, None, None
    exact = db.get_cc_merchant_map(key, conn=conn)
    family = db.get_cc_merchant_map_family(
        cc_ai.merchant_brand(line['reference']), conn=conn)
    code, name, how = cc_ai.resolve_remembered_account(exact, family)
    if code is None or not cc_accounts.is_valid(code):
        return None, None, None
    return code, (name or cc_accounts.name_for(code)), (how, key)


def suggest_accounts(limit=200, statement_id=None, use_ai=True):
    """Suggest a Xero account for spend lines needing coding.

    Merchant-memory first (a pure lookup, instant); the coding rules only when
    `use_ai` AND NW_CC_AI are on — so callers on a web request can pass
    use_ai=False to stay fast and leave the AI pass to the scheduled worker.
    Uses ONE shared DB connection for the whole batch. Returns lines coded.

    The AI pass is BATCHED (``cc_ai.CODING_BATCH_SIZE`` lines per call) and each
    line carries the detail from any receipt already attached to it, which is the
    difference between coding "SHOPFRONT R957" and coding "SHOPFRONT R957 — office
    chair, desk mat".
    """
    lines = db.claim_cc_lines_needing_coding(limit, statement_id=statement_id)
    conn = db.get_db()
    try:
        if not lines:
            return 0
        do_ai = use_ai and cc_ai.FEATURE_ENABLED
        coded = 0
        needs_ai = []
        for ln in lines:
            code, name, how = _remembered_coding(ln, conn)
            if code:
                kind, key = how
                note = (f"Remembered: '{key}' -> {code}." if kind == 'exact'
                        else f"Remembered for this merchant ('{key}') -> {code}.")
                db.set_cc_line_ai_coding(ln['id'], code, name, 'high', False, note,
                                         'memory', conn=conn)
                conn.commit()
                coded += 1
                continue
            if not do_ai:
                db.release_cc_line_coding_claim(ln['id'])
                continue  # no memory hit and AI off/deferred -> leave dirty for the cron
            needs_ai.append(ln)

        if needs_ai:
            # One read for the whole pass, not one per line.
            details = db.get_cc_receipt_details_for_lines(
                [ln['id'] for ln in needs_ai], conn=conn)
            size = max(1, int(cc_ai.CODING_BATCH_SIZE))
            for start in range(0, len(needs_ai), size):
                chunk = needs_ai[start:start + size]
                items = [{'reference': ln['reference'],
                          'amount_cents': ln['amount_cents'],
                          'reason': ln['reason'],
                          'line_date': ln['line_date'],
                          'receipt_text': details.get(ln['id'])}
                         for ln in chunk]
                sugs, error = cc_ai.suggest_accounts_batch(items)
                if error and error not in cc_ai.FATAL_PASS_ERRORS and len(items) > 1:
                    # A garbled or truncated response is usually provoked by ONE odd
                    # line, and batching means the other 19 were collateral. Retry
                    # them individually so the failure stays with the line that
                    # caused it — bounded to a single extra pass over this chunk.
                    log.info("cc coding: batch of %d failed (%s); retrying "
                             "individually", len(items), error)
                    sugs = [cc_ai.suggest_accounts_batch([one])[0][0]
                            for one in items]
                for ln, sug in zip(chunk, sugs):
                    if sug is None:
                        db.release_cc_line_coding_claim(ln['id'])
                        continue  # AI unavailable/failed -> retry next run
                    db.set_cc_line_ai_coding(
                        ln['id'], sug.account_code, sug.account_name, sug.confidence,
                        sug.needs_review, sug.rationale, 'ai', conn=conn)
                    coded += 1
                conn.commit()
                if error in cc_ai.FATAL_PASS_ERRORS:
                    # An exhausted quota or a dead key will fail every remaining
                    # batch identically. Release the rest so they stay dirty for the
                    # next run rather than firing a dozen more doomed calls.
                    for ln in needs_ai[start + size:]:
                        db.release_cc_line_coding_claim(ln['id'])
                    log.warning("cc coding: stopping this pass after %s; %d line(s) "
                                "left for the next run", error,
                                len(needs_ai) - (start + size))
                    break
        if coded:
            print(f"process_cc_receipts: coded {coded} line(s)")
        return coded
    finally:
        conn.close()


def run(limit=100, statement_id=None, code_accounts_ai=True, force=False):
    """Extract + match every pending receipt (optionally scoped to one month).

    Returns the number of receipts processed. Safe to call in-process from a web
    request (the "Match now" button) — it spawns nothing, just does the work.

    ``force=True`` retries receipts the scheduled run holds back (a terminal error,
    or one still inside the retry backoff). Pass it for anything a person triggered:
    "Match now" reporting "nothing waiting" when the cardholder can see their
    receipt sitting there is worse than spending one API call.
    """
    # Receipts are extracted and matched FIRST, then accounts are coded. The order
    # matters: linking a receipt marks its line for (re)coding, so doing extraction
    # first means a freshly uploaded statement gets coded off the receipt's line
    # items in the SAME run. Coding first meant the receipt detail always arrived a
    # run too late, and for a statement uploaded once, never at all.
    processed = _extract_and_match(limit, statement_id, force=force)

    # Account coding: merchant memory always; AI only when code_accounts_ai (the
    # in-request "Match now" passes False to stay fast — the cron runs the AI pass).
    suggest_accounts(limit=max(limit, 200), statement_id=statement_id,
                     use_ai=code_accounts_ai)
    return processed


def _extract_and_match(limit, statement_id, force=False):
    """Extract + match every pending receipt. Returns the number processed."""
    if not cc_ai.FEATURE_ENABLED:
        print("NW_CC_AI not enabled; receipt matching skipped.")
        return 0
    pending = db.claim_cc_receipts_pending_ai(limit, statement_id=statement_id,
                                              force=force)
    if not pending:
        return 0

    processed = 0
    # Lines auto-linked earlier in THIS batch, so two identical receipts (e.g.
    # two same-day Rideco trips of the same amount) don't both collapse onto the
    # one line — the second becomes a suggestion for review instead.
    taken_lines = set()
    for r in pending:
        try:
            data = storage.read(r['file_path'])
        except Exception as exc:
            # The file itself is unreachable (an object-store outage, a lost blob) — nothing to do
            # with the AI, and previously indistinguishable from a model failure.
            log.warning("receipt %s: cannot read %s: %s: %s", r['id'], r['file_path'],
                        type(exc).__name__, exc)
            db.set_cc_receipt_ai_status(r['id'], 'failed', error='storage_unreadable')
            continue

        extract, error = cc_ai.extract_receipt_with_error(
            data, r['content_type'] or 'application/octet-stream')
        if extract is None:
            # Record WHY (migration 0044): a dead key, an exhausted quota and an
            # unreadable slip need completely different responses, and a flat 'failed'
            # made them one state.
            db.set_cc_receipt_ai_status(r['id'], 'failed', error=error or 'unknown')
            continue

        # A zero or negative total is a failed read wearing the clothes of a real one.
        # Treating it as 'processed' put live receipt 95 (read as R0.00) into the
        # matching pool, and it also hides the failure from the portal's "could not be
        # read" count, so nobody knows to re-upload a clearer photo.
        readable = extract.total_cents is not None and extract.total_cents > 0
        status = 'processed' if readable else 'unreadable'
        db.set_cc_receipt_ai(
            r['id'], extract.vendor,
            extract.date.isoformat() if extract.date else None,
            extract.total_cents, extract.raw_json, status)

        lines = db.get_cc_spend_lines_for_matching(r['statement_id'])
        matches = cc_ai.match_receipt(extract, lines)
        pick = cc_ai.choose_auto_match(matches)

        # A total that disagrees with the slip's own subtotal + VAT means a number
        # was misread — and the amount is the anchor the whole match rests on. Still
        # offer the candidates as suggestions, but never auto-link on a figure we
        # already know not to trust: a silent wrong link is far worse than a row
        # someone has to confirm. (No extra work for the cardholder — confirming
        # matches is the admin's side of the screen.)
        if pick is not None and extract.total_disputed:
            log.info("receipt %s: not auto-linking — extracted total %s does not "
                     "reconcile with its own subtotal/VAT", r['id'],
                     extract.total_cents)
            pick = None

        # This receipt has already been placed — by a person, or by an earlier run.
        # Adding a SECOND link automatically would silently claim it covers a charge
        # nobody said it covers. The re-processing path is real now that "Match now"
        # forces retries of failed receipts: a slip the AI couldn't read, that an admin
        # then linked by hand, comes back through here the moment someone clicks the
        # button. Suggestions are still recorded, so a genuine multi-charge invoice can
        # be extended with one click.
        if pick is not None and db.count_cc_receipt_links(r['id']) > 0:
            log.info("receipt %s: not auto-linking — it is already linked to %d "
                     "transaction(s); recording suggestions instead",
                     r['id'], db.count_cc_receipt_links(r['id']))
            pick = None

        # Only auto-link onto a line that has no receipt yet (neither an existing
        # link nor one made earlier in this batch); otherwise leave it for review.
        linked = False
        if pick is not None and pick.line_id not in taken_lines:
            line = next((ln for ln in lines if ln['id'] == pick.line_id), None)
            linked = db.auto_link_cc_receipt_if_uncovered(r['id'], pick.line_id)
        if linked:
            db.add_cc_suggestion(pick.line_id, r['id'], pick.score, status='confirmed')
            taken_lines.add(pick.line_id)
            if line is not None:
                _rename_to_transaction(r, line)
        else:
            # Ambiguous, or the best line is already covered → ranked suggestions.
            for m in matches[:MAX_SUGGESTIONS_PER_RECEIPT]:
                db.add_cc_suggestion(m.line_id, r['id'], m.score)

        processed += 1

    print(f"process_cc_receipts: processed {processed} receipt(s)")
    return processed


if __name__ == '__main__':
    run()
