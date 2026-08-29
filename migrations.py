"""Lightweight, dependency-free schema migration runner.

Why: the historical schema lives in database.init_db()/migrate_db() (idempotent
bootstrap, left untouched). For every change *from here on* we use ordered,
recorded, transactional migrations instead of the old "ALTER ... try/except: pass"
soup — so we always know exactly what has been applied to a given database.

Migration files live in ./migrations/ and are named `NNNN_description.sql` or
`NNNN_description.py`:
  - .sql : executed as a script.
  - .py  : must define `up(conn)`; use this when a migration needs Python logic
           or data transformation (e.g. converting money columns to cents).

Each migration runs inside a transaction and is recorded in schema_migrations;
already-applied versions are skipped. Run via `python migrations.py` or it runs
automatically on app start.
"""
import os
import glob
import importlib.util
from datetime import datetime

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), 'migrations')


def _ensure_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )''')


def applied_versions(conn):
    _ensure_table(conn)
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}


def discover():
    """Return [(version, filename, path)] sorted by version."""
    paths = glob.glob(os.path.join(MIGRATIONS_DIR, '[0-9]*.sql')) + \
            glob.glob(os.path.join(MIGRATIONS_DIR, '[0-9]*.py'))
    found = []
    for p in paths:
        base = os.path.basename(p)
        try:
            version = int(base.split('_', 1)[0])
        except ValueError:
            continue
        found.append((version, base, p))
    found.sort(key=lambda t: t[0])
    return found


def _split_sql_statements(sql):
    """Split a SQL script into individual statements.

    Splits on ';' while respecting single/double-quoted string literals,
    line (--) and block (/* */) comments, and BEGIN...END blocks (so the
    inner statements of a trigger body are kept with their CREATE TRIGGER).
    Good enough for the hand-written DDL migrations in ./migrations/.
    """
    statements = []
    buf = []
    i, n = 0, len(sql)
    begin_depth = 0
    in_squote = in_dquote = False
    in_line_comment = in_block_comment = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ''
        if in_line_comment:
            buf.append(ch)
            if ch == '\n':
                in_line_comment = False
        elif in_block_comment:
            buf.append(ch)
            if ch == '*' and nxt == '/':
                buf.append(nxt); i += 1
                in_block_comment = False
        elif in_squote:
            buf.append(ch)
            if ch == "'":
                in_squote = False
        elif in_dquote:
            buf.append(ch)
            if ch == '"':
                in_dquote = False
        elif ch == '-' and nxt == '-':
            buf.append(ch); buf.append(nxt); i += 1
            in_line_comment = True
        elif ch == '/' and nxt == '*':
            buf.append(ch); buf.append(nxt); i += 1
            in_block_comment = True
        elif ch == "'":
            buf.append(ch); in_squote = True
        elif ch == '"':
            buf.append(ch); in_dquote = True
        else:
            # Track BEGIN/END so a trigger body's semicolons don't split it.
            upper_tail = ''.join(buf[-5:]).upper() + ch.upper()
            if upper_tail.endswith('BEGIN') and (i + 1 >= n or not sql[i + 1].isalnum()):
                begin_depth += 1
            elif upper_tail.endswith('END') and (i + 1 >= n or not sql[i + 1].isalnum()):
                if begin_depth > 0:
                    begin_depth -= 1
            if ch == ';' and begin_depth == 0:
                stmt = ''.join(buf).strip()
                if stmt:
                    statements.append(stmt)
                buf = []
                i += 1
                continue
            buf.append(ch)
        i += 1
    tail = ''.join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _apply_one(conn, base, path):
    if path.endswith('.sql'):
        with open(path) as fh:
            sql = fh.read()
        # Execute each statement on the existing connection so the whole file
        # stays inside this migration's transaction. Unlike conn.executescript(),
        # this does NOT implicitly commit, so a mid-file failure rolls back the
        # entire migration instead of leaving it half-applied.
        for stmt in _split_sql_statements(sql):
            conn.execute(stmt)
    else:  # .py with an up(conn) function
        spec = importlib.util.spec_from_file_location(base[:-3], path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, 'up'):
            raise RuntimeError(f"{base} has no up(conn) function")
        mod.up(conn)


def run_migrations(conn=None, verbose=False):
    """Apply any pending migrations. Returns the list of names applied."""
    from northwind.data import database as db
    own = conn is None
    if own:
        conn = db.get_db()
    applied = []
    try:
        done = applied_versions(conn)
        for version, base, path in discover():
            if version in done:
                continue
            try:
                _apply_one(conn, base, path)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, base, datetime.now().isoformat()))
                conn.commit()
                applied.append(base)
                if verbose:
                    print(f"  applied {base}")
            except Exception as e:
                conn.rollback()
                raise RuntimeError(f"Migration {base} failed: {e}") from e
    finally:
        if own:
            conn.close()
    return applied


if __name__ == '__main__':
    names = run_migrations(verbose=True)
    print(f"{len(names)} migration(s) applied." if names else "Already up to date.")
