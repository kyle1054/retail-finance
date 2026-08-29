"""Record WHY an AI extraction or coding failed, not just that it did.

``extract_receipt`` and ``suggest_account`` both ended in a bare
``except Exception: return None`` with no logging and nowhere to record a reason. The
caller then wrote a flat ``ai_status='failed'``. So a missing API key, an exhausted
quota, a network timeout, a malformed model response, an unsupported media type and a
genuinely unreadable slip were all one indistinguishable state — which meant the only
way to diagnose "the AI isn't working" was to probe production by hand and guess.

``ai_error`` holds a short, stable classification (see ``northwind.cards.ai.classify_ai_error``:
``no_api_key``, ``quota_exhausted``, ``auth_invalid_key``, ``timeout``,
``service_unavailable``, ``network``, ``unsupported_media``, ``blocked_by_safety``,
``bad_model_response``, ``empty_model_response``, ``sdk_missing``, ``unknown:<Type>``).
Short and stable on purpose: the useful question is "how many receipts failed for the
same reason", which needs grouping, not prose.

Two columns because they are two different calls that fail independently:
  * ``cc_receipts.ai_error``      — extraction (reading the slip)
  * ``cc_lines.ai_coding_error``  — coding (choosing the Xero account)

Nullable, not backfilled: existing 'failed' rows genuinely have no recorded reason and
inventing one would be worse than an honest NULL. They will be re-attempted by the
worker ('failed' is transient) and stamped then.

Idempotent.
"""


def up(conn):
    receipts = {row['name'] for row in conn.execute("PRAGMA table_info(cc_receipts)")}
    if 'ai_error' not in receipts:
        conn.execute("ALTER TABLE cc_receipts ADD COLUMN ai_error TEXT")

    lines = {row['name'] for row in conn.execute("PRAGMA table_info(cc_lines)")}
    if 'ai_coding_error' not in lines:
        conn.execute("ALTER TABLE cc_lines ADD COLUMN ai_coding_error TEXT")

    # "What is failing, and how often" is the question this exists to answer, and it is
    # a grouped scan over a column that is NULL for almost every row.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_receipts_ai_error "
                 "ON cc_receipts(ai_error) WHERE ai_error IS NOT NULL")
