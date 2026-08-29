"""Credit Card Reconciliation — admin side.

Bulk-upload Xero credit-card recon exports. Each file self-identifies its card
(see credit_card_parser), auto-provisioning the card and merging its
unreconciled lines (idempotent — re-uploads never duplicate or wipe receipts).
The landing page is a grid of card tiles showing only the count of spends still
needing a receipt. Cardholder portal + receipt uploads come next.

Access: admin credit-card endpoints are super-only. Cardholder portal endpoints
are separately scoped to the logged-in person's explicit card grants.
"""
import io
import os
import sys
import time
import uuid
import secrets
import zipfile
import hashlib
import subprocess

from flask import (render_template, request, redirect, url_for, flash, abort,
                   session, send_file, jsonify)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from northwind.data import database as db
from northwind.services import scrub
from northwind.services import security
from northwind.services import storage
from northwind.cards import ai as cc_ai
from northwind.cards import accounts as cc_accounts
from northwind.core import app
from northwind.cards.parser import parse_workbook
from northwind.cards import reminders
from northwind.services import images, mailer

_ALLOWED_EXT = ('.xlsx', '.xlsm')

# Unambiguous alphabet for generated passwords (no O/0/I/1/L confusion).
_PW_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def _generate_password():
    """A readable, shareable password like 'NORTHWIND-7K4P-9QXM'."""
    block = lambda: ''.join(secrets.choice(_PW_ALPHABET) for _ in range(4))
    return f'NORTHWIND-{block()}-{block()}'


# ── One-shot credential hand-off (server-side) ────────────────────────────────
# A freshly-generated cardholder password is shown ONCE in a copyable panel after
# a grant/reset. We must not put that plaintext in the client session cookie
# (Flask signs it but does NOT encrypt it — the value would be base64-readable in
# the cookie for that round-trip). Instead we stash it in this in-process store
# keyed by a random token; only the opaque token rides in the session cookie, and
# the credential is popped (one-shot) when the card page next renders. Single
# worker only (like security.py's throttle) — correct for a single-instance deployment.
_PENDING_CREDENTIALS = {}          # token -> (created_monotonic, {email, password})
_CREDENTIAL_TTL = 600              # seconds; a token unread after 10 min is dropped


def _prune_credentials():
    now = time.monotonic()
    stale = [t for t, (ts, _) in _PENDING_CREDENTIALS.items()
             if now - ts > _CREDENTIAL_TTL]
    for t in stale:
        _PENDING_CREDENTIALS.pop(t, None)


def _stash_credential(email, password):
    """Hold a one-time credential server-side; put only its token in the session."""
    _prune_credentials()
    token = secrets.token_urlsafe(16)
    _PENDING_CREDENTIALS[token] = (time.monotonic(), {'email': email, 'password': password})
    session['cc_new_credential_token'] = token


def _pop_credential():
    """Return the pending {email, password} for this session once, then forget it."""
    token = session.pop('cc_new_credential_token', None)
    if not token:
        return None
    _prune_credentials()
    entry = _PENDING_CREDENTIALS.pop(token, None)
    return entry[1] if entry else None


def _wants_json():
    """True when the caller is our in-page fetch (progressive enhancement): the
    admin coding UI posts with X-Requested-With: fetch and expects JSON, while a
    no-JS form post falls through to the usual redirect."""
    return (request.headers.get('X-Requested-With') == 'fetch'
            or 'application/json' in (request.headers.get('Accept') or ''))


_LINE_UPLOAD_TOKEN_MAX_AGE = 30 * 60


def _line_upload_token(card_id, statement_id, line_id):
    """Sign the exact rendered receipt target for this authenticated user."""
    serializer = URLSafeTimedSerializer(app.secret_key, salt='cc-line-receipt-upload')
    return serializer.dumps({
        'uid': session.get('uid'),
        'card_id': int(card_id),
        'statement_id': int(statement_id),
        'line_id': int(line_id),
    })


def _valid_line_upload_token(token, card_id, statement_id, line_id):
    """Fail closed when a target token is missing, stale, copied or altered."""
    if not token or session.get('uid') is None:
        return False
    serializer = URLSafeTimedSerializer(app.secret_key, salt='cc-line-receipt-upload')
    try:
        payload = serializer.loads(token, max_age=_LINE_UPLOAD_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return (payload.get('uid') == session.get('uid')
            and payload.get('card_id') == int(card_id)
            and payload.get('statement_id') == int(statement_id)
            and payload.get('line_id') == int(line_id))


def _statement_progress(card_id, statement_id, show_reconciled=False):
    """Recompute the completeness counts for one statement — the same numbers the
    cc_card view derives (see its 'Completeness banner' block), so an AJAX save
    can hand the client server-truth totals instead of recomputing in JS.

    Returns {total_spend, coded_count, receipted_count, reconciled_count,
    need_count} for the outstanding spend lines currently shown."""
    if not statement_id:
        return {'total_spend': 0, 'coded_count': 0, 'receipted_count': 0,
                'reconciled_count': 0, 'need_count': 0}
    lines = db.get_cc_card_lines(card_id, statement_id=statement_id)
    receipts = {r['id']: r for r in db.list_cc_receipts(statement_id)}
    covered = set()
    for lk in db.list_cc_receipt_links(statement_id):
        if lk['receipt_id'] in receipts:
            covered.add(lk['line_id'])
    reconciled_count = sum(1 for l in lines if l['xero_reconciled'])
    if not show_reconciled:
        lines = [l for l in lines if not l['xero_reconciled']]
    working = [l for l in lines if l['category'] == 'spend' and l['status'] == 'outstanding']
    return {
        'total_spend': len(working),
        'coded_count': sum(1 for l in working if l['xero_account_code']),
        'receipted_count': sum(1 for l in working if l['personal'] or l['id'] in covered),
        'reconciled_count': reconciled_count,
        'need_count': sum(1 for l in working if not (l['personal'] or l['id'] in covered)),
    }


def _kick_ai_worker():
    """Best-effort: nudge the out-of-band receipt worker after an upload so
    extraction feels upload-triggered. Detached and wrapped so it can NEVER
    affect the upload response; a scheduled task is the reliable fallback."""
    if not cc_ai.FEATURE_ENABLED:
        return
    try:
        # Resolve the worker via its module file (location-independent) rather
        # than relative to THIS file — this route module now lives under northwind/,
        # while the worker lives at workers/process_cc_receipts.py.
        from workers import process_cc_receipts as _worker_mod
        worker = _worker_mod.__file__
        subprocess.Popen(
            [sys.executable, worker],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, cwd=os.path.dirname(worker))
    except Exception:
        pass  # scheduled task will pick the receipts up regardless

# Uploaded receipts are opaque blobs keyed by a relative path stored on the DB
# row (cc_receipts.file_path). The bytes live wherever `storage` is pointed —
# local disk by default, an object store in a hosted deployment. See storage.py.
_RECEIPT_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.heic', '.heif', '.pdf'}
_MAX_RECEIPT_BYTES = 15 * 1024 * 1024  # 15 MB per file

# Content-type is ALWAYS derived from the (validated) extension here — never
# trusted from the client upload — so a receipt can't be stored/served as
# text/html and run inline script in our origin (stored XSS). Only these types
# are ever emitted.
_SAFE_TYPES = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.webp': 'image/webp',
    '.heic': 'image/heic', '.heif': 'image/heif', '.pdf': 'application/pdf',
}
# Types a browser renders safely inline. HEIC/HEIF can't render in a browser, so
# they (and anything unexpected) are sent as a download, never inline.
_INLINE_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp',
                 'application/pdf'}


def _content_matches_ext(ext, data):
    """Cheap magic-byte check that the file's leading bytes match its claimed
    extension. Blocks e.g. an HTML payload named .png.

    Stays a byte comparison rather than a Pillow ``Image.open`` even though Pillow
    is now a dependency (see ``northwind/services/images.py``): this runs on EVERY upload
    including PDFs, and it must be able to reject a hostile file without handing it
    to a decoder first.
    """
    if ext in ('.jpg', '.jpeg'):
        return data[:3] == b'\xff\xd8\xff'
    if ext == '.png':
        return data[:8] == b'\x89PNG\r\n\x1a\n'
    if ext == '.gif':
        return data[:6] in (b'GIF87a', b'GIF89a')
    if ext == '.webp':
        return data[:4] == b'RIFF' and data[8:12] == b'WEBP'
    if ext == '.pdf':
        return data[:5] == b'%PDF-'
    if ext in ('.heic', '.heif'):
        return data[4:8] == b'ftyp'   # ISO-BMFF box; HEIC/HEIF share this
    return False


def _prepare_receipt(data, filename, ext):
    """Last step before a receipt is stored: convert what browsers can't render.

    Returns ``(data, filename, ext)``, converting HEIC/HEIF to JPEG and renaming to
    match. Everything else passes through byte-identical.

    Placed here, at the single point every upload route funnels through, because the
    whole benefit of converting is that no consumer downstream has to know: the
    serve route's inline/attachment decision, both templates' image-extension
    allowlists and the extraction mime type all read
    the stored extension or a type derived from it.

    **Call this AFTER the content hash and the dedup lookup.** The hash must be over
    the bytes the user actually uploaded, so ``content_hash`` keeps meaning "the same
    photo" — a Pillow or libheif version bump changes JPEG output byte-for-byte and
    would otherwise silently stop deduplicating re-uploads. Converting afterwards
    also means a duplicate costs no CPU.

    On failure the original is returned unchanged, so the receipt is still stored and
    still downloadable — exactly the behaviour before this existed.
    """
    if not images.is_heif(ext):
        return data, filename, ext
    converted = images.transcode_to_jpeg(data)
    if converted is None:
        return data, filename, ext
    return converted, images.swap_ext(filename, '.jpg'), '.jpg'


@app.route('/cards')
def cc_home():
    all_cards = db.list_cc_cards(active_only=False)
    return render_template('cc_cards.html',
                           cards=[c for c in all_cards if c['active']],
                           archived_cards=[c for c in all_cards if not c['active']])


@app.route('/cards/upload', methods=['GET', 'POST'])
def cc_upload():
    if request.method == 'GET':
        return render_template('cc_upload.html', results=None, errors=None)

    files = [f for f in request.files.getlist('reports') if f and f.filename]
    if not files:
        flash('Choose at least one .xlsx report to upload.', 'danger')
        return redirect(url_for('cc_upload'))

    results, errors = [], []
    for f in files:
        name = f.filename
        if not name.lower().endswith(_ALLOWED_EXT):
            errors.append((name, 'Not an .xlsx file — skipped.'))
            continue
        try:
            snap = parse_workbook(io.BytesIO(f.read()), source_filename=name)
            if not snap.lines:
                errors.append((name, 'No statement lines found — is this a Xero recon export?'))
                continue
            # Spends are negative; if a file parses to lines but NONE are spends,
            # the amount column is likely mis-signed/oriented — importing it would
            # show the card as owing zero receipts. Warn rather than silently hide.
            if not any(ln.category == 'spend' for ln in snap.lines):
                errors.append((name, 'Parsed OK but found no chargeable (spend) lines '
                                     '— check the amount column sign/orientation. Not imported.'))
                continue
            results.append(db.import_card_snapshot(snap))
        except Exception as e:  # malformed/locked file — report, don't abort the batch
            errors.append((name, f'Could not read: {e}'))

    if results:
        created = sum(1 for r in results if r['created'])
        flash(
            f"Imported {len(results)} report(s) — {created} new card(s), "
            f"{sum(r['lines_new'] for r in results)} new line(s), "
            f"{sum(r['lines_updated'] for r in results)} updated.", 'success')
    for n, msg in errors:
        flash(f'{n}: {msg}', 'danger')

    return render_template('cc_upload.html', results=results, errors=errors)


@app.route('/cards/<int:card_id>')
def cc_card(card_id):
    card = db.get_cc_card(card_id)
    if not card:
        abort(404)
    statements = db.list_cc_statements(card_id)
    # Per-month workspace: review one statement at a time (defaults to newest) so
    # June/July lines aren't interleaved. The month switcher posts ?statement_id.
    selected_id = request.args.get('statement_id', type=int)
    if not any(s['id'] == selected_id for s in statements):
        selected_id = statements[0]['id'] if statements else None

    # Date filter: given a from/to window, list EVERY still-outstanding
    # transaction across ALL months in that range (a find/triage view), instead
    # of the single selected month.
    date_from = (request.args.get('from') or '').strip() or None
    date_to = (request.args.get('to') or '').strip() or None
    date_filter = bool(date_from or date_to)

    line_receipts = {}
    line_suggestions = {}   # line_id -> [AI suggestion rows awaiting confirmation]
    receipt_count = 0
    bucket = []             # receipts uploaded this month but not linked to any line
    if date_filter:
        lines = db.get_cc_card_lines(card_id, only_outstanding=True,
                                     date_from=date_from, date_to=date_to)
        # A filtered line's receipt can live in any month's statement, so map
        # receipts across every statement (scoped to the lines we're showing).
        want = {l['id'] for l in lines}
        for s in statements:
            receipts = {r['id']: r for r in db.list_cc_receipts(s['id'])}
            for lk in db.list_cc_receipt_links(s['id']):
                if lk['line_id'] in want and lk['receipt_id'] in receipts:
                    line_receipts.setdefault(lk['line_id'], []).append(receipts[lk['receipt_id']])
    else:
        lines = db.get_cc_card_lines(card_id, statement_id=selected_id) if selected_id else []
        if selected_id:
            receipts = {r['id']: r for r in db.list_cc_receipts(selected_id)}
            receipt_count = len(receipts)
            linked_ids = set()
            for lk in db.list_cc_receipt_links(selected_id):
                if lk['receipt_id'] in receipts:
                    line_receipts.setdefault(lk['line_id'], []).append(receipts[lk['receipt_id']])
                    linked_ids.add(lk['receipt_id'])
            bucket = [r for r in receipts.values() if r['id'] not in linked_ids]
            for sg in db.list_cc_suggestions_for_statement(selected_id):
                line_suggestions.setdefault(sg['line_id'], []).append(sg)
    # "Reconciled in Xero" is an admin housekeeping tick: hide those lines from
    # the working view by default so only what's left to do shows. ?show_reconciled=1
    # reveals them again. reconciled_count feeds the "N hidden · show" toggle.
    show_reconciled = bool(request.args.get('show_reconciled'))
    reconciled_count = sum(1 for l in lines if l['xero_reconciled'])
    if not show_reconciled:
        lines = [l for l in lines if not l['xero_reconciled']]

    # Personal charges the cardholder owes back (this month).
    personal = [l for l in lines if l['category'] == 'spend' and l['personal']]
    personal_cents = sum(abs(l['amount_cents']) for l in personal)
    # Count of receipts auto-matched by the AI this month (a 'confirmed' suggestion
    # that the worker created), for the "N auto-matched, M need review" banner.
    auto_matched = sum(1 for sgs in line_receipts.values() for _ in sgs)  # linked count
    needs_review = sum(len(v) for v in line_suggestions.values())

    # Completeness banner: of the outstanding spend lines shown, how many are
    # coded (have a Xero account) and how many have a receipt (or are personal).
    # A month is "ready to reconcile" when both hit 100%.
    working = [l for l in lines if l['category'] == 'spend' and l['status'] == 'outstanding']
    total_spend = len(working)
    coded = sum(1 for l in working if l['xero_account_code'])
    receipted = sum(1 for l in working if l['personal'] or l['id'] in line_receipts)
    xero_ready_ids = db.get_cc_xero_ready_line_ids([line['id'] for line in working])

    return render_template(
        'cc_card.html', card=card, lines=lines, covered=set(line_receipts),
        line_receipts=line_receipts, receipt_count=receipt_count,
        statements=statements, selected_id=selected_id,
        personal_cents=personal_cents, bucket=bucket,
        personal_count=len(personal), users=db.list_cc_card_users(card_id),
        line_suggestions=line_suggestions, ai_enabled=cc_ai.FEATURE_ENABLED,
        auto_matched=auto_matched, needs_review=needs_review,
        date_from=date_from, date_to=date_to, date_filter=date_filter,
        accounts=cc_accounts.choices(),
        show_reconciled=show_reconciled, reconciled_count=reconciled_count,
        total_spend=total_spend, coded_count=coded, receipted_count=receipted,
        xero_ready_ids=xero_ready_ids,
        inbox_count=db.count_cc_inbox_receipts(card_id),
        ai_status=db.get_cc_ai_status(card_id, selected_id),
        # Across every month, not just the selected one: the reminder email
        # chases the whole card, so the button must promise the same number.
        chase_count=len(db.list_cc_reminder_lines(card_id)),
        mail_ready=mailer.is_configured(),
        # A freshly-generated password to show ONCE in a persistent, copyable
        # panel (held server-side, popped so a refresh won't re-show it — the
        # plaintext never rides in the client session cookie).
        new_credential=_pop_credential())


@app.route('/cards/<int:card_id>/suggestions/<int:suggestion_id>/confirm', methods=['POST'])
def cc_confirm_suggestion(card_id, suggestion_id):
    """Admin: promote an AI match suggestion into a real receipt<->line link and
    rename the receipt to that transaction so downloads are self-labelling."""
    suggestion = db.get_cc_suggestion(suggestion_id)
    if not suggestion or suggestion['card_id'] != card_id:
        abort(404)
    result = db.confirm_cc_suggestion(suggestion_id, actor=_link_actor())
    if not result:
        flash('That suggestion is no longer available.', 'warning')
        return redirect(url_for('cc_card', card_id=card_id))
    receipt_id, line_id = result
    line = db.get_cc_line(line_id)
    receipt = db.get_cc_receipt(receipt_id)
    if line is not None and receipt is not None:
        ext = os.path.splitext(receipt['file_path'])[1] or '.pdf'
        extra = max(0, db.count_cc_receipt_links(receipt_id) - 1)
        db.set_cc_receipt_download_name(
            receipt_id,
            cc_ai.download_name_for(line['reference'], line['line_date'],
                                    line['amount_cents'], ext, extra_count=extra))
    flash('Receipt linked to the transaction.', 'success')
    return redirect(url_for('cc_card', card_id=card_id))


@app.route('/cards/<int:card_id>/suggestions/<int:suggestion_id>/dismiss', methods=['POST'])
def cc_reject_suggestion(card_id, suggestion_id):
    """Admin: dismiss a wrong AI suggestion so it stops offering a bad 'confirm'.
    The receipt stays in the bucket to be linked elsewhere."""
    suggestion = db.get_cc_suggestion(suggestion_id)
    if not suggestion or suggestion['card_id'] != card_id:
        abort(404)
    db.reject_cc_suggestion(suggestion_id)
    flash('Suggestion dismissed.', 'info')
    return redirect(url_for('cc_card', card_id=card_id))


@app.route('/cards/<int:card_id>/match-now', methods=['POST'])
def cc_match_now(card_id):
    """Admin: run the AI extractor + matcher right now, in-process, for this
    card's pending receipts (optionally scoped to one month). Works on any host
    — it calls the worker directly rather than spawning it, so it doesn't depend
    on the scheduled task or process-spawn permissions."""
    card = db.get_cc_card(card_id)
    if not card:
        abort(404)
    statement_id = request.form.get('statement_id', type=int)
    if statement_id:
        st = db.get_cc_statement(statement_id)
        if not st or st['card_id'] != card_id:
            statement_id = None
    if not cc_ai.FEATURE_ENABLED:
        flash('AI matching is switched off (set NW_CC_AI=1).',
              'warning')
        return redirect(url_for('cc_card', card_id=card_id, statement_id=statement_id))
    import process_cc_receipts as worker
    try:
        # code_accounts_ai=False: account-coding stays memory-only in the request
        # (fast); the AI account pass runs out-of-band on the scheduled worker.
        # force=True: a person asked for this, so retry even the receipts the cron
        # holds back (inside the retry backoff, or failed with a terminal error).
        # Reporting "nothing waiting" while the cardholder can see their receipt
        # sitting unmatched is the worst possible answer here.
        if statement_id:
            n = worker.run(statement_id=statement_id, code_accounts_ai=False,
                           force=True)
        else:
            # No month picked → work every statement on this card.
            n = sum(worker.run(statement_id=st['id'], code_accounts_ai=False,
                               force=True)
                    for st in db.list_cc_statements(card_id))
    except Exception:
        flash('AI matching hit an error — check the logs.', 'danger')
        return redirect(url_for('cc_card', card_id=card_id, statement_id=statement_id))
    if n:
        flash(f'AI checked {n} receipt{"" if n == 1 else "s"} — auto-matched the '
              f'clear ones; anything uncertain is flagged below to confirm.', 'success')
    else:
        flash('No receipts were waiting to be matched.', 'info')
    return redirect(url_for('cc_card', card_id=card_id, statement_id=statement_id))


@app.route('/cards/<int:card_id>/ai/retry', methods=['POST'])
def cc_retry_ai(card_id):
    if not db.get_cc_card(card_id):
        abort(404)
    statement_id = request.form.get('statement_id', type=int)
    if statement_id:
        st = db.get_cc_statement(statement_id)
        if not st or st['card_id'] != card_id:
            abort(404)
    count = db.retry_cc_receipt_ai(card_id, statement_id)
    if count:
        _kick_ai_worker()
        noun = 'receipt' if count == 1 else 'receipts'
        flash(f'Retrying AI matching for {count} {noun}.', 'info')
    else:
        flash('No failed AI jobs were waiting to retry.', 'info')
    return redirect(url_for('cc_card', card_id=card_id, statement_id=statement_id))


@app.route('/cards/<int:card_id>/rename', methods=['POST'])
def cc_card_rename(card_id):
    card = db.get_cc_card(card_id)
    if not card:
        abort(404)
    name = (request.form.get('display_name') or '').strip()
    if len(name) > 100:
        flash('Card display name must be 100 characters or fewer.', 'danger')
    else:
        db.set_cc_card_display_name(card_id, name)
        flash('Card display name updated.', 'success')
    return redirect(url_for('cc_card', card_id=card_id,
                            statement_id=request.form.get('statement_id', type=int)))


@app.route('/cards/<int:card_id>/active', methods=['POST'])
def cc_card_set_active(card_id):
    card = db.get_cc_card(card_id)
    if not card:
        abort(404)
    active = request.form.get('active') == '1'
    db.set_cc_card_active(card_id, active)
    flash('Card restored.' if active else 'Card archived. Its history and receipts are preserved.',
          'success' if active else 'info')
    return redirect(url_for('cc_card', card_id=card_id) if active else url_for('cc_home'))


@app.route('/cards/<int:card_id>/statements/<int:statement_id>/reopen', methods=['POST'])
def cc_card_reopen(card_id, statement_id):
    """Admin: clear a month's soft sent-to-finance marker."""
    st = db.get_cc_statement(statement_id)
    if not st or st['card_id'] != card_id:
        abort(404)
    db.reopen_cc_statement(statement_id)
    flash('Sent-to-finance marker cleared for this month.', 'info')
    return redirect(url_for('cc_card', card_id=card_id))


@app.route('/cards/<int:card_id>/users/add', methods=['POST'])
def cc_card_add_user(card_id):
    if not db.get_cc_card(card_id):
        abort(404)
    email = (request.form.get('email') or '').strip().lower()
    if '@' not in email or '.' not in email:
        flash('Enter a valid email address.', 'danger')
        return redirect(url_for('cc_card', card_id=card_id))
    existing_user = db.get_cc_user(email)
    typed = (request.form.get('password') or '').strip()
    if not existing_user and typed and len(typed) < security.MIN_PASSWORD_LENGTH:
        flash(f'Password must be at least {security.MIN_PASSWORD_LENGTH} characters.', 'danger')
        return redirect(url_for('cc_card', card_id=card_id))
    try:
        db.add_cc_card_user(card_id, email, request.form.get('name'),
                            request.form.get('access_note'))
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('cc_card', card_id=card_id))

    # Give them a login if they don't already have one (a person may hold
    # access to several cards but has a single password).
    if existing_user:
        flash(f'{email} already has a login — access to this card added. '
              f'Use "reset password" if they need a new one.', 'success')
    else:
        password = typed or _generate_password()
        db.set_cc_user_password(email, generate_password_hash(password, method='pbkdf2:sha256'))
        # Show the password in a persistent, copyable panel (see cc_card.html)
        # rather than a toast that auto-hides after a few seconds. Held
        # server-side (not in the cookie) and shown once.
        _stash_credential(email, password)
        flash(f'Access granted for {email}. Copy their password below — it is not stored and won\'t be shown again.', 'success')
    return redirect(url_for('cc_card', card_id=card_id))


@app.route('/cards/<int:card_id>/users/reset', methods=['POST'])
def cc_card_reset_password(card_id):
    if not db.get_cc_card(card_id):
        abort(404)
    email = (request.form.get('email') or '').strip().lower()
    if '@' not in email:
        flash('Could not reset — missing email.', 'danger')
        return redirect(url_for('cc_card', card_id=card_id))
    if not db.cc_card_user_has_access(card_id, email):
        flash('Could not reset — that person does not have access to this card.', 'danger')
        return redirect(url_for('cc_card', card_id=card_id))
    typed = (request.form.get('password') or '').strip()
    if typed and len(typed) < security.MIN_PASSWORD_LENGTH:
        flash(f'Password must be at least {security.MIN_PASSWORD_LENGTH} characters.', 'danger')
        return redirect(url_for('cc_card', card_id=card_id))
    user = db.get_cc_user(email)
    if not user:
        flash('Could not reset — that person does not have a login yet.', 'danger')
        return redirect(url_for('cc_card', card_id=card_id))
    if not user['is_active'] and not request.form.get('enable_account'):
        flash('Password not reset — this login is disabled. Use “Enable & reset” if access should be restored.', 'warning')
        return redirect(url_for('cc_card', card_id=card_id))
    password = typed or _generate_password()
    db.set_cc_user_password(email, generate_password_hash(password, method='pbkdf2:sha256'))
    if not user['is_active']:
        db.set_user_active(user['id'], True)
    # Show the password in a persistent, copyable panel (see cc_card.html)
    # rather than a toast that auto-hides after a few seconds. Held server-side
    # (not in the cookie) and shown once.
    _stash_credential(email, password)
    flash(f'Password reset for {email}. Copy the new one below — it is not stored and won\'t be shown again.', 'success')
    return redirect(url_for('cc_card', card_id=card_id))


@app.route('/cards/<int:card_id>/delete', methods=['POST'])
def cc_card_delete(card_id):
    """Admin: hard-delete a card and all of its data (statements, transactions,
    receipts + files, suggestions, access grants)."""
    if not db.get_cc_card(card_id):
        abort(404)
    files = db.delete_cc_card(card_id)
    for rel in files:
        try:
            storage.delete(rel)
        except Exception:
            pass  # DB row is already gone; a leftover file is harmless
    flash('Card and all of its data have been deleted.', 'info')
    return redirect(url_for('cc_home'))


@app.route('/cards/<int:card_id>/remind', methods=['POST'])
def cc_card_remind(card_id):
    """Admin: email this card's people the transactions still owed, with a link.

    Only ever on a button press — nothing here is scheduled. An optional `email`
    field chases one person instead of everyone on the card.
    """
    if not db.get_cc_card(card_id):
        abort(404)
    if not mailer.is_configured():
        flash('Email is not set up yet — see Settings → Email. Missing: %s'
              % ', '.join(mailer.missing_config()), 'danger')
        return redirect(url_for('cc_card', card_id=card_id))

    report = reminders.send_for_card(card_id, only_email=request.form.get('email'))

    if report['sent']:
        flash('Reminder sent to %s — %d transaction(s) listed.%s'
              % (', '.join(report['sent']), report['outstanding'],
                 ' (Dry run — nothing actually left the building.)'
                 if mailer.DRY_RUN else ''), 'success')
    if report['skipped']:
        flash('Nothing outstanding on this card, so no reminder was sent.', 'info')
    if not report['sent'] and not report['skipped'] and not report['failed']:
        flash('Nobody has access to this card yet — grant access first.', 'warning')
    for address, error in report['failed']:
        flash('Could not send to %s: %s' % (address, error), 'danger')
    return redirect(url_for('cc_card', card_id=card_id))


@app.route('/cards/users/<int:user_id>/delete', methods=['POST'])
def cc_card_delete_user(user_id):
    card_id = request.form.get('card_id', type=int)
    db.delete_cc_card_user(user_id)
    flash('Access removed.', 'info')
    return redirect(url_for('cc_card', card_id=card_id) if card_id else url_for('cc_home'))


@app.route('/cards/<int:card_id>/receipts.zip')
def cc_card_receipts_zip(card_id):
    """Admin: download all of a card's receipts as a zip, foldered by month."""
    card = db.get_cc_card(card_id)
    if not card:
        abort(404)
    # Optionally scope to one month (the per-month workspace's download button);
    # omit statement_id to zip the whole card.
    only_sid = request.args.get('statement_id', type=int)
    statements = [s for s in db.list_cc_statements(card_id)
                  if only_sid is None or s['id'] == only_sid]
    mem = io.BytesIO()
    n = 0
    with zipfile.ZipFile(mem, 'w', zipfile.ZIP_DEFLATED) as z:
        for st in statements:
            folder = f"{st['year']:04d}-{st['month']:02d}"
            for r in db.list_cc_receipts(st['id']):
                try:
                    data = storage.read(r['file_path'])
                except FileNotFoundError:
                    continue
                # Prefer the transaction-derived name so each file in the zip
                # tells you which transaction it belongs to.
                base = secure_filename(os.path.basename(
                    r['download_name'] or r['original_filename'] or r['file_path'])) or 'receipt'
                z.writestr(f"{folder}/{r['id']}_{base}", data)
                n += 1
        # A whole-card download must include receipts dropped off before their
        # statement arrived. A month-scoped download intentionally excludes the
        # inbox because those files do not belong to that statement yet.
        if only_sid is None:
            for r in db.list_cc_inbox_receipts(card_id):
                try:
                    data = storage.read(r['file_path'])
                except FileNotFoundError:
                    continue
                base = secure_filename(os.path.basename(
                    r['download_name'] or r['original_filename'] or r['file_path'])) or 'receipt'
                z.writestr(f"inbox/{r['id']}_{base}", data)
                n += 1
    if n == 0:
        flash('No receipts uploaded for this month yet.', 'info')
        return redirect(url_for('cc_card', card_id=card_id, statement_id=only_sid))
    mem.seek(0)
    safe = secure_filename(card['display_name'] or card['card_name']) or 'card'
    return send_file(mem, mimetype='application/zip', as_attachment=True,
                     download_name=f'{safe}_receipts.zip')


@app.route('/cards/<int:card_id>/lines/<int:line_id>/require', methods=['POST'])
def cc_card_toggle_require(card_id, line_id):
    """Admin: flag/unflag a transaction as needing its own receipt attached."""
    line = db.get_cc_line(line_id)
    if not line or line['card_id'] != card_id:
        abort(404)
    db.set_cc_line_require(line_id, not line['require_individual'])
    return redirect(url_for('cc_card', card_id=card_id))


@app.route('/cards/<int:card_id>/lines/<int:line_id>/vat-invoice',
           methods=['POST'])
def cc_card_toggle_vat_invoice(card_id, line_id):
    """Admin: request or clear a compliant supplier VAT tax invoice."""
    line = db.get_cc_line(line_id)
    if not line or line['card_id'] != card_id:
        abort(404)
    statement_id = request.form.get('statement_id', type=int)
    if statement_id and line['statement_id'] != statement_id:
        abort(404)
    required = not bool(line['vat_invoice_required'])
    db.set_cc_line_vat_invoice_required(
        line_id, required, session.get('admin_username') or 'admin')
    flash(
        'VAT tax invoice requested from the cardholder.'
        if required else 'VAT tax invoice request cleared.',
        'warning' if required else 'success',
    )
    params = {'card_id': card_id, 'statement_id': line['statement_id']}
    if request.form.get('show_reconciled') == '1':
        params['show_reconciled'] = 1
    return redirect(url_for('cc_card', **params))


@app.route('/cards/<int:card_id>/lines/<int:line_id>/reconciled', methods=['POST'])
def cc_card_toggle_reconciled(card_id, line_id):
    """Admin: tick/untick 'reconciled in Xero' — the transaction's "done" marker.

    A ticked line drops out of the admin working view (unless
    ?show_reconciled=1) AND out of the cardholder's checklist, so ticking it
    closes the transaction on both sides. Receipt data itself is untouched.

    A transaction that already meets the finance-ready rule ticks straight
    through. One that doesn't needs an explicit ``override=1`` — the UI asks for
    confirmation and then sends it — so a stale page or hand-made POST can't
    silently close off a transaction that is still missing its evidence. The
    override is recorded against the line.
    """
    line = db.get_cc_line(line_id)
    if not line or line['card_id'] != card_id:
        abort(404)
    sid = request.form.get('statement_id', type=int)
    if sid and line['statement_id'] != sid:
        abort(404)
    new_state = not line['xero_reconciled']
    forced = False
    if new_state and line_id not in db.get_cc_xero_ready_line_ids([line_id]):
        if request.form.get('override') != '1':
            message = ('Finish the receipt or personal choice, reason, location, '
                       'submission, and any AI receipt review first.')
            if _wants_json():
                return jsonify({'ok': False, 'error': message}), 400
            flash(message, 'warning')
            return redirect(url_for('cc_card', card_id=card_id, statement_id=sid))
        # Override still only applies to an open card charge — a transfer or a
        # bank fee has no cardholder obligation to close off.
        if line['category'] != 'spend' or line['status'] != 'outstanding':
            abort(404)
        forced = True
    db.set_cc_line_xero_reconciled(
        line_id, new_state, actor=session.get('admin_username') or 'admin',
        override=forced)
    show_reconciled = bool(request.form.get('show_reconciled'))
    if _wants_json():
        return jsonify({'ok': True, 'reconciled': new_state, 'forced': forced,
                        'progress': _statement_progress(card_id, sid, show_reconciled)})
    return redirect(url_for('cc_card', card_id=card_id, statement_id=sid,
                            show_reconciled=request.form.get('show_reconciled') or None))


@app.route('/cards/<int:card_id>/lines/bulk', methods=['POST'])
def cc_card_bulk_lines(card_id):
    if not db.get_cc_card(card_id):
        abort(404)
    statement_id = request.form.get('statement_id', type=int)
    st = db.get_cc_statement(statement_id)
    if not st or st['card_id'] != card_id:
        abort(404)
    line_ids = request.form.getlist('line_id', type=int)
    action = request.form.get('action')
    actor = session.get('admin_username') or 'admin'
    if action == 'reconcile':
        # override=1 (the UI sends it after confirming) closes off every selected
        # transaction, incomplete ones included. Without it only finance-ready
        # rows move, and the flash says how many were left behind.
        if request.form.get('override') == '1':
            count, forced = db.force_reconcile_cc_lines(
                card_id, statement_id, line_ids, actor=actor)
        else:
            count = db.bulk_reconcile_cc_lines(
                card_id, statement_id, line_ids, actor=actor)
            forced = 0
        noun = 'transaction' if count == 1 else 'transactions'
        flash(f'Marked {count} {noun} reconciled in Xero — hidden from your '
              'view and the cardholder\'s.', 'success' if count else 'warning')
        if forced:
            noun = 'transaction was' if forced == 1 else 'transactions were'
            flash(f'{forced} {noun} still missing a receipt, reason, location '
                  'or submission and has been closed off anyway.', 'warning')
        skipped = len(set(line_ids)) - count
        if skipped > 0 and not forced:
            noun = 'transaction was' if skipped == 1 else 'transactions were'
            flash(f'{skipped} selected {noun} skipped — not finance-ready yet.',
                  'warning')
    elif action == 'accept_ai':
        count = db.bulk_accept_cc_ai_accounts(card_id, statement_id, line_ids)
        noun = 'account suggestion' if count == 1 else 'account suggestions'
        flash(f'Accepted {count} reviewed high-confidence {noun}.',
              'success' if count else 'warning')
    else:
        abort(400)
    return redirect(url_for('cc_card', card_id=card_id, statement_id=statement_id))


@app.route('/cards/<int:card_id>/lines/<int:line_id>/account', methods=['POST'])
def cc_card_set_account(card_id, line_id):
    """Admin: set/confirm the Xero expense account for a line (the coding step).

    The code is validated against cc_accounts (never trust the posted name — we
    look it up). On confirm we also TEACH the merchant memory so next month the
    same merchant auto-codes; and if 'apply_all' is set, code every other
    outstanding line from the same merchant this month in one go."""
    line = db.get_cc_line(line_id)
    if not line or line['card_id'] != card_id:
        abort(404)
    sid = request.form.get('statement_id', type=int)
    code = (request.form.get('account_code') or '').strip()
    as_json = _wants_json()

    def _progress_payload(**extra):
        p = _statement_progress(card_id, sid,
                                show_reconciled=bool(request.form.get('show_reconciled')))
        return jsonify({'ok': True, 'progress': p, **extra})

    # Empty selection clears the coding (and skips learning) — an explicit reset.
    if not code:
        db.set_cc_line_xero_account(line_id, None, None)
        if as_json:
            return _progress_payload(coded=False, code='', name='')
        return redirect(url_for('cc_card', card_id=card_id, statement_id=sid))

    name = cc_accounts.name_for(code)
    if not name:
        if as_json:
            return jsonify({'ok': False, 'error': 'That account code is not on the list.'}), 400
        flash('That account code is not on the list.', 'danger')
        return redirect(url_for('cc_card', card_id=card_id, statement_id=sid))

    db.set_cc_line_xero_account(line_id, code, name)

    # Teach the merchant memory (global, so every card benefits next month).
    key = cc_ai.normalize_merchant(line['reference'])
    if len(key) >= 3:
        db.upsert_cc_merchant_map(key, code, name)

    applied = 1
    if request.form.get('apply_all') and len(key) >= 3:
        n = db.apply_cc_account_to_statement_merchant(
            line['statement_id'], key, code, name, cc_ai.normalize_merchant)
        if n > 1:
            applied = n
            flash(f'Coded {n} “{key}” transactions to {code} · {name}.', 'success')
    if as_json:
        return _progress_payload(coded=True, code=code, name=name, applied=applied)
    return redirect(url_for('cc_card', card_id=card_id, statement_id=sid))


@app.route('/cards/<int:card_id>/unlink', methods=['POST'])
def cc_card_unlink(card_id):
    """Admin: detach a receipt from one transaction. The receipt keeps any other
    links; with none left it falls back into the month's unmatched bucket. Admins
    are the authority here — unlike the cardholder flow this ignores the month
    lock (an admin can still correct a submitted month, as with reopen)."""
    if not db.get_cc_card(card_id):
        abort(404)
    receipt_id = request.form.get('receipt_id', type=int)
    line_id = request.form.get('line_id', type=int)
    statement_id = request.form.get('statement_id', type=int)
    r = db.get_cc_receipt(receipt_id)
    if r and r['card_id'] == card_id and line_id:
        db.unlink_cc_receipt(receipt_id, line_id)
        flash('Receipt unmatched from that transaction.', 'info')
    return redirect(url_for('cc_card', card_id=card_id, statement_id=statement_id))


@app.route('/cards/<int:card_id>/link', methods=['POST'])
def cc_card_link(card_id):
    """Admin: manually match a bucket receipt to one or more transactions (the
    without-AI equivalent of confirming a suggestion). Renames the file to the
    first matched transaction so downloads stay self-labelling."""
    if not db.get_cc_card(card_id):
        abort(404)
    receipt_id = request.form.get('receipt_id', type=int)
    statement_id = request.form.get('statement_id', type=int)
    line_ids = request.form.getlist('line_id', type=int)
    r = db.get_cc_receipt(receipt_id)
    if not r or r['card_id'] != card_id:
        flash('Could not match that receipt.', 'danger')
        return redirect(url_for('cc_card', card_id=card_id, statement_id=statement_id))
    linked = []
    for line_id in line_ids:
        line = db.get_cc_line(line_id)
        # Only lines on this card and in the receipt's OWN month are valid targets.
        if line and line['card_id'] == card_id and line['statement_id'] == r['statement_id']:
            db.link_cc_receipt(receipt_id, line_id, actor=_link_actor())
            linked.append(line)
    if linked:
        first = linked[0]
        ext = os.path.splitext(r['file_path'])[1] or '.pdf'
        extra = max(0, db.count_cc_receipt_links(receipt_id) - 1)
        db.set_cc_receipt_download_name(
            receipt_id,
            cc_ai.download_name_for(first['reference'], first['line_date'],
                                    first['amount_cents'], ext, extra_count=extra))
        flash(f'Matched to {len(linked)} transaction{"" if len(linked) == 1 else "s"}.', 'success')
    else:
        flash('Pick at least one transaction to match to.', 'warning')
    return redirect(url_for('cc_card', card_id=card_id, statement_id=statement_id))


@app.route('/cards/<int:card_id>/receipts/<int:receipt_id>/delete', methods=['POST'])
def cc_card_delete_receipt(card_id, receipt_id):
    """Admin: permanently remove a receipt (junk/duplicate upload) — its file, DB
    row, links and AI suggestions all go. Unlike the cardholder delete this is not
    gated by the month lock."""
    r = db.get_cc_receipt(receipt_id)
    if not r or r['card_id'] != card_id:
        abort(404)
    statement_id = r['statement_id']
    file_path = db.delete_cc_receipt(receipt_id)
    if file_path:
        try:
            storage.delete(file_path)
        except Exception:
            pass  # DB truth is clean; an orphaned blob can be removed later
    flash('Receipt deleted.', 'info')
    return redirect(url_for('cc_card', card_id=card_id, statement_id=statement_id))


# ── Cardholder portal ────────────────────────────────────────────────────────
# A logged-in card user (session['cc_user'] = their email) is taken straight to
# their card, sees the transactions they owe receipts for, and drops all their
# receipts/invoices into that month's bucket. Access is scoped per request to
# cards their email may access.

def _cc_user():
    return session.get('cc_user')


def _link_actor():
    """Who to stamp on a receipt<->charge link (migration 0043).

    An admin username where there is one, else the portal cardholder identity, else a
    generic 'admin'. Matters because the MCP connector can now create and remove these
    links as 'mcp:claude' — an unstamped browser write would leave the two
    indistinguishable, which is the question the column exists to answer.
    """
    return session.get('admin_username') or _cc_user() or 'admin'


def _require_card_access(card_id):
    """Return the card row if the current cardholder may access it, else 403.

    A super admin may inspect any card. Scoped retail/HQ admins who also have a
    portal identity still fall through to the explicit email grant check."""
    if session.get('admin') and session.get('admin_role') == 'super':
        card = db.get_cc_card(card_id)
        if not card:
            abort(404)
        return card
    card = db.get_cc_card_for_user(card_id, _cc_user())
    if not card:
        abort(403)
    return card


def _locked(statement):
    """Cardholder edit-lock — DISABLED by request (2026-07-17).

    Submitting a month still records `submitted_at` (a "sent to finance" marker),
    but it no longer freezes the month: cardholders can keep uploading receipts
    and editing reasons/links/personal/location afterwards. Kept as a function so
    every caller/template stays wired — flip the body back to
    `bool(statement and statement['submitted_at']) and not session.get('admin')`
    to restore the lock."""
    return False


def _portal_return(card_id, statement_id=None, *, focus_id=None, anchor_line_id=None):
    """Return to the portal without losing the cardholder's place.

    `focus` is no longer just a scroll target — it selects the one-at-a-time
    view. So a mutation must never invent one: doing that switched a list-view
    cardholder into single-transaction view every time they marked something
    personal. The forms carry `focus` themselves when (and only when) that view
    is active, which is what keeps it. In list view the edited line comes back
    as a plain fragment anchor instead, which restores the scroll position
    without touching the view mode.

    The focus id is always re-scoped before it is reflected into a URL. This
    keeps the progressive no-JS form path as safe as the page's GET handler.
    """
    if focus_id is None:
        focus_id = (request.form.get('focus', type=int)
                    or request.args.get('focus', type=int))
    if focus_id:
        line = db.get_cc_line(focus_id)
        if (not line or line['card_id'] != card_id
                or (statement_id and line['statement_id'] != statement_id)):
            focus_id = None
    kwargs = {}
    if statement_id:
        kwargs['statement_id'] = statement_id
    if focus_id:
        kwargs['focus'] = focus_id
    url = url_for('cc_portal_card', card_id=card_id, **kwargs)
    if not focus_id and anchor_line_id:
        url += '#txn-%d' % int(anchor_line_id)
    return redirect(url)


def _portal_spend_line(card_id, statement_id, line_id):
    """Return one editable portal line only when every posted scope agrees.

    A transaction the admin has marked reconciled in Xero is done: it is gone
    from the cardholder's checklist, so every cardholder mutation that routes
    through here refuses it rather than editing a closed transaction.
    """
    line = db.get_cc_line(line_id)
    if (not line or line['card_id'] != card_id
            or line['statement_id'] != statement_id
            or line['category'] != 'spend'
            or line['status'] != 'outstanding'
            or line['xero_reconciled']):
        return None
    return line


# How far back the merchant memory reads. Deep enough to cover years of one
# card's statements, but bounded so a portal page load never has to walk the
# card's entire lifetime — that cost only ever grows.
_MERCHANT_HISTORY_LIMIT = 4000


def _merchant_field_suggestions(card_id, lines):
    """Suggest a prior location for each line's normalised merchant.

    Reasons are deliberately excluded: the cardholder's own typed description
    is authoritative and must never be replaced by merchant history. Location
    hints remain read-only and require an explicit click beside an empty field.

    Only history for the merchants actually on screen is indexed, so the work
    tracks the visible page rather than the whole card history.
    """
    normalised = {}

    def key_for(reference):
        """normalize_merchant, memoised — the same reference repeats a lot."""
        if reference not in normalised:
            normalised[reference] = cc_ai.normalize_merchant(reference)
        return normalised[reference]

    line_keys = {line['id']: key_for(line['reference']) for line in lines}
    wanted = {k for k in line_keys.values() if k}
    if not wanted:
        return {line['id']: {'reason': None, 'location': None} for line in lines}

    keyed = {}
    conn = db.get_db()
    try:
        # Streamed rather than fetchall()'d: a row for a merchant that isn't on
        # screen is dropped as it arrives instead of being kept and indexed.
        for old in conn.execute(
                "SELECT id, line_date, reference, location "
                "FROM cc_lines WHERE card_id=? AND category='spend' "
                "AND trim(COALESCE(location, '')) != '' "
                "ORDER BY COALESCE(line_date, '') DESC, id DESC "
                "LIMIT ?",
                (card_id, _MERCHANT_HISTORY_LIMIT)):
            key = key_for(old['reference'])
            if key in wanted:
                keyed.setdefault(key, []).append(old)
    finally:
        conn.close()

    result = {}
    for line in lines:
        key = line_keys[line['id']]
        location = None
        for old in keyed.get(key, []):
            if old['id'] == line['id']:
                continue
            # Use only a genuinely earlier transaction, including an earlier
            # line on the same date. A later row must not teach an older one.
            old_key = (str(old['line_date'] or ''), old['id'])
            line_key = (str(line['line_date'] or ''), line['id'])
            if old_key >= line_key:
                continue
            if location is None and (old['location'] or '').strip():
                location = old['location'].strip()
            if location is not None:
                break
        result[line['id']] = {'reason': None, 'location': location}
    return result


def _statement_ai_states(statement_id):
    """Return explicit per-line AI receipt state supported by existing data."""
    conn = db.get_db()
    try:
        states = {}
        for row in conn.execute(
                "SELECT line_id, status, COUNT(*) AS n "
                "FROM cc_line_receipt_suggestions "
                "WHERE line_id IN (SELECT id FROM cc_lines WHERE statement_id=?) "
                "AND NOT (status='suggested' AND EXISTS ("
                "    SELECT 1 FROM cc_receipt_lines linked "
                "    WHERE linked.line_id=cc_line_receipt_suggestions.line_id "
                "      AND linked.receipt_id=cc_line_receipt_suggestions.receipt_id)) "
                "GROUP BY line_id, status", (statement_id,)).fetchall():
            states.setdefault(row['line_id'], {})[row['status']] = row['n']
        return states
    finally:
        conn.close()


@app.route('/portal/cards')
def cc_portal():
    email = _cc_user()
    cards = db.find_cc_cards_for_email(email)
    rm = db.get_rm_user(email)
    is_active_rm = bool(rm and rm['active'])
    if is_active_rm and len(cards) != 1:
        if cards:
            flash('More than one card is linked to your RM profile. Ask an administrator '
                  'to choose your single company card.', 'warning')
        else:
            flash('No company card is assigned to your RM profile yet.', 'info')
        return redirect(url_for('rm_dashboard'))
    if not cards:
        flash('No credit card is linked to your login yet.', 'info')
        return redirect(url_for('cc_portal_logout'))
    if len(cards) == 1:
        return redirect(url_for('cc_portal_card', card_id=cards[0]['id']))
    return render_template('portal_cc_pick.html', cards=cards)


@app.route('/portal/cards/<int:card_id>')
def cc_portal_card(card_id):
    card = _require_card_access(card_id)
    statements = db.list_cc_statements(card_id)
    if not statements:
        return render_template('portal_cc_card.html', card=card, statements=[],
                               statement=None, rows=[], bucket=[],
                               total=0, covered=0, outstanding_by_statement={},
                               ai_enabled=cc_ai.FEATURE_ENABLED,
                               ai_status=db.get_cc_ai_status(card_id),
                               inbox=db.list_cc_inbox_receipts(card_id))
    # Land on a month that actually has work. `statements` is newest-first, so
    # defaulting to [0] meant a cardholder opening the portal in August saw an
    # empty August while July still held everything they owed. An explicit
    # ?statement_id always wins — this only changes where they arrive.
    outstanding_by_statement = db.count_cc_outstanding_by_statement(card_id)
    sid = request.args.get('statement_id', type=int)
    statement = next((s for s in statements if s['id'] == sid), None)
    if statement is None:
        # Oldest first: if two months are outstanding, the older debt is the one
        # finance is chasing.
        statement = next(
            (s for s in reversed(statements)
             if outstanding_by_statement.get(s['id'])), statements[0])

    # Transactions the admin has marked reconciled in Xero are closed off and
    # drop out of the cardholder's checklist (and every count derived from it).
    lines = db.get_cc_statement_lines(statement['id'], needing_receipts_only=True,
                                      exclude_reconciled=True)
    receipts = db.list_cc_receipts(statement['id'])
    by_id = {r['id']: r for r in receipts}
    links_by_line = {}
    linked_ids = set()
    for lk in db.list_cc_receipt_links(statement['id']):
        links_by_line.setdefault(lk['line_id'], []).append(lk['receipt_id'])
        linked_ids.add(lk['receipt_id'])
    bucket = [r for r in receipts if r['id'] not in linked_ids]

    field_suggestions = _merchant_field_suggestions(card_id, lines)
    ai_states = _statement_ai_states(statement['id'])
    rows = []
    for l in lines:
        rc = [by_id[i] for i in links_by_line.get(l['id'], []) if i in by_id]
        personal = bool(l['personal'])
        handled = bool(rc) or personal
        has_reason = bool((l['reason'] or '').strip())
        has_location = bool((l['location'] or '').strip())
        line_ai = ai_states.get(l['id'], {})
        pending_ai = bool(line_ai.get('suggested'))
        if pending_ai:
            ai_state = 'suggested'
        elif line_ai.get('confirmed'):
            ai_state = 'confirmed'
        elif line_ai.get('rejected'):
            ai_state = 'rejected'
        elif rc:
            ai_state = 'manual'
        else:
            ai_state = 'not_matched'
        missing = []
        if not handled:
            missing.append('receipt')
        if not has_reason:
            missing.append('reason')
        if not has_location:
            missing.append('location')
        if pending_ai:
            missing.append('ai')
        if l['vat_invoice_required']:
            missing.insert(0, 'vat_invoice')
        rows.append({'l': l, 'receipts': rc, 'personal': personal,
                     'submitted': bool(l['submitted_at']), 'handled': handled,
                     'has_reason': has_reason, 'has_location': has_location,
                     'pending_ai': pending_ai, 'ai_state': ai_state,
                     'missing': missing, 'ready': not missing,
                     'next_action': missing[0] if missing else None,
                     'upload_token': _line_upload_token(
                         card_id, statement['id'], l['id']),
                     'field_suggestions': field_suggestions.get(l['id'], {})})
    receipt_required = sum(1 for r in rows if not r['personal'])
    covered = sum(1 for r in rows if not r['personal'] and r['receipts'])
    submitted_count = sum(1 for r in rows if r['submitted'])
    personal_count = sum(1 for r in rows if r['personal'])
    with_reason = sum(1 for r in rows if r['has_reason'])
    with_location = sum(1 for r in rows if r['has_location'])
    pending_ai_count = sum(1 for r in rows if r['pending_ai'])
    missing_receipt_count = sum(1 for r in rows if 'receipt' in r['missing'])
    missing_reason_count = sum(1 for r in rows if 'reason' in r['missing'])
    missing_location_count = sum(1 for r in rows if 'location' in r['missing'])
    vat_invoice_request_count = sum(
        1 for r in rows if 'vat_invoice' in r['missing'])
    complete = sum(1 for r in rows if r['ready'])
    ready_count = sum(1 for r in rows if r['ready'] and not r['submitted'])
    next_row = next((r for r in rows if not r['ready']), None)
    total_cents = sum(abs(r['l']['amount_cents']) for r in rows)
    receipt_required_cents = sum(abs(r['l']['amount_cents']) for r in rows
                                 if not r['personal'])
    outstanding_cents = sum(abs(r['l']['amount_cents']) for r in rows if not r['handled'])
    personal_cents = sum(abs(r['l']['amount_cents']) for r in rows if r['personal'])
    # Only for transactions still on the checklist — a suggestion against a
    # closed-off line would ask the cardholder to decide something that is gone.
    visible_ids = {r['l']['id'] for r in rows}
    suggestions = [s for s in db.list_cc_suggestions_for_statement(statement['id'])
                   if s['line_id'] in visible_ids]

    focus_id = request.args.get('focus', type=int)
    focus_row = next((r for r in rows if r['l']['id'] == focus_id), None)
    if focus_id and focus_row is None:
        focus_id = None
    display_rows = [focus_row] if focus_row else rows
    focus_prev = focus_next = focus_next_missing = None
    focus_position = None
    if focus_row:
        idx = rows.index(focus_row)
        focus_position = idx + 1
        focus_prev = rows[idx - 1] if idx > 0 else None
        focus_next = rows[idx + 1] if idx + 1 < len(rows) else None
        ordered_after = rows[idx + 1:] + rows[:idx]
        focus_next_missing = next((r for r in ordered_after if not r['ready']), None)
    display_suggestions = [
        s for s in suggestions if not focus_row or s['line_id'] == focus_id]

    return render_template(
        'portal_cc_card.html', card=card, statements=statements, statement=statement,
        rows=rows, display_rows=display_rows, focus_row=focus_row,
        focus_prev=focus_prev, focus_next=focus_next,
        focus_next_missing=focus_next_missing, focus_id=focus_id,
        focus_position=focus_position,
        bucket=bucket, all_receipts=receipts, total=len(rows),
        outstanding_by_statement=outstanding_by_statement,
        covered=covered, receipt_required=receipt_required,
        personal_count=personal_count, with_reason=with_reason,
        with_location=with_location, pending_ai_count=pending_ai_count,
        missing_receipt_count=missing_receipt_count,
        missing_reason_count=missing_reason_count,
        missing_location_count=missing_location_count,
        vat_invoice_request_count=vat_invoice_request_count,
        complete=complete, total_cents=total_cents, outstanding_cents=outstanding_cents,
        receipt_required_cents=receipt_required_cents,
        personal_cents=personal_cents, locked=_locked(statement),
        submitted_count=submitted_count, ready_count=ready_count, next_row=next_row,
        suggestions=suggestions, display_suggestions=display_suggestions,
        ai_enabled=cc_ai.FEATURE_ENABLED,
        ai_status=db.get_cc_ai_status(card_id, statement['id']),
        submitted_at=statement['submitted_at'],
        cc_locations=db.get_cc_locations(),
        inbox=db.list_cc_inbox_receipts(card['id']))


@app.route('/portal/cards/<int:card_id>/upload', methods=['POST'])
def cc_portal_upload(card_id):
    _require_card_access(card_id)
    as_json = _wants_json()

    def _fail(msg, cat='danger', sid=None):
        # JSON path: return the message so the client can toast it (the upload
        # bar is XHR, no page reload). No-JS path: flash + redirect as before.
        if as_json:
            return jsonify({'ok': False, 'error': msg}), 400
        flash(msg, cat)
        return _portal_return(card_id, sid)

    statement_id = request.form.get('statement_id', type=int)
    statement = db.get_cc_statement(statement_id)
    if not statement or statement['card_id'] != card_id:
        return _fail('Could not find that month to upload to.')
    if _locked(statement):
        return _fail('This month has been submitted and is locked.', 'warning', statement_id)

    # Optional: attach straight onto one transaction instead of the bucket.
    line_was_posted = 'line_id' in request.form
    line_id = request.form.get('line_id', type=int)
    if line_was_posted:
        # Direct attachments use a separate URL + signed target.  Keeping the
        # month-bucket endpoint from accepting a caller-supplied line id removes
        # the old ambiguous path where the receipt could commit before its link.
        return _fail('That transaction upload form is out of date. Refresh and try again.',
                     'danger', statement_id)

    files = [f for f in request.files.getlist('receipts') if f and f.filename]
    if not files:
        return _fail('Choose at least one receipt or invoice to upload.', 'danger', statement_id)

    saved, skipped, dups, unlinked = 0, [], 0, 0
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in _RECEIPT_EXT:
            skipped.append(f'{f.filename} (unsupported type)')
            continue
        data = f.read()
        if len(data) > _MAX_RECEIPT_BYTES:
            skipped.append(f'{f.filename} (over 15 MB)')
            continue
        if not _content_matches_ext(ext, data):
            skipped.append(f'{f.filename} (content does not match its type)')
            continue
        digest = hashlib.sha256(data).hexdigest()
        # Dedup is scoped to THIS month's bucket: a genuine re-upload of the same
        # file into the same month is skipped (or linked to the line), but the
        # same file may still be added to a different month — e.g. one
        # quarterly/annual invoice covering charges across several statements.
        same_month = db.find_cc_receipt_by_hash_in_statement(statement_id, digest)
        if same_month:
            if line_id:
                try:
                    db.link_cc_receipt(same_month['id'], line_id,
                                       actor=_link_actor())
                except Exception:
                    # The existing receipt is still safely in this month's
                    # bucket. Report the partial result instead of turning an
                    # idempotent retry into a 500 with ambiguous state.
                    unlinked += 1
            dups += 1
            continue
        # HEIC/HEIF becomes JPEG here (and `name`/`ext` follow it); everything else
        # passes through untouched. Deliberately after the dedup lookup above.
        data, name, ext = _prepare_receipt(data, f.filename, ext)
        # Keep the validated extension on the stored name so the file previews
        # inline later (cc_portal_file derives its MIME from the path). A
        # non-ASCII filename can reduce to a dotless string under
        # secure_filename, which would otherwise be served as octet-stream.
        safe = secure_filename(name)
        if not safe.lower().endswith(ext):
            safe = (safe or 'receipt') + ext
        stored = f'{uuid.uuid4().hex}_{safe}'
        rel = f"{card_id}/{statement['year']:04d}-{statement['month']:02d}/{stored}"
        try:
            storage.save(rel, data)
            receipt_id = db.add_cc_receipt(
                card_id, statement_id, rel, scrub.mask_pans(name),
                _SAFE_TYPES[ext], _cc_user() or 'admin', content_hash=digest)
        except Exception:
            try:
                storage.delete(rel)
            except Exception:
                pass
            skipped.append(f'{f.filename} (could not be saved — retry it)')
            continue
        if line_id:
            try:
                db.link_cc_receipt(receipt_id, line_id, actor=_link_actor())
            except Exception:
                # Blob + DB receipt are durable at this point. Keeping it in
                # the month bucket is safer than deleting a successfully
                # uploaded document because the final link write failed.
                unlinked += 1
        saved += 1

    if saved:
        where = ('to this transaction'
                 if line_id and unlinked == 0 else "to this month's bucket")
        flash(f'Uploaded {saved} file{"" if saved == 1 else "s"} {where}.', 'success')
    if dups:
        duplicate_result = (
            ' — linked the existing copy'
            if line_id and unlinked == 0 else
            ' — the existing copy is still under Unmatched files'
            if line_id else ' — skipped'
        )
        flash(f'{dups} file{"" if dups == 1 else "s"} were already uploaded'
              f'{duplicate_result}.', 'info')
    if unlinked:
        flash(f'{unlinked} file{"" if unlinked == 1 else "s"} uploaded safely but '
              'could not be attached. It is available under Unmatched files; '
              'please link it there.', 'warning')
    for s in skipped:
        flash(f'Skipped {s}.', 'warning')
    if saved:
        _kick_ai_worker()  # extract + match the new batch out-of-band
    # JSON path: the flashes above stay queued in the session and render as
    # toasts when the client reloads after the progress bar completes.
    if as_json:
        return jsonify({'ok': True, 'saved': saved, 'dups': dups,
                        'unlinked': unlinked, 'skipped': len(skipped)})
    return _portal_return(card_id, statement_id, anchor_line_id=line_id)


@app.route('/portal/cards/<int:card_id>/statements/<int:statement_id>/'
           'lines/<int:line_id>/receipts', methods=['POST'])
def cc_portal_upload_to_line(card_id, statement_id, line_id):
    """Attach uploaded evidence to exactly one signed, revalidated transaction.

    Unlike the month bucket uploader, this route never leaves a successful file
    unmatched.  Each file's receipt row and join row commit together; any
    database failure rolls both back and the just-saved blob is removed best
    effort.  The response echoes the verified target so the browser can refuse
    to display success if its pending card and server truth ever disagree.
    """
    _require_card_access(card_id)
    as_json = _wants_json()

    def _fail(msg, cat='danger'):
        if as_json:
            return jsonify({'ok': False, 'error': msg}), 400
        flash(msg, cat)
        return _portal_return(card_id, statement_id, anchor_line_id=line_id)

    if not _valid_line_upload_token(
            request.form.get('upload_token'), card_id, statement_id, line_id):
        return _fail('This receipt target is no longer valid. Refresh and try again.')

    statement = db.get_cc_statement(statement_id)
    if not statement or statement['card_id'] != card_id:
        return _fail('Could not find that card statement.')
    line = _portal_spend_line(card_id, statement_id, line_id)
    if not line:
        return _fail('That transaction is no longer open. Refresh and try again.')
    if _locked(statement):
        return _fail('This month has been submitted and is locked.', 'warning')

    files = [f for f in request.files.getlist('receipts') if f and f.filename]
    if not files:
        return _fail('Choose at least one receipt or invoice to attach.')

    attached = new_receipts = duplicates = 0
    skipped = []
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in _RECEIPT_EXT:
            skipped.append(f'{f.filename} (unsupported type)')
            continue
        data = f.read()
        if len(data) > _MAX_RECEIPT_BYTES:
            skipped.append(f'{f.filename} (over 15 MB)')
            continue
        if not _content_matches_ext(ext, data):
            skipped.append(f'{f.filename} (content does not match its type)')
            continue

        digest = hashlib.sha256(data).hexdigest()
        # Convert HEIC/HEIF to JPEG. This route dedups DB-side inside
        # add_and_link_cc_receipt (by content_hash) rather than with a lookup first,
        # so the conversion happens before the save either way — but the hash above
        # is still taken on the uploaded bytes, which is what keeps that dedup working.
        data, name, ext = _prepare_receipt(data, f.filename, ext)
        safe = secure_filename(name)
        if not safe.lower().endswith(ext):
            safe = (safe or 'receipt') + ext
        stored = f'{uuid.uuid4().hex}_{safe}'
        rel = f"{card_id}/{statement['year']:04d}-{statement['month']:02d}/{stored}"
        blob_saved = False
        try:
            storage.save(rel, data)
            blob_saved = True
            result = db.add_and_link_cc_receipt(
                card_id, statement_id, line_id, rel,
                scrub.mask_pans(name), _SAFE_TYPES[ext],
                _cc_user() or 'admin', content_hash=digest,
                actor=_link_actor())
            if result['receipt_created']:
                new_receipts += 1
            else:
                # The content-identical statement receipt is authoritative; the
                # newly uploaded duplicate blob has no DB row and must be removed.
                duplicates += 1
                try:
                    storage.delete(rel)
                except Exception:
                    pass
            attached += 1
        except Exception:
            if blob_saved:
                try:
                    storage.delete(rel)
                except Exception:
                    pass
            skipped.append(f'{f.filename} (could not be attached — retry it)')

    if not attached:
        return _fail('No files were attached. ' + ' '.join(skipped))

    flash(f'Attached {attached} file{"" if attached == 1 else "s"} to '
          f'{line["reference"]} on {line["line_date"]}.', 'success')
    for item in skipped:
        flash(f'Skipped {item}.', 'warning')
    if new_receipts:
        _kick_ai_worker()

    target = {
        'card_id': card_id,
        'statement_id': statement_id,
        'line_id': line_id,
    }
    if as_json:
        return jsonify({
            'ok': True,
            'attached': attached,
            'new_receipts': new_receipts,
            'duplicates': duplicates,
            'skipped': len(skipped),
            'target': target,
        })
    focus_id = request.form.get('focus', type=int)
    return _portal_return(card_id, statement_id, focus_id=focus_id,
                          anchor_line_id=line_id)


@app.route('/portal/cards/<int:card_id>/inbox/upload', methods=['POST'])
def cc_portal_inbox_upload(card_id):
    """Drop off receipts ahead of a statement. They're stored in the card's inbox
    (statement_id NULL) until the cardholder assigns them to a month to match.
    No lock/statement checks — the inbox exists precisely for when no month is
    loaded yet."""
    _require_card_access(card_id)
    as_json = _wants_json()

    def _fail(msg, cat='danger'):
        if as_json:
            return jsonify({'ok': False, 'error': msg}), 400
        flash(msg, cat)
        return _portal_return(card_id, request.form.get('statement_id', type=int))

    files = [f for f in request.files.getlist('receipts') if f and f.filename]
    if not files:
        return _fail('Choose at least one receipt or invoice to drop off.')

    saved, skipped, dups = 0, [], 0
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in _RECEIPT_EXT:
            skipped.append(f'{f.filename} (unsupported type)')
            continue
        data = f.read()
        if len(data) > _MAX_RECEIPT_BYTES:
            skipped.append(f'{f.filename} (over 15 MB)')
            continue
        if not _content_matches_ext(ext, data):
            skipped.append(f'{f.filename} (content does not match its type)')
            continue
        digest = hashlib.sha256(data).hexdigest()
        # Dedup card-wide: if this exact file is anywhere on the card already
        # (inbox or a month), don't drop another copy.
        if db.find_cc_receipt_by_hash(card_id, digest):
            dups += 1
            continue
        data, name, ext = _prepare_receipt(data, f.filename, ext)
        safe = secure_filename(name)
        if not safe.lower().endswith(ext):
            safe = (safe or 'receipt') + ext
        rel = f"{card_id}/inbox/{uuid.uuid4().hex}_{safe}"
        try:
            storage.save(rel, data)
            db.add_cc_receipt(card_id, None, rel, scrub.mask_pans(name),
                              _SAFE_TYPES[ext], _cc_user() or 'admin', content_hash=digest)
        except Exception:
            try:
                storage.delete(rel)
            except Exception:
                pass
            skipped.append(f'{f.filename} (could not be saved — retry it)')
            continue
        saved += 1

    if saved:
        flash(f'Dropped off {saved} receipt{"" if saved == 1 else "s"} — they’ll wait '
              f'here until the matching transactions are loaded.', 'success')
    if dups:
        flash(f'{dups} file{"" if dups == 1 else "s"} already uploaded — skipped.', 'info')
    for s in skipped:
        flash(f'Skipped {s}.', 'warning')
    if as_json:
        return jsonify({'ok': True, 'saved': saved, 'dups': dups, 'skipped': len(skipped)})
    return _portal_return(card_id, request.form.get('statement_id', type=int))


@app.route('/portal/cards/<int:card_id>/inbox/assign', methods=['POST'])
def cc_portal_inbox_assign(card_id):
    """Move a dropped-off receipt into the selected month's bucket so it can be
    linked to a transaction and given a reason."""
    _require_card_access(card_id)
    receipt_id = request.form.get('receipt_id', type=int)
    statement_id = request.form.get('statement_id', type=int)
    stmt = db.get_cc_statement(statement_id)
    r = db.get_cc_receipt(receipt_id)
    if (not stmt or stmt['card_id'] != card_id or not r or r['card_id'] != card_id
            or r['statement_id'] is not None):
        flash('Could not move that receipt.', 'danger')
    elif _locked(stmt):
        flash('This month has been submitted and is locked.', 'warning')
    elif db.assign_cc_receipt_to_statement(receipt_id, statement_id):
        _kick_ai_worker()  # let the AI try to match it now it's in a month
        flash('Moved into this month — now link it to a transaction and add a reason.', 'success')
    else:
        flash('Could not move that receipt.', 'danger')
    return _portal_return(card_id, statement_id)


@app.route('/portal/cards/<int:card_id>/link', methods=['POST'])
def cc_portal_link(card_id):
    _require_card_access(card_id)
    receipt_id = request.form.get('receipt_id', type=int)
    statement_id = request.form.get('statement_id', type=int)
    line_ids = request.form.getlist('line_id', type=int)  # one or many
    r = db.get_cc_receipt(receipt_id)
    if (not r or r['card_id'] != card_id
            or r['statement_id'] != statement_id):
        flash('Could not link that receipt.', 'danger')
        return _portal_return(card_id, statement_id)
    # Lock check against the receipt's OWN month, not a caller-supplied one, so a
    # locked month can't be edited by naming a different unlocked statement_id.
    if _locked(db.get_cc_statement(r['statement_id'])):
        flash('This month has been submitted and is locked.', 'warning')
        return _portal_return(card_id, statement_id)
    n = 0
    for line_id in line_ids:
        line = _portal_spend_line(card_id, r['statement_id'], line_id)
        if line:
            db.link_cc_receipt(receipt_id, line_id, actor=_link_actor())
            n += 1
    if n:
        flash(f'Linked to {n} transaction{"" if n == 1 else "s"}.', 'success')
    else:
        flash('Pick at least one transaction to link to.', 'warning')
    return _portal_return(card_id, statement_id)


@app.route('/portal/cards/<int:card_id>/reason', methods=['POST'])
def cc_portal_reason(card_id):
    _require_card_access(card_id)
    line_id = request.form.get('line_id', type=int)
    statement_id = request.form.get('statement_id', type=int)
    is_fetch = request.headers.get('X-Requested-With') == 'fetch'
    line = _portal_spend_line(card_id, statement_id, line_id)
    # Validate ownership first, then check the lock against the LINE's own
    # statement (not a caller-supplied statement_id) so a locked month can't be
    # edited by naming a different unlocked statement.
    if not line:
        if is_fetch:
            return ('error', 400)
        flash('Could not save that reason.', 'danger')
    elif _locked(db.get_cc_statement(line['statement_id'])):
        if is_fetch:
            return ('locked', 423)
        flash('This month has been submitted and is locked.', 'warning')
    else:
        db.set_cc_line_reason(line_id, request.form.get('reason', ''))
        if is_fetch:
            return ('saved', 200)
        flash('Reason saved.', 'success')
    return _portal_return(card_id, statement_id, anchor_line_id=line_id)


@app.route('/portal/cards/<int:card_id>/location', methods=['POST'])
def cc_portal_location(card_id):
    """Save a transaction's free-text location (autosaves on blur, like reason).
    Lock is checked against the line's OWN statement, not a caller-supplied one."""
    _require_card_access(card_id)
    line_id = request.form.get('line_id', type=int)
    statement_id = request.form.get('statement_id', type=int)
    is_fetch = request.headers.get('X-Requested-With') == 'fetch'
    line = _portal_spend_line(card_id, statement_id, line_id)
    if not line:
        if is_fetch:
            return ('error', 400)
        flash('Could not save that location.', 'danger')
    elif _locked(db.get_cc_statement(line['statement_id'])):
        if is_fetch:
            return ('locked', 423)
        flash('This month has been submitted and is locked.', 'warning')
    else:
        db.set_cc_line_location(line_id, request.form.get('location', ''))
        if is_fetch:
            return ('saved', 200)
        flash('Location saved.', 'success')
    return _portal_return(card_id, statement_id, anchor_line_id=line_id)


@app.route('/portal/cards/<int:card_id>/personal', methods=['POST'])
def cc_portal_personal(card_id):
    _require_card_access(card_id)
    line_id = request.form.get('line_id', type=int)
    statement_id = request.form.get('statement_id', type=int)
    line = _portal_spend_line(card_id, statement_id, line_id)
    # Validate ownership first, then check the lock against the LINE's own
    # statement (not a caller-supplied statement_id).
    if not line:
        flash('Could not update that transaction.', 'danger')
    elif _locked(db.get_cc_statement(line['statement_id'])):
        flash('This month has been submitted and is locked.', 'warning')
    else:
        db.set_cc_line_personal(line_id, not line['personal'])
    return _portal_return(card_id, statement_id, anchor_line_id=line_id)


@app.route('/portal/cards/<int:card_id>/submit', methods=['POST'])
def cc_portal_submit(card_id):
    _require_card_access(card_id)
    statement_id = request.form.get('statement_id', type=int)
    statement = db.get_cc_statement(statement_id)
    if not statement or statement['card_id'] != card_id:
        abort(404)
    line_ids = [l['id'] for l in db.get_cc_statement_lines(
        statement_id, needing_receipts_only=True, exclude_reconciled=True)]
    ready = db.get_cc_ready_line_ids(line_ids)
    if len(ready) != len(line_ids):
        flash('Finish each receipt or personal choice, reason, location, and AI '
              'match review before submitting the month.', 'warning')
        return _portal_return(card_id, statement_id)
    db.set_cc_statement_submitted(statement_id, _cc_user() or 'admin')
    flash('Submitted to finance — thank you! You can still add or change '
          'anything afterwards.', 'success')
    return _portal_return(card_id, statement_id)


@app.route('/portal/cards/<int:card_id>/submit-lines', methods=['POST'])
def cc_portal_submit_lines(card_id):
    """Cardholder submits one transaction (individual) or many (bulk select) to
    finance. A soft 'sent' marker per line — NOT a lock; lines stay editable.
    Pass undo=1 to move them back to draft. AJAX (X-Requested-With: fetch) gets
    JSON so the page can update in place; a no-JS post falls back to a redirect."""
    _require_card_access(card_id)
    is_fetch = request.headers.get('X-Requested-With') == 'fetch'
    statement_id = request.form.get('statement_id', type=int)
    undo = request.form.get('undo') == '1'
    statement = db.get_cc_statement(statement_id)
    if not statement or statement['card_id'] != card_id:
        if is_fetch:
            return jsonify({'ok': False, 'error': 'Could not find that statement.'}), 400
        abort(404)
    # Never trust the posted ids — keep only lines in this card and month.
    valid = []
    for lid in request.form.getlist('line_id', type=int):
        line = _portal_spend_line(card_id, statement_id, lid)
        if line:
            valid.append(lid)
    valid = list(dict.fromkeys(valid))
    if not undo:
        ready = db.get_cc_ready_line_ids(valid)
        incomplete = [lid for lid in valid if lid not in ready]
        if incomplete:
            suffix = '' if len(incomplete) == 1 else 's'
            msg = ('Finish the receipt/personal choice, reason, location, and any '
                   'suggested receipt review before submitting '
                   f'{len(incomplete)} transaction{suffix}.')
            if is_fetch:
                return jsonify({'ok': False, 'error': msg,
                                'incomplete_ids': incomplete}), 400
            flash(msg, 'warning')
            return _portal_return(card_id, statement_id)
    n = db.set_cc_lines_submitted(valid, _cc_user() or 'admin', submitted=not undo)
    if is_fetch:
        return jsonify({'ok': True, 'count': n, 'submitted': not undo})
    if not n:
        flash('No transactions selected.', 'warning')
    elif undo:
        flash(f'{n} transaction{"" if n == 1 else "s"} moved back to draft.', 'info')
    else:
        flash(f'Submitted {n} transaction{"" if n == 1 else "s"} to finance — '
              f'thank you! You can still change anything afterwards.', 'success')
    return _portal_return(card_id, statement_id)


@app.route('/portal/cards/<int:card_id>/suggestions/<int:suggestion_id>/confirm',
           methods=['POST'])
def cc_portal_confirm_suggestion(card_id, suggestion_id):
    """Cardholder confirms one AI-proposed receipt match within their own card."""
    _require_card_access(card_id)
    suggestion = db.get_cc_suggestion(suggestion_id)
    if not suggestion or suggestion['card_id'] != card_id:
        abort(404)
    if not _portal_spend_line(
            card_id, suggestion['statement_id'], suggestion['line_id']):
        abort(404)
    statement = db.get_cc_statement(suggestion['statement_id'])
    if _locked(statement):
        flash('This month is locked.', 'warning')
    else:
        result = db.confirm_cc_suggestion(suggestion_id, actor=_link_actor())
        if result:
            receipt_id, line_id = result
            receipt = db.get_cc_receipt(receipt_id)
            line = db.get_cc_line(line_id)
            if receipt is not None and line is not None:
                ext = os.path.splitext(receipt['file_path'])[1] or '.pdf'
                extra = max(0, db.count_cc_receipt_links(receipt_id) - 1)
                db.set_cc_receipt_download_name(
                    receipt_id, cc_ai.download_name_for(
                        line['reference'], line['line_date'], line['amount_cents'],
                        ext, extra_count=extra))
            flash('Receipt match confirmed.', 'success')
        else:
            flash('That suggestion is no longer available.', 'warning')
    return _portal_return(card_id, suggestion['statement_id'])


@app.route('/portal/cards/<int:card_id>/suggestions/<int:suggestion_id>/dismiss',
           methods=['POST'])
def cc_portal_reject_suggestion(card_id, suggestion_id):
    """Cardholder dismisses an incorrect AI match within their own card."""
    _require_card_access(card_id)
    suggestion = db.get_cc_suggestion(suggestion_id)
    if not suggestion or suggestion['card_id'] != card_id:
        abort(404)
    if not _portal_spend_line(
            card_id, suggestion['statement_id'], suggestion['line_id']):
        abort(404)
    if _locked(db.get_cc_statement(suggestion['statement_id'])):
        flash('This month is locked.', 'warning')
    else:
        db.reject_cc_suggestion(suggestion_id)
        flash('Suggestion dismissed. The file is still available to link manually.', 'info')
    return _portal_return(card_id, suggestion['statement_id'])


@app.route('/portal/cards/<int:card_id>/unlink', methods=['POST'])
def cc_portal_unlink(card_id):
    _require_card_access(card_id)
    receipt_id = request.form.get('receipt_id', type=int)
    line_id = request.form.get('line_id', type=int)
    statement_id = request.form.get('statement_id', type=int)
    line = _portal_spend_line(card_id, statement_id, line_id)
    r = db.get_cc_receipt(receipt_id)
    if (not line or not r or r['card_id'] != card_id
            or r['statement_id'] != statement_id):
        flash('Could not detach that receipt.', 'danger')
        return _portal_return(card_id, statement_id)
    if _locked(db.get_cc_statement(line['statement_id'])):
        flash('This month has been submitted and is locked.', 'warning')
        return _portal_return(card_id, statement_id)
    db.unlink_cc_receipt(receipt_id, line_id)
    flash('Receipt detached from that transaction.', 'info')
    return _portal_return(card_id, statement_id, anchor_line_id=line_id)


@app.route('/portal/receipts/<int:receipt_id>/delete', methods=['POST'])
def cc_portal_delete_receipt(receipt_id):
    r = db.get_cc_receipt(receipt_id)
    if not r:
        abort(404)
    card = _require_card_access(r['card_id'])
    # Respect the lock like every other cardholder mutation — a submitted month
    # is final; the delete button is hidden in the UI but the POST must refuse it
    # too. _locked() already returns False for admins, who own the reopen flow.
    if _locked(db.get_cc_statement(r['statement_id'])):
        flash('This month has been submitted and is locked.', 'warning')
        return _portal_return(card['id'], r['statement_id'])
    file_path = db.delete_cc_receipt(receipt_id)
    if file_path:
        try:
            storage.delete(file_path)
        except Exception:
            pass  # deletion is complete in DB; leave only a harmless orphan blob
    flash('Removed.', 'info')
    return _portal_return(card['id'], r['statement_id'])


@app.route('/portal/receipts/<int:receipt_id>/file')
def cc_portal_file(receipt_id):
    r = db.get_cc_receipt(receipt_id)
    if not r:
        abort(404)
    _require_card_access(r['card_id'])  # 403 unless cardholder-with-access or admin
    try:
        data = storage.read(r['file_path'])
    except FileNotFoundError:
        abort(404)
    # Derive the served type from the stored extension, never the (client-set)
    # stored content_type. Anything not a browser-safe image/PDF is forced to
    # download so it can't execute in our origin.
    ext = os.path.splitext(r['file_path'])[1].lower()
    mime = _SAFE_TYPES.get(ext, 'application/octet-stream')
    # Prefer the transaction-derived name once matched, so the file is
    # self-labelling on download; fall back to the original upload name.
    dl_name = r['download_name'] or r['original_filename'] or f'receipt{ext}'
    return send_file(io.BytesIO(data), mimetype=mime,
                     as_attachment=mime not in _INLINE_TYPES,
                     download_name=dl_name)


@app.route('/portal/logout')
def cc_portal_logout():
    session.pop('cc_user', None)
    session.pop('cc_last_active', None)
    if not session.get('admin'):
        session.pop('uid', None)
        session.pop('auth_version', None)
    flash('Logged out.', 'info')
    return redirect(url_for('landing'))


# Imported for its route registration side effect — the cross-card admin review
# queue is a sibling module in this package, so it registers with the card
# routes rather than being reached into from app.py. Last, so nothing here is
# half-defined when it imports.
from northwind.cards import admin_review  # noqa: E402,F401
