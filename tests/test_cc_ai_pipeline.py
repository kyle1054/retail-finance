"""The DB side of the credit-card AI pipeline.

Covers the three things that decide whether the AI feels useful or clunky:
  - a receipt's extracted detail actually reaching the coding prompt,
  - a receipt arriving late re-queueing its line for coding,
  - failed receipts not being re-read on every hourly run.
"""
import json
import datetime as dt

import pytest

from northwind.cards import ai as cc_ai
from northwind.data import database as db
from northwind.cards.parser import CardSnapshot, StatementLine


@pytest.fixture(autouse=True)
def _isolate_ai_state(conn):
    """Undo this module's receipt and merchant-memory writes after each test.

    The db_copy fixture is session-scoped, so both leak otherwise:
      - receipts here are deliberately left 'failed', which would corrupt the
        global ai_error tally that test_cc_link_provenance asserts exact counts on;
      - a remembered merchant would silently code a LATER test's line from memory
        instead of sending it to the (stubbed) model.
    """
    high_water = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS m FROM cc_receipts").fetchone()['m']
    memory = conn.execute(
        "SELECT merchant_key, account_code, account_name, hits, updated_at "
        "FROM cc_merchant_map").fetchall()
    yield
    conn.execute("DELETE FROM cc_receipt_lines WHERE receipt_id > ?", (high_water,))
    conn.execute("DELETE FROM cc_receipts WHERE id > ?", (high_water,))
    conn.execute("DELETE FROM cc_merchant_map")
    conn.executemany(
        "INSERT INTO cc_merchant_map (merchant_key, account_code, account_name, "
        "hits, updated_at) VALUES (?,?,?,?,?)", [tuple(r) for r in memory])
    conn.commit()


def _line(ref, cents, fp):
    return StatementLine(line_date=dt.date(2026, 5, 10), reference=ref,
                         amount_cents=cents, category='spend', reconciled=False,
                         fingerprint=fp, occurrence=0)


def _make_card(name):
    snap = CardSnapshot(
        card_name=name, display_name=name.split()[0],
        period_start=dt.date(2026, 5, 1), period_end=dt.date(2026, 5, 31),
        as_at=dt.date(2026, 5, 31), statement_balance_cents=None,
        lines=[_line('SHOPFRONT', -95700, name + '-a'),
               _line('DL RIDECO WST', -5000, name + '-b')],
        duplicates_removed_by_xero=0, source_filename='pytest.xlsx')
    r = db.import_card_snapshot(snap)
    return r['card_id']


def _ids(conn, card_id):
    return [row['id'] for row in conn.execute(
        "SELECT id FROM cc_lines WHERE card_id=? ORDER BY id", (card_id,)).fetchall()]


def _statement_id(conn, card_id):
    return conn.execute("SELECT id FROM cc_statements WHERE card_id=?",
                        (card_id,)).fetchone()['id']


def _add_receipt(conn, card_id, tag, raw=None, vendor=None, ai_status='processed'):
    sid = _statement_id(conn, card_id)
    rid = db.add_cc_receipt(card_id, sid, f'{card_id}/{tag}.jpg', f'{tag}.jpg',
                            'image/jpeg', 'pytest', content_hash=f'{card_id}-{tag}')
    conn.execute("UPDATE cc_receipts SET ai_raw_json=?, ai_vendor=?, ai_status=? "
                 "WHERE id=?", (raw, vendor, ai_status, rid))
    conn.commit()
    return rid


# ── The receipt detail that feeds the coding prompt ───────────────────────────

def test_receipt_line_items_reach_the_coding_prompt(conn):
    """The whole point of widening the extraction schema: coding "SHOPFRONT R957"
    blind is why many lines landed on the fallback accounts."""
    cid = _make_card('Pytest CC AI Detail Card')
    line_id = _ids(conn, cid)[0]
    raw = json.dumps({'vendor': 'Shopfront', 'total_cents': 95700,
                      'summary': 'office furniture',
                      'line_items': ['Office chair', 'Desk mat']})
    rid = _add_receipt(conn, cid, 'detail', raw=raw, vendor='Shopfront')
    db.link_cc_receipt(rid, line_id)

    detail = db.get_cc_receipt_details_for_lines([line_id])
    assert 'office furniture' in detail[line_id]
    assert 'Office chair' in detail[line_id]


def test_lines_without_a_receipt_are_simply_absent(conn):
    cid = _make_card('Pytest CC AI No Receipt Card')
    ids = _ids(conn, cid)
    assert db.get_cc_receipt_details_for_lines(ids) == {}
    assert db.get_cc_receipt_details_for_lines([]) == {}


def test_a_pre_enrichment_receipt_still_contributes_its_vendor(conn):
    """Receipts extracted before the schema was widened have no line_items. The
    extracted vendor is still better evidence than the bank's mangled reference."""
    cid = _make_card('Pytest CC AI Legacy Receipt Card')
    line_id = _ids(conn, cid)[0]
    raw = json.dumps({'vendor': 'Greenfields', 'total_cents': 95700})
    rid = _add_receipt(conn, cid, 'legacy', raw=raw, vendor='Greenfields')
    db.link_cc_receipt(rid, line_id)
    assert 'Greenfields' in db.get_cc_receipt_details_for_lines([line_id])[line_id]


def test_unparsable_extraction_json_is_ignored_not_raised(conn):
    cid = _make_card('Pytest CC AI Bad Json Card')
    line_id = _ids(conn, cid)[0]
    rid = _add_receipt(conn, cid, 'badjson', raw='{not json at all', vendor=None)
    db.link_cc_receipt(rid, line_id)
    assert db.get_cc_receipt_details_for_lines([line_id]) == {}


def test_an_unextracted_receipt_contributes_nothing(conn):
    """A receipt still pending extraction has no detail to offer yet."""
    cid = _make_card('Pytest CC AI Pending Receipt Card')
    line_id = _ids(conn, cid)[0]
    raw = json.dumps({'summary': 'should not be used'})
    rid = _add_receipt(conn, cid, 'pending', raw=raw, ai_status='pending')
    db.link_cc_receipt(rid, line_id)
    assert db.get_cc_receipt_details_for_lines([line_id]) == {}


def test_several_receipts_on_one_line_are_all_offered(conn):
    cid = _make_card('Pytest CC AI Multi Receipt Card')
    line_id = _ids(conn, cid)[0]
    r1 = _add_receipt(conn, cid, 'multi1',
                      raw=json.dumps({'summary': 'chair'}))
    r2 = _add_receipt(conn, cid, 'multi2',
                      raw=json.dumps({'summary': 'desk mat'}))
    db.link_cc_receipt(r1, line_id)
    db.link_cc_receipt(r2, line_id)
    text = db.get_cc_receipt_details_for_lines([line_id])[line_id]
    assert 'chair' in text and 'desk mat' in text


# ── A late receipt re-queues its line for coding ─────────────────────────────

def test_linking_a_receipt_requeues_the_line_for_coding(conn):
    """A line coded before its receipt arrived was coded off the merchant string
    alone; the new evidence has to trigger a re-code or it is wasted."""
    cid = _make_card('Pytest CC AI Requeue Card')
    line_id = _ids(conn, cid)[0]
    db.set_cc_line_ai_coding(line_id, '6170', 'General Expenses', 'low', True,
                             'no idea', 'ai')
    assert conn.execute("SELECT coding_dirty FROM cc_lines WHERE id=?",
                        (line_id,)).fetchone()['coding_dirty'] == 0

    rid = _add_receipt(conn, cid, 'requeue',
                       raw=json.dumps({'summary': 'office furniture'}))
    db.link_cc_receipt(rid, line_id)
    assert conn.execute("SELECT coding_dirty FROM cc_lines WHERE id=?",
                        (line_id,)).fetchone()['coding_dirty'] == 1


def test_relinking_the_same_receipt_does_not_requeue_again(conn):
    cid = _make_card('Pytest CC AI Relink Card')
    line_id = _ids(conn, cid)[0]
    rid = _add_receipt(conn, cid, 'relink', raw=json.dumps({'summary': 'x'}))
    db.link_cc_receipt(rid, line_id)
    db.set_cc_line_ai_coding(line_id, '6400', 'Local Travel - Other', 'high', False,
                             'coded with the receipt', 'ai')
    # A second link attempt is a no-op, so it must not dirty a settled line.
    assert db.link_cc_receipt(rid, line_id) is False
    assert conn.execute("SELECT coding_dirty FROM cc_lines WHERE id=?",
                        (line_id,)).fetchone()['coding_dirty'] == 0


def test_a_xero_reconciled_line_is_never_requeued(conn):
    """Reconciled lines are closed. Re-coding one would churn settled work and
    spend an API call to change nothing anyone is looking at."""
    cid = _make_card('Pytest CC AI Reconciled Card')
    line_id = _ids(conn, cid)[0]
    db.set_cc_line_ai_coding(line_id, '6170', 'General Expenses', 'low', True, '', 'ai')
    conn.execute("UPDATE cc_lines SET xero_reconciled=1 WHERE id=?", (line_id,))
    conn.commit()
    rid = _add_receipt(conn, cid, 'reconciled', raw=json.dumps({'summary': 'x'}))
    db.link_cc_receipt(rid, line_id)
    assert conn.execute("SELECT coding_dirty FROM cc_lines WHERE id=?",
                        (line_id,)).fetchone()['coding_dirty'] == 0


# ── Retry policy: stop burning calls on hopeless receipts ─────────────────────

def _claimable_ids(statement_id):
    return {r['id'] for r in db.claim_cc_receipts_pending_ai(
        limit=50, statement_id=statement_id)}


def test_a_terminal_failure_is_never_retried(conn):
    cid = _make_card('Pytest CC AI Terminal Card')
    sid = _statement_id(conn, cid)
    rid = _add_receipt(conn, cid, 'terminal', ai_status='pending')
    db.set_cc_receipt_ai_status(rid, 'failed', error='unsupported_media')
    assert rid not in _claimable_ids(sid)


def test_a_retryable_failure_waits_for_the_backoff_then_is_retried(conn):
    cid = _make_card('Pytest CC AI Backoff Card')
    sid = _statement_id(conn, cid)
    rid = _add_receipt(conn, cid, 'backoff', ai_status='pending')
    db.set_cc_receipt_ai_status(rid, 'failed', error='quota_exhausted')
    # Just failed -> held back, so the hourly run doesn't re-hit the same quota wall.
    assert rid not in _claimable_ids(sid)

    # Older than the backoff window -> retried.
    conn.execute("UPDATE cc_receipts SET ai_processed_at="
                 "datetime('now', '-{} hours') WHERE id=?".format(
                     db.CC_AI_RETRY_BACKOFF_HOURS + 1), (rid,))
    conn.commit()
    assert rid in _claimable_ids(sid)


def test_a_fresh_receipt_is_claimed_immediately(conn):
    """The backoff must apply to retries only — a new upload is processed at once."""
    cid = _make_card('Pytest CC AI Fresh Card')
    sid = _statement_id(conn, cid)
    rid = _add_receipt(conn, cid, 'fresh', ai_status='pending')
    assert rid in _claimable_ids(sid)


def test_a_stalled_processing_lease_is_still_reclaimed(conn):
    """The pre-existing crash-recovery path must survive the retry-gate rewrite."""
    cid = _make_card('Pytest CC AI Stalled Card')
    sid = _statement_id(conn, cid)
    rid = _add_receipt(conn, cid, 'stalled', ai_status='pending')
    conn.execute("UPDATE cc_receipts SET ai_status='processing', "
                 "ai_processed_at=datetime('now', '-90 minutes') WHERE id=?", (rid,))
    conn.commit()
    assert rid in _claimable_ids(sid)


def test_a_live_processing_lease_is_left_alone(conn):
    cid = _make_card('Pytest CC AI Live Lease Card')
    sid = _statement_id(conn, cid)
    rid = _add_receipt(conn, cid, 'live', ai_status='pending')
    conn.execute("UPDATE cc_receipts SET ai_status='processing', "
                 "ai_processed_at=datetime('now') WHERE id=?", (rid,))
    conn.commit()
    assert rid not in _claimable_ids(sid)


# ── Merchant memory reuse, end to end through the DB ─────────────────────────

def test_a_new_branch_inherits_the_brand_decision(conn):
    """'FUELSTOP MAIN ROAD' was stored as a 3-token key that no other Fuelstop could
    ever match. The brand-level family lookup is what closes that gap."""
    db.upsert_cc_merchant_map('FUELSTOP MAIN', '6230', 'Motor Vehicle Expenses')
    family = db.get_cc_merchant_map_family(cc_ai.merchant_brand('FUELSTOP CONVENIENCE 41'))
    code, name, how = cc_ai.resolve_remembered_account(None, family)
    assert (code, how) == ('6230', 'brand')


def test_the_family_lookup_does_not_bleed_across_brands(conn):
    db.upsert_cc_merchant_map('AUTOSTOP KINGSFORD', '6230', 'Motor Vehicle Expenses')
    keys = {r['merchant_key'] for r in db.get_cc_merchant_map_family('FUELSTOP')}
    assert 'AUTOSTOP KINGSFORD' not in keys


def test_an_empty_brand_matches_nothing(conn):
    assert db.get_cc_merchant_map_family('') == []
    assert db.get_cc_merchant_map_family(None) == []


# ── The worker: memory first, then ONE batched AI call ───────────────────────

def _fake_batch(monkeypatch, code='6400', calls=None):
    """Replace the batched coding call with a recorder, so the worker's
    orchestration is testable independently of what does the coding."""
    from northwind.cards import ai as _ai

    def _stub(items, **kwargs):
        if calls is not None:
            calls.append(list(items))
        return ([_ai.AccountSuggestion(code, 'Local Travel - Other', 'high', False,
                                       'stub') for _ in items], None)

    monkeypatch.setattr(_ai, 'suggest_accounts_batch', _stub)
    monkeypatch.setattr(_ai, 'FEATURE_ENABLED', True)


def test_worker_codes_uncoded_lines_in_one_batched_call(conn, monkeypatch):
    """249 lines used to mean 249 sequential coding calls. They now go out
    CODING_BATCH_SIZE at a time, which is most of why the pass felt slow."""
    from workers import process_cc_receipts as worker

    cid = _make_card('Pytest CC AI Worker Batch Card')
    sid = _statement_id(conn, cid)
    calls = []
    _fake_batch(monkeypatch, calls=calls)

    coded = worker.suggest_accounts(limit=50, statement_id=sid)
    assert coded == 2
    assert len(calls) == 1                # both lines in a single call
    assert len(calls[0]) == 2
    for lid in _ids(conn, cid):
        row = db.get_cc_line(lid)
        assert row['ai_account_code'] == '6400'
        assert row['ai_source'] == 'ai'
        assert row['coding_dirty'] == 0


def test_worker_passes_receipt_detail_into_the_batch(conn, monkeypatch):
    """The regression that mattered most: the coding call used to be handed
    'Receipt text: (none)' even when the receipt had already been extracted."""
    from workers import process_cc_receipts as worker

    cid = _make_card('Pytest CC AI Worker Detail Card')
    sid = _statement_id(conn, cid)
    line_id = _ids(conn, cid)[0]
    rid = _add_receipt(conn, cid, 'workerdetail',
                       raw=json.dumps({'summary': 'office furniture',
                                       'line_items': ['Office chair']}))
    db.link_cc_receipt(rid, line_id)

    calls = []
    _fake_batch(monkeypatch, calls=calls)
    worker.suggest_accounts(limit=50, statement_id=sid)

    sent = {i['reference']: i.get('receipt_text') for i in calls[0]}
    assert 'office furniture' in (sent['SHOPFRONT'] or '')
    assert 'Office chair' in (sent['SHOPFRONT'] or '')
    assert sent['DL RIDECO WST'] is None        # no receipt attached to that one


def test_worker_prefers_merchant_memory_over_the_ai(conn, monkeypatch):
    from workers import process_cc_receipts as worker

    cid = _make_card('Pytest CC AI Worker Memory Card')
    sid = _statement_id(conn, cid)
    db.upsert_cc_merchant_map(cc_ai.normalize_merchant('SHOPFRONT'), '6270',
                              'Small Assets - Expense')
    calls = []
    _fake_batch(monkeypatch, calls=calls)
    worker.suggest_accounts(limit=50, statement_id=sid)

    shopfront, rideco = (db.get_cc_line(i) for i in _ids(conn, cid))
    assert (shopfront['ai_account_code'], shopfront['ai_source']) == ('6270', 'memory')
    assert (rideco['ai_account_code'], rideco['ai_source']) == ('6400', 'ai')
    # Only the un-remembered line was sent to the model.
    assert [i['reference'] for i in calls[0]] == ['DL RIDECO WST']


def test_worker_requeues_lines_the_model_skipped(conn, monkeypatch):
    """A dropped or failed line must stay dirty for the next run, never be left
    silently uncoded or coded from another line's answer."""
    from workers import process_cc_receipts as worker
    from northwind.cards import ai as _ai

    cid = _make_card('Pytest CC AI Worker Requeue Card')
    sid = _statement_id(conn, cid)
    monkeypatch.setattr(_ai, 'FEATURE_ENABLED', True)
    monkeypatch.setattr(_ai, 'suggest_accounts_batch',
                        lambda items, **kw: ([None] * len(items), 'quota_exhausted'))

    assert worker.suggest_accounts(limit=50, statement_id=sid) == 0
    for lid in _ids(conn, cid):
        row = db.get_cc_line(lid)
        assert row['ai_account_code'] is None
        assert row['coding_dirty'] == 1        # picked up again next run
        assert row['coding_status'] is None    # lease released


def test_worker_leaves_lines_for_the_cron_when_ai_is_deferred(conn, monkeypatch):
    """The in-request "Match now" button passes use_ai=False to stay fast."""
    from workers import process_cc_receipts as worker

    cid = _make_card('Pytest CC AI Worker Deferred Card')
    sid = _statement_id(conn, cid)
    calls = []
    _fake_batch(monkeypatch, calls=calls)

    assert worker.suggest_accounts(limit=50, statement_id=sid, use_ai=False) == 0
    assert calls == []
    for lid in _ids(conn, cid):
        assert db.get_cc_line(lid)['coding_dirty'] == 1


def test_worker_stops_the_pass_on_an_exhausted_quota(conn, monkeypatch):
    """Batch 1 of 13 hitting a quota wall means batches 2..13 will too. Firing them
    anyway is exactly the kind of wasted work that made the pass feel slow."""
    from workers import process_cc_receipts as worker
    from northwind.cards import ai as _ai

    cid = _make_card('Pytest CC AI Worker Quota Card')
    sid = _statement_id(conn, cid)
    calls = []

    def _stub(items, **kwargs):
        calls.append(list(items))
        return ([None] * len(items), 'quota_exhausted')

    monkeypatch.setattr(_ai, 'FEATURE_ENABLED', True)
    monkeypatch.setattr(_ai, 'suggest_accounts_batch', _stub)
    monkeypatch.setattr(_ai, 'CODING_BATCH_SIZE', 1)   # force two batches

    assert worker.suggest_accounts(limit=50, statement_id=sid) == 0
    assert len(calls) == 1              # second batch never attempted
    for lid in _ids(conn, cid):
        row = db.get_cc_line(lid)
        assert row['coding_dirty'] == 1
        assert row['coding_status'] is None


def test_worker_keeps_going_after_a_one_off_bad_batch(conn, monkeypatch):
    """A garbled response is about that batch's content, not the key or the quota,
    so the remaining batches must still be attempted."""
    from workers import process_cc_receipts as worker
    from northwind.cards import ai as _ai

    cid = _make_card('Pytest CC AI Worker Bad Batch Card')
    sid = _statement_id(conn, cid)
    calls = []

    def _stub(items, **kwargs):
        calls.append(list(items))
        if len(calls) == 1:
            return ([None] * len(items), 'bad_model_response')
        return ([_ai.AccountSuggestion('6400', 'Local Travel - Other', 'high', False,
                                       'ok') for _ in items], None)

    monkeypatch.setattr(_ai, 'FEATURE_ENABLED', True)
    monkeypatch.setattr(_ai, 'suggest_accounts_batch', _stub)
    monkeypatch.setattr(_ai, 'CODING_BATCH_SIZE', 1)

    assert worker.suggest_accounts(limit=50, statement_id=sid) == 1
    assert len(calls) == 2


# ── Human-triggered retries must never be silently refused ───────────────────

def test_match_now_retries_a_receipt_inside_the_backoff(conn):
    """The backoff exists to stop the hourly cron wasting calls. It must not
    overrule an admin who just clicked "Match now" — being told "nothing waiting"
    while the receipt is visibly sitting there is the worst possible answer."""
    cid = _make_card('Pytest CC AI Force Backoff Card')
    sid = _statement_id(conn, cid)
    rid = _add_receipt(conn, cid, 'forceback', ai_status='pending')
    db.set_cc_receipt_ai_status(rid, 'failed', error='timeout')

    assert rid not in _claimable_ids(sid)                      # cron holds back
    forced = db.claim_cc_receipts_pending_ai(limit=50, statement_id=sid, force=True)
    assert rid in {r['id'] for r in forced}                    # the human wins


def test_match_now_retries_a_terminally_failed_receipt(conn):
    """A cardholder's iPhone upload the API could not decode would otherwise be
    permanently unretryable — no button anywhere could ever pick it up again."""
    cid = _make_card('Pytest CC AI Force Terminal Card')
    sid = _statement_id(conn, cid)
    rid = _add_receipt(conn, cid, 'forceterm', ai_status='pending')
    db.set_cc_receipt_ai_status(rid, 'failed', error='unsupported_media')

    assert rid not in _claimable_ids(sid)
    forced = db.claim_cc_receipts_pending_ai(limit=50, statement_id=sid, force=True)
    assert rid in {r['id'] for r in forced}


def test_forcing_still_respects_a_live_processing_lease(conn):
    """force is about retry gates, not about trampling a worker mid-request."""
    cid = _make_card('Pytest CC AI Force Lease Card')
    sid = _statement_id(conn, cid)
    rid = _add_receipt(conn, cid, 'forcelease', ai_status='pending')
    conn.execute("UPDATE cc_receipts SET ai_status='processing', "
                 "ai_processed_at=datetime('now') WHERE id=?", (rid,))
    conn.commit()
    forced = db.claim_cc_receipts_pending_ai(limit=50, statement_id=sid, force=True)
    assert rid not in {r['id'] for r in forced}


def test_the_match_now_route_forces_a_retry(client, conn, monkeypatch):
    """End to end through the admin button, since that is the only escape hatch a
    human has for a receipt the cron has given up on."""
    from workers import process_cc_receipts as worker
    from northwind.cards import ai as _ai

    cid = _make_card('Pytest CC AI Match Now Route Card')
    sid = _statement_id(conn, cid)
    rid = _add_receipt(conn, cid, 'routeforce', ai_status='pending')
    db.set_cc_receipt_ai_status(rid, 'failed', error='unsupported_media')

    seen = {}
    monkeypatch.setattr(_ai, 'FEATURE_ENABLED', True)
    monkeypatch.setattr(worker, 'run',
                        lambda **kw: seen.update(kw) or 0)
    client.post(f'/cards/{cid}/match-now', data={'statement_id': sid})
    assert seen.get('force') is True


# ── One odd line must not cost the rest of its batch ─────────────────────────

def test_a_failed_batch_is_retried_line_by_line(conn, monkeypatch):
    """Batching means a garbled response takes 19 innocent lines down with it.
    Isolating the retry keeps the failure with the line that caused it."""
    from workers import process_cc_receipts as worker
    from northwind.cards import ai as _ai

    cid = _make_card('Pytest CC AI Split Retry Card')
    sid = _statement_id(conn, cid)
    calls = []

    def _stub(items, **kwargs):
        calls.append([i['reference'] for i in items])
        if len(items) > 1:
            return ([None] * len(items), 'bad_model_response')   # whole batch fails
        if items[0]['reference'] == 'SHOPFRONT':
            return ([None], 'bad_model_response')                # the culprit
        return ([_ai.AccountSuggestion('6400', 'Local Travel - Other', 'high', False,
                                       'ok')], None)

    monkeypatch.setattr(_ai, 'FEATURE_ENABLED', True)
    monkeypatch.setattr(_ai, 'suggest_accounts_batch', _stub)

    coded = worker.suggest_accounts(limit=50, statement_id=sid)
    assert coded == 1                            # the innocent line still got coded
    assert calls[0] == ['SHOPFRONT', 'DL RIDECO WST']
    assert ['DL RIDECO WST'] in calls[1:]           # retried on its own
    rideco = [db.get_cc_line(i) for i in _ids(conn, cid)][1]
    assert rideco['ai_account_code'] == '6400'
    shopfront = [db.get_cc_line(i) for i in _ids(conn, cid)][0]
    assert shopfront['coding_dirty'] == 1          # culprit requeued, not mis-coded


def test_a_fatal_error_does_not_trigger_line_by_line_retries(conn, monkeypatch):
    """An exhausted quota fails every single-line retry too — splitting would turn
    one wasted call into twenty."""
    from workers import process_cc_receipts as worker
    from northwind.cards import ai as _ai

    cid = _make_card('Pytest CC AI No Split On Quota Card')
    sid = _statement_id(conn, cid)
    calls = []

    def _stub(items, **kwargs):
        calls.append(list(items))
        return ([None] * len(items), 'quota_exhausted')

    monkeypatch.setattr(_ai, 'FEATURE_ENABLED', True)
    monkeypatch.setattr(_ai, 'suggest_accounts_batch', _stub)

    worker.suggest_accounts(limit=50, statement_id=sid)
    assert len(calls) == 1


# ── A total we already distrust must not be auto-linked ──────────────────────

def _run_matching(monkeypatch, extract, sid):
    """Drive _extract_and_match with extraction stubbed to one known result."""
    from workers import process_cc_receipts as worker
    from northwind.cards import ai as _ai
    from northwind.services import storage

    monkeypatch.setattr(_ai, 'FEATURE_ENABLED', True)
    monkeypatch.setattr(storage, 'read', lambda path: b'\x00')
    monkeypatch.setattr(worker.storage, 'read', lambda path: b'\x00')
    monkeypatch.setattr(_ai, 'extract_receipt_with_error',
                        lambda data, mime, **kw: (extract, None))
    return worker._extract_and_match(50, sid)


def _extract_for(total_cents, disputed):
    return cc_ai.ReceiptExtract(
        vendor='SHOPFRONT', date=dt.date(2026, 5, 10), total_cents=total_cents,
        currency='ZAR', confidence=0.9, raw_json='{}', total_disputed=disputed)


def test_a_clean_receipt_is_auto_linked(conn, monkeypatch):
    cid = _make_card('Pytest CC AI Autolink Clean Card')
    sid = _statement_id(conn, cid)
    rid = _add_receipt(conn, cid, 'clean', ai_status='pending')
    assert _run_matching(monkeypatch, _extract_for(95700, False), sid) == 1
    linked = conn.execute("SELECT COUNT(*) n FROM cc_receipt_lines WHERE receipt_id=?",
                          (rid,)).fetchone()['n']
    assert linked == 1


def test_a_receipt_whose_total_does_not_add_up_is_only_suggested(conn, monkeypatch):
    """Same receipt, same unambiguous match — but the total contradicts the slip's
    own VAT breakdown, so a silent link would risk attaching it to the wrong charge."""
    cid = _make_card('Pytest CC AI Autolink Disputed Card')
    sid = _statement_id(conn, cid)
    rid = _add_receipt(conn, cid, 'disputed', ai_status='pending')
    assert _run_matching(monkeypatch, _extract_for(95700, True), sid) == 1
    linked = conn.execute("SELECT COUNT(*) n FROM cc_receipt_lines WHERE receipt_id=?",
                          (rid,)).fetchone()['n']
    suggested = conn.execute(
        "SELECT COUNT(*) n FROM cc_line_receipt_suggestions WHERE receipt_id=?",
        (rid,)).fetchone()['n']
    assert linked == 0          # not auto-linked
    assert suggested >= 1       # but still surfaced for a human to confirm


# ── Guardrails found by auditing real matching runs ──────────────────────

def test_a_zero_total_receipt_is_recorded_as_unreadable(conn, monkeypatch):
    """Marking it 'processed' both fed it into matching and hid it from the portal's
    "could not be read" count, so nobody knew to re-upload a clearer photo."""
    cid = _make_card('Pytest CC AI Zero Total Card')
    sid = _statement_id(conn, cid)
    rid = _add_receipt(conn, cid, 'zerototal', ai_status='pending')
    _run_matching(monkeypatch, _extract_for(0, False), sid)
    assert conn.execute("SELECT ai_status FROM cc_receipts WHERE id=?",
                        (rid,)).fetchone()['ai_status'] == 'unreadable'
    assert conn.execute("SELECT COUNT(*) n FROM cc_receipt_lines WHERE receipt_id=?",
                        (rid,)).fetchone()['n'] == 0


def test_an_already_linked_receipt_is_never_auto_linked_again(conn, monkeypatch):
    """"Match now" force-retries failed receipts, so a slip the AI could not read and
    an admin then linked by hand comes back through the matcher. It must not quietly
    claim to cover a second charge nobody assigned it to."""
    cid = _make_card('Pytest CC AI Already Linked Card')
    sid = _statement_id(conn, cid)
    shopfront, rideco = _ids(conn, cid)
    rid = _add_receipt(conn, cid, 'alreadylinked', ai_status='pending')
    db.link_cc_receipt(rid, rideco, actor='admin')      # the human's placement

    _run_matching(monkeypatch, _extract_for(95700, False), sid)

    linked = [r['line_id'] for r in conn.execute(
        "SELECT line_id FROM cc_receipt_lines WHERE receipt_id=?", (rid,))]
    assert linked == [rideco]                          # unchanged
    # …but the candidate is still offered, so a genuine multi-charge invoice is one
    # click away rather than invisible.
    assert conn.execute(
        "SELECT COUNT(*) n FROM cc_line_receipt_suggestions WHERE receipt_id=?",
        (rid,)).fetchone()['n'] >= 1


def test_a_reconciled_line_is_never_sent_to_the_coding_ai(conn):
    """Closed in Xero — re-coding spends a coding pass to change a suggestion on a row
    nobody is looking at. set_cc_line_reason raises coding_dirty without checking, so
    the guard has to live at the claim."""
    cid = _make_card('Pytest CC AI Reconciled Coding Card')
    sid = _statement_id(conn, cid)
    line_id = _ids(conn, cid)[0]
    conn.execute("UPDATE cc_lines SET xero_reconciled=1, coding_dirty=1 WHERE id=?",
                 (line_id,))
    conn.commit()
    claimed = {r['id'] for r in db.claim_cc_lines_needing_coding(50, statement_id=sid)}
    assert line_id not in claimed


def test_the_matcher_flags_cannot_be_null(conn):
    """The candidate pool used to filter on a bare `personal=0`, which is FALSE for
    NULL — one un-defaulted row would have dropped out of matching entirely and its
    receipt could never match anything. The queries now COALESCE, but the real
    protection is the schema: these columns are NOT NULL with a 0 default. This test
    exists so a future migration that relaxes either one fails here rather than
    silently making receipts unmatchable."""
    cols = {r['name']: r for r in conn.execute("PRAGMA table_info(cc_lines)")}
    for name in ('personal', 'xero_reconciled'):
        assert cols[name]['notnull'] == 1, f"{name} lost its NOT NULL"
        assert str(cols[name]['dflt_value']) == '0', f"{name} lost its 0 default"


def test_a_personal_line_is_still_excluded_from_matching(conn):
    """The guardrail that must survive the COALESCE change: personal charges are repaid
    by the cardholder and must never attract a receipt link or an account code."""
    cid = _make_card('Pytest CC AI Personal Excluded Card')
    sid = _statement_id(conn, cid)
    line_id = _ids(conn, cid)[0]
    db.set_cc_line_personal(line_id, True)
    ids = {r['id'] for r in db.get_cc_spend_lines_for_matching(sid)}
    assert line_id not in ids
    claimed = {r['id'] for r in db.claim_cc_lines_needing_coding(50, statement_id=sid)}
    assert line_id not in claimed
