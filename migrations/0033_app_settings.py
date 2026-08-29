"""A small key/value settings store for app-level config that isn't per-row.

First use: the consolidated cash-sales manual-journal export (the single contra
account it credits, and its narration). CREATE ... IF NOT EXISTS because some
environments already have this table from an earlier out-of-band migration —
this makes the schema self-contained without clobbering existing data.
"""


def up(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_settings ("
        "  key TEXT PRIMARY KEY,"
        "  value TEXT"
        ")")
