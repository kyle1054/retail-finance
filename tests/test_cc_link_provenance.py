"""Receipt<->charge link provenance, the AI-coded review query, and error classification.

These back the connector's new CC write path. The properties worth pinning:

* a link records WHO made it, so an agent's link is distinguishable from a human's —
  the question that was unanswerable before migration 0043;
* link/unlink report whether they actually changed anything, because an agent claiming
  to have cut a link it never found is worse than one that says nothing;
* the review query surfaces charges the AI coded *confidently*, which no existing query
  did — that blind spot is why a batch of groceries sat miscoded as travel.
"""
import pytest

from northwind.data import database as db
from northwind.cards import ai as cc_ai


# --------------------------------------------------------------------------- #
# Link provenance (migration 0043)
# --------------------------------------------------------------------------- #
def _a_receipt_and_line(conn):
    """A receipt and a spend line on the SAME card, not already linked."""
    row = conn.execute(
        "SELECT r.id AS receipt_id, l.id AS line_id FROM cc_receipts r "
        "  JOIN cc_lines l ON l.card_id = r.card_id AND l.category='spend' "
        " WHERE NOT EXISTS (SELECT 1 FROM cc_receipt_lines rl "
        "                    WHERE rl.receipt_id=r.id AND rl.line_id=l.id) "
        " LIMIT 1").fetchone()
    if row is None:
        pytest.skip("no unlinked receipt/line pair on a shared card in the test DB")
    return row["receipt_id"], row["line_id"]


def test_link_stamps_the_actor_and_a_timestamp(conn, db_copy):
    receipt_id, line_id = _a_receipt_and_line(conn)
    try:
        created = db.link_cc_receipt(receipt_id, line_id, actor="mcp:claude")
        row = conn.execute(
            "SELECT actor, linked_at FROM cc_receipt_lines "
            "WHERE receipt_id=? AND line_id=?", (receipt_id, line_id)).fetchone()
        assert created is True
        assert row["actor"] == "mcp:claude"
        assert row["linked_at"], "linked_at must be stamped"
    finally:
        db.unlink_cc_receipt(receipt_id, line_id)


def test_relinking_reports_no_change(conn, db_copy):
    """`INSERT OR IGNORE` makes a re-link silent; the caller still has to be able to
    tell that nothing happened."""
    receipt_id, line_id = _a_receipt_and_line(conn)
    try:
        assert db.link_cc_receipt(receipt_id, line_id, actor="mcp:claude") is True
        assert db.link_cc_receipt(receipt_id, line_id, actor="mcp:claude") is False
    finally:
        db.unlink_cc_receipt(receipt_id, line_id)


def test_unlink_reports_whether_it_removed_anything(conn, db_copy):
    receipt_id, line_id = _a_receipt_and_line(conn)
    db.link_cc_receipt(receipt_id, line_id, actor="mcp:claude")
    assert db.unlink_cc_receipt(receipt_id, line_id) is True
    # Second call has nothing to remove and must say so rather than claiming success.
    assert db.unlink_cc_receipt(receipt_id, line_id) is False


def test_cc_receipt_line_exists(conn, db_copy):
    receipt_id, line_id = _a_receipt_and_line(conn)
    assert db.cc_receipt_line_exists(receipt_id, line_id) is False
    try:
        db.link_cc_receipt(receipt_id, line_id, actor="pytest")
        assert db.cc_receipt_line_exists(receipt_id, line_id) is True
    finally:
        db.unlink_cc_receipt(receipt_id, line_id)


def test_auto_link_stamps_ai_by_default(conn, db_copy):
    """The worker's auto-match must be distinguishable from a human's link."""
    row = conn.execute(
        "SELECT r.id AS receipt_id, l.id AS line_id FROM cc_receipts r "
        "  JOIN cc_lines l ON l.card_id = r.card_id AND l.category='spend' "
        " WHERE NOT EXISTS (SELECT 1 FROM cc_receipt_lines rl WHERE rl.line_id=l.id) "
        " LIMIT 1").fetchone()
    if row is None:
        pytest.skip("no uncovered line available")
    try:
        assert db.auto_link_cc_receipt_if_uncovered(
            row["receipt_id"], row["line_id"]) is True
        actor = conn.execute(
            "SELECT actor FROM cc_receipt_lines WHERE receipt_id=? AND line_id=?",
            (row["receipt_id"], row["line_id"])).fetchone()["actor"]
        assert actor == "ai"
    finally:
        db.unlink_cc_receipt(row["receipt_id"], row["line_id"])


def test_link_provenance_query_returns_the_actor(conn, db_copy):
    receipt_id, line_id = _a_receipt_and_line(conn)
    statement_id = conn.execute(
        "SELECT statement_id FROM cc_receipts WHERE id=?", (receipt_id,)).fetchone()[0]
    if statement_id is None:
        pytest.skip("receipt is not assigned to a statement")
    try:
        db.link_cc_receipt(receipt_id, line_id, actor="mcp:claude")
        rows = db.get_cc_receipt_link_provenance(statement_id, conn=conn)
        mine = [r for r in rows if r["receipt_id"] == receipt_id
                and r["line_id"] == line_id]
        assert mine and mine[0]["actor"] == "mcp:claude"
    finally:
        db.unlink_cc_receipt(receipt_id, line_id)


# --------------------------------------------------------------------------- #
# The AI-coded review query
# --------------------------------------------------------------------------- #
def test_ai_coded_query_returns_only_coded_lines(conn, db_copy):
    rows = db.get_cc_lines_ai_coded(limit=50, conn=conn)
    if not rows:
        pytest.skip("no AI-coded lines in the test DB")
    for r in rows:
        assert r["ai_account_code"] is not None
        assert r["personal"] == 0


def test_ai_coded_query_includes_already_confirmed_lines(conn, db_copy):
    """The key difference from the to-do list: a charge whose code a human already
    accepted still needs to be reviewable, because the AI's suggestion may have been
    accepted in bulk and been wrong."""
    confirmed = conn.execute(
        "SELECT COUNT(*) c FROM cc_lines WHERE ai_account_code IS NOT NULL "
        "AND xero_account_code IS NOT NULL AND category='spend' AND personal=0"
    ).fetchone()["c"]
    if not confirmed:
        pytest.skip("no confirmed AI-coded lines in the test DB")
    rows = db.get_cc_lines_ai_coded(limit=500, conn=conn)
    assert any(r["xero_account_code"] for r in rows)


def test_ai_coded_query_excludes_uncoded_lines(conn, db_copy):
    """Sanity that the two queries really are complements, not overlapping views."""
    coded = {r["id"] for r in db.get_cc_lines_ai_coded(limit=500, conn=conn)}
    uncoded = {r["id"] for r in db.get_cc_lines_missing_coding(limit=500, conn=conn)}
    # A line can be in both only if it has an AI suggestion but no confirmed code —
    # which is legitimate. What must NOT happen is a line with no AI code appearing
    # in the review list.
    for line_id in coded:
        row = conn.execute("SELECT ai_account_code FROM cc_lines WHERE id=?",
                           (line_id,)).fetchone()
        assert row["ai_account_code"] is not None


def test_ai_coded_query_filters_by_confidence(conn, db_copy):
    rows = db.get_cc_lines_ai_coded(confidence="high", limit=50, conn=conn)
    for r in rows:
        assert (r["ai_confidence"] or "").lower() == "high"


def test_ai_coded_query_orders_least_trustworthy_first(conn, db_copy):
    rows = db.get_cc_lines_ai_coded(limit=200, conn=conn)
    if len(rows) < 2:
        pytest.skip("not enough AI-coded lines to check ordering")
    rank = {"low": 0, "medium": 1, "high": 2}
    seen = [(0 if r["ai_needs_review"] else 1,
             rank.get((r["ai_confidence"] or "").lower(), 3)) for r in rows]
    assert seen == sorted(seen), "needs_review first, then low confidence first"


def test_ai_coded_query_scopes_to_a_card(conn, db_copy):
    any_row = conn.execute(
        "SELECT card_id FROM cc_lines WHERE ai_account_code IS NOT NULL LIMIT 1"
    ).fetchone()
    if any_row is None:
        pytest.skip("no AI-coded lines")
    card_id = any_row["card_id"]
    rows = db.get_cc_lines_ai_coded(card_id=card_id, limit=500, conn=conn)
    assert rows and all(r["card_id"] == card_id for r in rows)


# --------------------------------------------------------------------------- #
# AI failure classification (migration 0044)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("message,expected", [
    ("429 RESOURCE_EXHAUSTED: quota exceeded", "quota_exhausted"),
    ("You exceeded your current quota", "quota_exhausted"),
    ("Rate limit reached for model", "quota_exhausted"),
    ("API key not valid. Please pass a valid API key.", "auth_invalid_key"),
    ("403 PERMISSION_DENIED", "auth_denied"),
    ("Request timed out", "timeout"),
    ("Deadline exceeded", "timeout"),
    ("503 Service Unavailable", "service_unavailable"),
    ("Connection reset by peer", "network"),
    ("Unsupported mime type: image/heic", "unsupported_media"),
    ("Response blocked by safety filters", "blocked_by_safety"),
    ("Malformed JSON in response", "bad_model_response"),
])
def test_classify_ai_error_distinguishes_real_failures(message, expected):
    assert cc_ai.classify_ai_error(RuntimeError(message)) == expected


def test_classify_ai_error_uses_the_exception_type_when_the_message_is_opaque():
    """A JSONDecodeError reads 'Expecting value: line 1 column 1' — nothing in that
    text says it is a malformed model response, so the type has to carry it."""
    import json as _json
    try:
        _json.loads("")
    except Exception as exc:
        assert cc_ai.classify_ai_error(exc) == "bad_model_response"
    assert cc_ai.classify_ai_error(TimeoutError("")) == "timeout"
    assert cc_ai.classify_ai_error(ConnectionError("")) == "network"


def test_classify_ai_error_prefers_the_type_over_a_misleading_message():
    """A timeout whose message happens to mention a 500 is still a timeout."""
    assert cc_ai.classify_ai_error(TimeoutError("after 500 attempts")) == "timeout"


def test_classify_ai_error_keeps_the_type_for_unknowns():
    """An unrecognised failure still beats the previous silence."""
    class WeirdError(Exception):
        pass
    assert cc_ai.classify_ai_error(WeirdError("???")) == "unknown:WeirdError"


def test_extract_receipt_for_an_unrecorded_slip_reports_why():
    extract, error = cc_ai.extract_receipt_with_error(b"x", "image/png")
    assert extract is None and error == "no_recorded_extraction"


def test_extract_receipt_wrapper_still_returns_just_the_value():
    """The old single-value signature must keep working for existing callers."""
    assert cc_ai.extract_receipt(b"x", "image/png") is None


def test_suggest_account_codes_from_the_cardholders_own_words():
    """What the cardholder typed describes what was bought, so it outranks the
    bank's abbreviation of the merchant — and is trusted enough not to need
    review."""
    suggestion, error = cc_ai.suggest_account_with_error(
        "GREENFIELDS", -25000, "milk and coffee for the office")
    assert error is None
    assert suggestion.account_code == "6330"        # Staff Amenities
    assert suggestion.needs_review is False


def test_status_write_records_and_then_clears_the_error(conn, db_copy):
    """A receipt that failed once and later processed must not keep a stale reason."""
    rid = conn.execute("SELECT id FROM cc_receipts LIMIT 1").fetchone()
    if rid is None:
        pytest.skip("no receipts in the test DB")
    rid = rid["id"]

    db.set_cc_receipt_ai_status(rid, "failed", error="quota_exhausted")
    assert conn.execute("SELECT ai_error FROM cc_receipts WHERE id=?",
                        (rid,)).fetchone()["ai_error"] == "quota_exhausted"

    db.set_cc_receipt_ai(rid, "Vendor", "2026-07-01", 12345, "{}", "processed")
    assert conn.execute("SELECT ai_error FROM cc_receipts WHERE id=?",
                        (rid,)).fetchone()["ai_error"] is None


def test_error_tally_groups_by_reason(conn, db_copy):
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM cc_receipts LIMIT 3").fetchall()]
    if len(ids) < 3:
        pytest.skip("need at least 3 receipts")
    db.set_cc_receipt_ai_status(ids[0], "failed", error="quota_exhausted")
    db.set_cc_receipt_ai_status(ids[1], "failed", error="quota_exhausted")
    db.set_cc_receipt_ai_status(ids[2], "failed", error="timeout")

    tally = db.get_cc_ai_error_tally(conn=conn)
    assert tally.get("quota_exhausted") == 2
    assert tally.get("timeout") == 1
