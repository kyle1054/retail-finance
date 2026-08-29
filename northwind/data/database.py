import sqlite3
import os
import re
from collections import defaultdict
from datetime import date
from northwind.services import money
from northwind.services import scrub


def canonical_name(s):
    """Order- and punctuation-insensitive key for a person's name.

    Lower-cases, drops punctuation (commas, hyphens, etc.) and sorts the
    name tokens, so 'Smith, John', 'Smith John' and 'John Smith' all
    collapse to the same key 'john smith'. Used by payroll matching and
    duplicate detection so a missing comma or a swapped first/last name
    doesn't hide a real match."""
    return ' '.join(sorted(re.findall(r'[a-z0-9]+', (s or '').lower())))


def layby_schedule_issues(conn=None):
    """Active lay-bys whose numbers don't reconcile — surfaced for review so a
    mis-set monthly/term or a stale balance (e.g. after an ad-hoc payment or a
    changed payment structure) can be spotted and corrected. Flags when the
    agreed schedule (monthly × term) doesn't match the total, or when the stored
    balance disagrees with the scheduled remaining (term − payments × monthly)."""
    own = conn is None
    if own:
        conn = get_db()
    try:
        rows = conn.execute('''
            SELECT l.id, e.full_name, e.current_store,
                   l.monthly_amount_cents AS m, l.term_months AS term,
                   l.payments_made AS paid, l.total_amount_cents AS total,
                   l.balance_remaining_cents AS bal
            FROM layby_deductions_cents l JOIN employees e ON e.id = l.employee_id
            WHERE l.status = 'active'
        ''').fetchall()
    finally:
        if own:
            conn.close()
    issues = []
    for r in rows:
        m, term, total, paid = r['m'] or 0, r['term'] or 0, r['total'] or 0, r['paid'] or 0
        problems = []
        if m and term and abs(m * term - total) > 100:   # off by more than R1
            problems.append(f"monthly × term = R{m * term / 100:,.2f}, but total = R{total / 100:,.2f}")
        if r['bal'] is not None and m and term:
            sched = max(0, term - paid) * m
            if abs(sched - r['bal']) > 100:
                problems.append(f"balance R{r['bal'] / 100:,.2f} ≠ scheduled remaining R{sched / 100:,.2f}")
        if problems:
            issues.append({'id': r['id'], 'full_name': r['full_name'],
                           'current_store': r['current_store'], 'monthly': m / 100,
                           'term': term, 'payments_made': paid, 'problems': problems})
    issues.sort(key=lambda i: i['full_name'].lower())
    return issues


def employee_link_count(emp_id, conn=None):
    """How many deduction/allowance records point at this employee — used to
    show, when merging duplicates, which record actually holds the data."""
    own = conn is None
    if own:
        conn = get_db()
    try:
        n = 0
        for table in ('uniform_deductions_cents', 'layby_deductions_cents',
                      'undercharges_cents', 'overpayments_cents', 'allowance_purchases'):
            n += conn.execute(f"SELECT COUNT(*) FROM {table} WHERE employee_id=?", (emp_id,)).fetchone()[0]
        return n
    finally:
        if own:
            conn.close()


def merge_employees(keep_id, remove_ids, conn=None):
    """Fold one or more duplicate employees into `keep_id`, then delete them.

    Every employee-linked row (plans, transactions, undercharges, overpayments,
    allowances/purchases) is reassigned to the keeper. Per-person rows that
    can't sensibly coexist — the staff-portal login and store history — are
    dropped from the duplicate (the keeper keeps its own). Runs in one
    transaction with FK enforcement, so it either fully merges or rolls back;
    returns a dict of how many rows moved per table."""
    if isinstance(remove_ids, str):
        remove_ids = [remove_ids]
    remove_ids = [r for r in dict.fromkeys(remove_ids) if r and r != keep_id]
    if not remove_ids:
        raise ValueError("No distinct duplicate to merge in.")

    own = conn is None
    if own:
        conn = get_db()
    try:
        with conn:
            if not conn.execute("SELECT 1 FROM employees WHERE id=?", (keep_id,)).fetchone():
                raise ValueError(f"Employee to keep ({keep_id}) not found.")
            moved = defaultdict(int)
            reassign = ('uniform_deductions_cents', 'layby_deductions_cents',
                        'undercharges_cents', 'deduction_transactions_cents',
                        'overpayments_cents', 'allowance_purchases')
            for rid in remove_ids:
                if not conn.execute("SELECT 1 FROM employees WHERE id=?", (rid,)).fetchone():
                    raise ValueError(f"Employee to merge ({rid}) not found.")
                for table in reassign:
                    cur = conn.execute(
                        f"UPDATE {table} SET employee_id=? WHERE employee_id=?", (keep_id, rid))
                    moved[table] += cur.rowcount
                # Allowances carry UNIQUE(employee_id, year): move only the years
                # the keeper lacks, then drop any colliding leftovers.
                cur = conn.execute(
                    "UPDATE allowances SET employee_id=? WHERE employee_id=? AND year NOT IN "
                    "(SELECT year FROM allowances WHERE employee_id=?)", (keep_id, rid, keep_id))
                moved['allowances'] += cur.rowcount
                conn.execute("DELETE FROM allowances WHERE employee_id=?", (rid,))
                # Hand the staff login to the keeper only if it has none of its own.
                if conn.execute("SELECT 1 FROM employee_logins WHERE employee_id=?", (keep_id,)).fetchone():
                    conn.execute("DELETE FROM employee_logins WHERE employee_id=?", (rid,))
                else:
                    conn.execute("UPDATE employee_logins SET employee_id=? WHERE employee_id=?", (keep_id, rid))
                conn.execute("DELETE FROM store_history WHERE employee_id=?", (rid,))
                conn.execute("DELETE FROM employees WHERE id=?", (rid,))
        return dict(moved)
    finally:
        if own:
            conn.close()


def find_duplicate_employees(conn=None):
    """Groups of employees whose names collapse to the same canonical key.

    Returns a list of groups (each a list of employee dict rows) for every
    canonical name shared by two or more employees — catches exact dupes
    plus swapped/comma-less variants. Ordered with active rows first."""
    own = conn is None
    if own:
        conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, full_name, current_store, job_title, status, sector "
            "FROM employees"
        ).fetchall()
    finally:
        if own:
            conn.close()
    groups = defaultdict(list)
    for r in rows:
        groups[canonical_name(r['full_name'])].append(dict(r))
    out = [
        sorted(emps, key=lambda e: (e['status'] != 'active', e['full_name']))
        for key, emps in groups.items() if key and len(emps) > 1
    ]
    out.sort(key=lambda g: g[0]['full_name'].lower())
    return out

# Default lives under db/ at the REPO ROOT. This module now lives at
# northwind/data/database.py (three levels down), so anchor the default to the repo
# root — NOT the module dir — so the package move keeps the same deductions.db.
# Set NW_DB_PATH (e.g. a path on a mounted volume) on hosts with
# ephemeral filesystems so data survives deploys.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.environ.get('NW_DB_PATH') or os.path.join(_REPO_ROOT, 'db', 'deductions.db')

BALANCE_EPSILON = 0.01


# Money conversion helpers, re-exported for write sites (rands <-> integer cents).
to_cents = money.to_cents
to_rands = money.to_rands


def calc_uniform_balance(plan):
    """Remaining balance on a uniform deduction plan (delegates to money.py)."""
    cents = money.uniform_balance_cents(
        plan['total_amount'], plan['monthly_amount'], plan['term_months'],
        plan['payments_made'], plan['balance_remaining'])
    return money.to_rands(cents)


def calc_installment_amount(total_amount, monthly_amount, term_months, installment_index):
    """Installment amount for a specific installment index (delegates to money.py)."""
    return money.installment_amount(total_amount, monthly_amount, term_months, installment_index)

STORES_DEFAULT = sorted([
    'Riverbend', 'Westgate', 'Kingsway', 'Ashford', 'Brookfield',
    'Fairview', 'Mill Street', 'Crossroads', 'Lakeside', 'Northgate',
    'Elmwood', 'Sunfield', 'Ravine', 'Vineyard', 'Grand Central Mall',
    'Highland Mall', 'Parkview Mall', 'Riverside', 'Eastvale',
    'Stonebridge - Fairhaven', 'The Atrium', 'Rosewood', 'Summit',
    'Somerton Mall', 'Oakvale', 'Harbour Point', 'Wellstone'
])

MONTH_NAMES = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

MONTH_FULL = ['', 'January', 'February', 'March', 'April', 'May', 'June',
              'July', 'August', 'September', 'October', 'November', 'December']


def validate_month_year(year, month):
    """Validate a (year, month) pair parsed from user input (URLs, forms).

    Returns (year, month) as ints when valid, or None when out of range, so
    callers can flash a friendly error instead of crashing on e.g.
    MONTH_NAMES[13]. Month must be 1-12; year a sane 2020-2100.
    """
    try:
        year = int(year)
        month = int(month)
    except (TypeError, ValueError):
        return None
    if not (1 <= month <= 12):
        return None
    if not (2020 <= year <= 2100):
        return None
    return (year, month)


def month_name(month, full=False):
    """Safe MONTH_NAMES/MONTH_FULL lookup — returns '' for out-of-range months
    instead of raising IndexError."""
    table = MONTH_FULL if full else MONTH_NAMES
    try:
        month = int(month)
    except (TypeError, ValueError):
        return ''
    return table[month] if 1 <= month <= 12 else ''


# WAL is the best journal mode on a real local disk (a mounted volume), and it's the
# default. It relies on shared memory, so on a network filesystem set
# NW_SQLITE_JOURNAL=DELETE instead. Values outside the allowlist fall back to
# WAL rather than reaching the PRAGMA (env vars aren't trusted SQL).
_ALLOWED_JOURNAL_MODES = {'DELETE', 'TRUNCATE', 'PERSIST', 'MEMORY', 'WAL'}
JOURNAL_MODE = os.environ.get('NW_SQLITE_JOURNAL', 'WAL').upper()
if JOURNAL_MODE not in _ALLOWED_JOURNAL_MODES:
    JOURNAL_MODE = 'WAL'


def _connect():
    """Open a brand-new connection with this app's pragmas applied."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA journal_mode = {JOURNAL_MODE}")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -32000")
    # Wait up to 5s for a competing writer instead of instantly raising
    # "database is locked" — SQLite serialises writes, so this is essential
    # once more than one request can be in flight at a time.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class _RequestConnection:
    """A handle on the request's shared connection whose ``close()`` is a no-op.

    Nearly every call site is written as ``conn = db.get_db()`` … ``conn.close()``
    in a ``finally``. Those closes were written when each caller owned its own
    connection. Once one connection is shared by a whole request, the FIRST
    caller to finish would close it out from under everyone still to run in that
    request, and every later query would raise
    "Cannot operate on a closed database" — a 500 on a page that worked before.
    So the shared connection is only ever handed out behind this proxy: the real
    close happens once, in ``close_request_conns`` at teardown, after every
    caller is done. Everything else delegates to the real connection unchanged.
    """
    __slots__ = ('_conn',)

    def __init__(self, conn):
        object.__setattr__(self, '_conn', conn)

    def close(self):
        # Deliberately nothing — see the class docstring.
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        # e.g. a caller setting row_factory must reach the real connection.
        setattr(self._conn, name, value)

    def __enter__(self):
        # sqlite3's context manager commits/rolls back but never closes, so
        # `with conn:` keeps its meaning. It must yield the PROXY, not the raw
        # connection, or `with db.get_db() as c: ... c.close()` would bypass the
        # no-op and close the shared connection early.
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)

    def __repr__(self):
        return f'<_RequestConnection {self._conn!r}>'


def _request_g():
    """Flask's ``g`` when an app context is active, else None (scripts, workers)."""
    try:
        from flask import g, has_app_context
    except ImportError:
        return None
    if not has_app_context():
        return None
    return g


def get_db():
    """A SQLite connection with this app's pragmas applied.

    Inside a Flask app/request context the SAME connection is reused for the
    whole request, cached on ``g``. Opening one is not free: /admin used to open
    8 connections and pay 5 PRAGMAs on each before rendering a single row.
    Outside an app context — scripts/, workers/, migrations at boot, and the MCP
    server (its own ASGI app, no Flask context) — every call still returns a
    fresh, private connection that the caller owns and must close itself.
    """
    g = _request_g()
    if g is None:
        return _connect()

    shared = getattr(g, '_db_shared', None)
    if shared is None:
        shared = _connect()
        g._db_shared = shared
        _track_for_teardown(shared)
        return _RequestConnection(shared)

    if shared.in_transaction:
        # Someone up the stack is mid-write. Sharing the connection here would
        # mean this caller's commit() makes their half-finished write durable,
        # and its rollback() throws their work away — on a payroll tick or an
        # undercharge reschedule that is a partially applied money change.
        # Hand out a private connection so the two transactions stay
        # independent, exactly as they were before the sharing existed.
        conn = _connect()
        _track_for_teardown(conn)
        return conn

    return _RequestConnection(shared)


def _track_for_teardown(conn):
    # Inside a Flask request, register the connection so it is guaranteed to be
    # closed at request end even if a handler raises before its explicit close().
    # Outside an app context (CLI import scripts, init), this is a no-op and the
    # caller remains responsible for closing.
    try:
        from flask import g, has_app_context
    except ImportError:
        return
    if not has_app_context():
        return
    conns = getattr(g, '_db_conns', None)
    if conns is None:
        conns = []
        g._db_conns = conns
    conns.append(conn)


def close_request_conns(exception=None):
    """Flask teardown handler: close any connections opened during the request.

    This is where the request's shared connection is actually closed (its
    handles' close() is a no-op), plus any private connection handed out while a
    write was in flight. Double-closing is safe in sqlite3, so the explicit
    conn.close() calls in route handlers continue to work.
    """
    try:
        from flask import g
    except ImportError:
        return
    for conn in getattr(g, '_db_conns', []):
        try:
            conn.close()
        except Exception:
            pass
    # Don't leave a closed connection reachable if this context is reused.
    if hasattr(g, '_db_shared'):
        del g._db_shared
    if hasattr(g, '_db_conns'):
        del g._db_conns


def init_db():
    # A custom NW_DB_PATH may point inside a freshly mounted volume.
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS employees (
            id TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            current_store TEXT,
            job_title TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now')),
            terminated_at TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS store_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT NOT NULL,
            store TEXT NOT NULL,
            from_date TEXT NOT NULL,
            to_date TEXT
        );

        CREATE TABLE IF NOT EXISTS uniform_deductions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT NOT NULL,
            sku TEXT,
            description TEXT,
            sale_number TEXT,
            total_amount REAL,
            monthly_amount REAL NOT NULL,
            term_months INTEGER NOT NULL,
            start_month INTEGER NOT NULL,
            start_year INTEGER NOT NULL,
            payments_made INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now')),
            notes TEXT,
            end_date TEXT,
            balance_remaining REAL
        );

        CREATE TABLE IF NOT EXISTS layby_deductions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT NOT NULL,
            sale_number TEXT DEFAULT '',
            description TEXT,
            basket_total REAL DEFAULT 0,
            discount_pct REAL DEFAULT 40,
            total_amount REAL,
            monthly_amount REAL NOT NULL,
            balance_remaining REAL DEFAULT 0,
            term_months INTEGER NOT NULL,
            start_month INTEGER NOT NULL,
            start_year INTEGER NOT NULL,
            payments_made INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now')),
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS layby_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            layby_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            unit_price REAL NOT NULL,
            quantity INTEGER DEFAULT 1,
            line_total REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS plan_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_type TEXT NOT NULL,
            plan_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            new_monthly REAL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS undercharges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT NOT NULL,
            sale_number TEXT,
            total_amount REAL NOT NULL,
            reason TEXT,
            incident_month INTEGER,
            incident_year INTEGER,
            start_month INTEGER,
            start_year INTEGER,
            recovery_method TEXT DEFAULT 'full',
            split_months INTEGER DEFAULT 1,
            payments_made INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            notes TEXT,
            type TEXT DEFAULT 'undercharge',
            reimburse_month INTEGER,
            reimburse_year INTEGER
        );

        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS store_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS employee_logins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT NOT NULL UNIQUE,
            login_code TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );
    ''')
    conn.commit()
    conn.close()


_stores_cache = None

def get_stores():
    global _stores_cache
    if _stores_cache is None:
        conn = get_db()
        try:
            rows = conn.execute("SELECT name FROM stores ORDER BY name").fetchall()
        finally:
            conn.close()
        _stores_cache = [r['name'] for r in rows]
    return _stores_cache

def invalidate_stores_cache():
    global _stores_cache
    _stores_cache = None


def migrate_db():
    """Safe migration — adds new columns/tables without touching existing data."""
    conn = get_db()
    migrations = [
        "ALTER TABLE layby_deductions ADD COLUMN sale_number TEXT DEFAULT ''",
        "ALTER TABLE layby_deductions ADD COLUMN basket_total REAL DEFAULT 0",
        "ALTER TABLE layby_deductions ADD COLUMN discount_pct REAL DEFAULT 40",
        "ALTER TABLE layby_deductions ADD COLUMN balance_remaining REAL DEFAULT 0",
        "ALTER TABLE employees ADD COLUMN notes TEXT",
        "ALTER TABLE uniform_deductions ADD COLUMN end_date TEXT",
        "ALTER TABLE uniform_deductions ADD COLUMN balance_remaining REAL",
        "ALTER TABLE undercharges ADD COLUMN type TEXT DEFAULT 'undercharge'",
        "ALTER TABLE undercharges ADD COLUMN reimburse_month INTEGER",
        "ALTER TABLE undercharges ADD COLUMN reimburse_year INTEGER",
        "ALTER TABLE undercharges ADD COLUMN start_month INTEGER",
        "ALTER TABLE undercharges ADD COLUMN start_year INTEGER",
        # Sector discriminator: 'retail' (default, all existing staff) or 'hq'.
        # Keeps the Retail and HQ sections as fully separate entities.
        "ALTER TABLE employees ADD COLUMN sector TEXT DEFAULT 'retail'",
        # Admin access tier: 'super' (everything — the default, so every
        # existing admin keeps full access), 'retail' (retail deductions only)
        # or 'hq' (HQ deductions only). Enforced in core.require_login().
        "ALTER TABLE admin_users ADD COLUMN role TEXT DEFAULT 'super'",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except Exception:
            pass  # column already exists

    # Backfill start_month/year for legacy undercharges (skip once it is a view).
    if conn.execute("SELECT type FROM sqlite_master WHERE name='undercharges'").fetchone()[0] == 'table':
        conn.execute('''
            UPDATE undercharges
            SET start_month = incident_month,
                start_year = incident_year
            WHERE start_month IS NULL OR start_year IS NULL
        ''')

    conn.executescript('''
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
    ''')
    if conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 0:
        for s in STORES_DEFAULT:
            conn.execute("INSERT OR IGNORE INTO stores (name) VALUES (?)", (s,))

    conn.executescript('''
        CREATE TABLE IF NOT EXISTS overpayments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT,
            store TEXT,
            individual_name TEXT,
            sale_number TEXT,
            total_amount REAL NOT NULL,
            reason TEXT,
            incident_month INTEGER,
            incident_year INTEGER,
            status TEXT DEFAULT 'pending',
            balance_remaining REAL DEFAULT 0,
            corrected_on TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS layby_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            layby_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            unit_price REAL NOT NULL,
            quantity INTEGER DEFAULT 1,
            line_total REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plan_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_type TEXT NOT NULL,
            plan_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            new_monthly REAL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS deduction_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_type TEXT NOT NULL,
            plan_id INTEGER NOT NULL,
            employee_id TEXT NOT NULL,
            amount REAL NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            voided INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS locked_periods (
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            locked_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (year, month)
        );
    ''')

    # Make payroll locks sector-aware: rebuild once so the primary key includes
    # sector, letting Retail and HQ lock the same calendar month independently.
    lp_cols = [r[1] for r in conn.execute("PRAGMA table_info(locked_periods)").fetchall()]
    if lp_cols and 'sector' not in lp_cols:
        # Rebuild to add `sector` to the primary key. Run as individual
        # statements (not executescript, which would auto-commit and could leave
        # the table half-rebuilt if it failed) and verify every row copied across
        # BEFORE dropping the original, so the lock data can never be lost.
        conn.execute("DROP TABLE IF EXISTS locked_periods_new")
        conn.execute('''
            CREATE TABLE locked_periods_new (
                sector TEXT NOT NULL DEFAULT 'retail',
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                locked_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (sector, year, month)
            )''')
        conn.execute('''
            INSERT INTO locked_periods_new (sector, year, month, locked_at)
                SELECT 'retail', year, month, locked_at FROM locked_periods''')
        old_count = conn.execute("SELECT COUNT(*) FROM locked_periods").fetchone()[0]
        new_count = conn.execute("SELECT COUNT(*) FROM locked_periods_new").fetchone()[0]
        if new_count != old_count:
            raise RuntimeError(
                f"locked_periods rebuild copied {new_count}/{old_count} rows; "
                "aborting before drop to protect the lock data.")
        conn.execute("DROP TABLE locked_periods")
        conn.execute("ALTER TABLE locked_periods_new RENAME TO locked_periods")

    # One-time balance_remaining backfills for legacy rows. These run only while
    # the table is still a physical table; once a *_cents migration has turned it
    # into a view, the cents base table is authoritative and these are skipped.
    def _is_table(name):
        r = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
        return bool(r) and r[0] == 'table'

    if _is_table('layby_deductions'):
        conn.execute('''
            UPDATE layby_deductions
            SET balance_remaining = total_amount - (payments_made * monthly_amount)
            WHERE balance_remaining = 0 AND total_amount > 0
        ''')

    if _is_table('uniform_deductions'):
        conn.execute('''
            UPDATE uniform_deductions
            SET balance_remaining = COALESCE(total_amount, term_months * monthly_amount) - (payments_made * ROUND(monthly_amount, 2))
            WHERE balance_remaining IS NULL
        ''')

    # Backfill store_history for employees imported before history tracking was added
    conn.execute('''
        INSERT INTO store_history (employee_id, store, from_date)
        SELECT e.id, e.current_store, COALESCE(e.created_at, datetime('now'))
        FROM employees e
        WHERE NOT EXISTS (
            SELECT 1 FROM store_history sh WHERE sh.employee_id = e.id
        )
    ''')

    # Indexes on never-migrated tables (always physical tables).
    conn.executescript('''
        CREATE INDEX IF NOT EXISTS idx_employees_store ON employees(current_store);
        CREATE INDEX IF NOT EXISTS idx_employees_status ON employees(status);
        CREATE INDEX IF NOT EXISTS idx_employees_sector ON employees(sector);
        CREATE INDEX IF NOT EXISTS idx_store_history_employee_id ON store_history(employee_id);''')

    # Indexes on the deduction tables — only while they are still physical tables.
    # Once a *_cents migration turns them into views, the migration creates the
    # equivalent indexes on the base table (and "views may not be indexed").
    _deduction_indexes = [
        ("uniform_deductions", "CREATE INDEX IF NOT EXISTS idx_uniform_deductions_employee_id ON uniform_deductions(employee_id)"),
        ("uniform_deductions", "CREATE INDEX IF NOT EXISTS idx_uniform_deductions_status ON uniform_deductions(status)"),
        ("uniform_deductions", "CREATE INDEX IF NOT EXISTS idx_uniform_month_range ON uniform_deductions(start_year * 12 + start_month)"),
        ("layby_deductions", "CREATE INDEX IF NOT EXISTS idx_layby_deductions_employee_id ON layby_deductions(employee_id)"),
        ("layby_deductions", "CREATE INDEX IF NOT EXISTS idx_layby_deductions_status ON layby_deductions(status)"),
        ("layby_deductions", "CREATE INDEX IF NOT EXISTS idx_layby_month_range ON layby_deductions(start_year * 12 + start_month)"),
        ("layby_items", "CREATE INDEX IF NOT EXISTS idx_layby_items_layby_id ON layby_items(layby_id)"),
        ("undercharges", "CREATE INDEX IF NOT EXISTS idx_undercharges_employee_id ON undercharges(employee_id)"),
        ("undercharges", "CREATE INDEX IF NOT EXISTS idx_undercharges_status_type ON undercharges(status, type)"),
        ("undercharges", "CREATE INDEX IF NOT EXISTS idx_undercharges_month_range ON undercharges(incident_year * 12 + incident_month)"),
        ("deduction_transactions", "CREATE INDEX IF NOT EXISTS idx_deduction_transactions_plan ON deduction_transactions(plan_type, plan_id)"),
        ("deduction_transactions", "CREATE INDEX IF NOT EXISTS idx_deduction_transactions_date ON deduction_transactions(year, month)"),
        ("deduction_transactions", "CREATE UNIQUE INDEX IF NOT EXISTS idx_prevent_duplicate_payments ON deduction_transactions(plan_type, plan_id, year, month) WHERE voided = 0"),
    ]
    for tbl, sql in _deduction_indexes:
        if _is_table(tbl):
            conn.execute(sql)

    conn.commit()
    conn.close()


def next_employee_id(conn):
    row = conn.execute(
        "SELECT COALESCE(MAX(CAST(SUBSTR(id, 5) AS INTEGER)), 0) AS max_n FROM employees WHERE id LIKE 'EMP-%'"
    ).fetchone()
    return f"EMP-{(row['max_n'] + 1):04d}"


def _outstanding_by_employee(conn, sector=None, emp_id=None, store=None):
    """Single source of truth for per-employee outstanding balances.

    Returns {employee_id: {'uniform': R, 'layby': R, 'undercharges': R}} (rands,
    2dp) for active uniform/lay-by plans and pending/partial undercharges,
    computed via money.py so every read path (employee page, dashboard, top
    debtors) reports identical figures. Filter by sector, store, or one employee.
    """
    emp_filter, params = "", []
    if emp_id is not None:
        emp_filter = "employee_id = ?"
        params = [emp_id]
    elif sector is not None:
        emp_filter = "employee_id IN (SELECT id FROM employees WHERE sector = ?)"
        params = [sector]
    elif store is not None:
        emp_filter = "employee_id IN (SELECT id FROM employees WHERE current_store = ?)"
        params = [store]
    where = f"AND {emp_filter}" if emp_filter else ""

    totals = defaultdict(lambda: {'uniform': 0, 'layby': 0, 'undercharges': 0})

    rows = conn.execute(f'''
        SELECT employee_id, total_amount, monthly_amount, term_months, payments_made, balance_remaining
        FROM uniform_deductions WHERE status = 'active' {where}''', params).fetchall()
    for r in rows:
        totals[r['employee_id']]['uniform'] += money.uniform_balance_cents(
            r['total_amount'], r['monthly_amount'], r['term_months'],
            r['payments_made'], r['balance_remaining'])

    rows = conn.execute(f'''
        SELECT employee_id, total_amount, monthly_amount, term_months, payments_made, balance_remaining
        FROM layby_deductions WHERE status = 'active' {where}''', params).fetchall()
    for r in rows:
        totals[r['employee_id']]['layby'] += money.layby_balance_cents(
            r['total_amount'], r['monthly_amount'], r['term_months'],
            r['payments_made'], r['balance_remaining'])

    rows = conn.execute(f'''
        SELECT id, employee_id
        FROM undercharges
        WHERE (type IS NULL OR type = 'undercharge')
        {where}''', params).fetchall()
    # Batched: the per-row get_undercharge_account() here ran 5 queries each, and
    # the dashboard calls this three times over every undercharge in the company.
    accounts = get_undercharge_accounts([r['id'] for r in rows], conn)
    for r in rows:
        totals[r['employee_id']]['undercharges'] += accounts[r['id']]['remaining_cents']

    return {eid: {k: money.to_rands(v) for k, v in t.items()} for eid, t in totals.items()}


def xl_safe(val):
    """Excel-safe cell value for exports.

    openpyxl stores any string starting with '=' as a live formula, so a
    crafted description/name entered in the app (or arriving via an import)
    would execute as a formula on whoever opens the export. Strings starting
    with +, -, @, tab, CR, or LF can also trigger formula execution in Excel.
    Prefix with an apostrophe so Excel shows it as plain text.
    """
    if isinstance(val, str) and val and val[0] in ('=', '+', '-', '@', '\t', '\r', '\n'):
        return "'" + val
    return val


def get_store_portal_data(store):
    """Active employees at a store (staff portal name picker).

    Deliberately returns no amounts: the store page is shared by everyone at
    the store, so balances stay off it — each person sees their own figures
    only after tapping through to their profile.
    """
    conn = get_db()
    try:
        emps = conn.execute(
            "SELECT id, full_name, job_title FROM employees "
            "WHERE current_store = ? AND status = 'active' ORDER BY full_name",
            (store,)).fetchall()
    finally:
        conn.close()
    return [{'id': e['id'], 'full_name': e['full_name'],
             'job_title': e['job_title']} for e in emps]


def get_outstanding_totals():
    """Outstanding total (rands) per employee in one pass: {employee_id: R}.

    Use this instead of calling get_outstanding_summary() in a loop — one
    connection and three queries for the whole company rather than per person.
    """
    conn = get_db()
    try:
        totals = _outstanding_by_employee(conn)
    finally:
        conn.close()
    return {eid: round(t['uniform'] + t['layby'] + t['undercharges'], 2)
            for eid, t in totals.items()}


def get_outstanding_summary(emp_id):
    conn = get_db()
    try:
        t = _outstanding_by_employee(conn, emp_id=emp_id).get(
            emp_id, {'uniform': 0.0, 'layby': 0.0, 'undercharges': 0.0})
        t['total'] = round(t['uniform'] + t['layby'] + t['undercharges'], 2)
        return t
    finally:
        conn.close()


def get_category_totals(emp_id):
    conn = get_db()
    try:
        # Custom calculation for uniform_deductions
        u_rows = conn.execute(
            "SELECT total_amount, term_months, monthly_amount, payments_made, status, balance_remaining FROM uniform_deductions WHERE employee_id=?",
            (emp_id,)
        ).fetchall()
        
        u_charged = 0.0
        u_paid = 0.0
        u_remaining = 0.0
        
        for r in u_rows:
            total_amt = r['total_amount'] if r['total_amount'] is not None else round(r['term_months'] * r['monthly_amount'], 2)
            bal_rem = calc_uniform_balance(r)
            
            if r['status'] != 'written_off':
                u_charged += total_amt
                
            u_paid += round(total_amt - bal_rem, 2)
            
            if r['status'] == 'active':
                u_remaining += bal_rem
                
        u = {'charged': round(u_charged, 2), 'paid': round(u_paid, 2), 'remaining': round(u_remaining, 2)}

        lb_rows = conn.execute(
            "SELECT total_amount, balance_remaining, status FROM layby_deductions WHERE employee_id=?", (emp_id,)
        ).fetchall()
        lb_charged   = sum(r['total_amount'] or 0 for r in lb_rows if r['status'] != 'written_off')
        lb_paid      = sum((r['total_amount'] or 0) - (r['balance_remaining'] or 0) for r in lb_rows)
        lb_remaining = sum(r['balance_remaining'] or 0 for r in lb_rows if r['status'] == 'active')
        l = {'charged': round(lb_charged, 2), 'paid': round(lb_paid, 2), 'remaining': round(lb_remaining, 2)}

        ucs = conn.execute(
            "SELECT id FROM undercharges WHERE employee_id=? "
            "AND (type IS NULL OR type='undercharge')", (emp_id,)).fetchall()
        accounts = list(get_undercharge_accounts([r['id'] for r in ucs], conn).values())
        uc_charged = sum(a['adjusted_total_cents'] - a['written_off_cents'] for a in accounts)
        uc_paid = sum(max(a['net_employee_paid_cents'], 0) for a in accounts)
        uc_remaining = sum(a['remaining_cents'] for a in accounts)
        uc = {'charged': money.to_rands(uc_charged),
              'paid': money.to_rands(uc_paid),
              'remaining': money.to_rands(uc_remaining)}

        ovs = conn.execute(
            "SELECT total_amount, status FROM undercharges WHERE employee_id=? AND type='overcharge'",
            (emp_id,)
        ).fetchall()
        ov_total   = sum(r['total_amount'] for r in ovs if r['status'] != 'written_off')
        ov_pending = sum(r['total_amount'] for r in ovs if r['status'] == 'pending')
        ov = {'total': round(ov_total, 2), 'pending': round(ov_pending, 2), 'count': len(ovs)}

        return {'uniform': u, 'layby': l, 'undercharges': uc, 'overcharges': ov}
    finally:
        conn.close()


def get_employee_schedule(emp_id):
    """Month-by-month deduction schedule for one employee."""
    conn = get_db()
    try:
        uniforms    = conn.execute("SELECT * FROM uniform_deductions WHERE employee_id=? ORDER BY start_year, start_month", (emp_id,)).fetchall()
        laybys      = conn.execute("SELECT * FROM layby_deductions WHERE employee_id=? ORDER BY start_year, start_month", (emp_id,)).fetchall()
        undercharges = conn.execute("SELECT * FROM undercharges WHERE employee_id=? ORDER BY incident_year, incident_month", (emp_id,)).fetchall()
        ensure_undercharge_schedules(conn, [uc['id'] for uc in undercharges])
        uc_items = conn.execute(
            "SELECT i.due_year,i.due_month,i.amount_cents,i.transaction_id,"
            "t.voided FROM undercharge_schedule_items i "
            "JOIN undercharges_cents u ON u.id=i.undercharge_id "
            "LEFT JOIN deduction_transactions_cents t ON t.id=i.transaction_id "
            "WHERE u.employee_id=? AND i.state='scheduled' "
            "ORDER BY i.due_year,i.due_month,i.id", (emp_id,)).fetchall()
    finally:
        conn.close()

    sched = defaultdict(lambda: {
        'uniform': 0.0, 'layby': 0.0, 'undercharges': 0.0,
        'uniform_paid': 0.0, 'layby_paid': 0.0, 'undercharges_paid': 0.0
    })

    def nth_month(start_m, start_y, n):
        m = start_m + n
        return (start_y + (m - 1) // 12, ((m - 1) % 12) + 1)

    for p in uniforms:
        if p['status'] == 'written_off':
            continue
        term = p['term_months']
        for i in range(term):
            y, m = nth_month(p['start_month'], p['start_year'], i)
            installment = calc_installment_amount(p['total_amount'], p['monthly_amount'], p['term_months'], i)
            sched[(y, m)]['uniform'] += installment
            if i < p['payments_made']:
                sched[(y, m)]['uniform_paid'] += installment

    for p in laybys:
        if p['status'] == 'written_off':
            continue
        # Cents allocation: final month absorbs the remainder so the schedule
        # sums exactly to the entered total (fixes export reconciliation).
        for i in range(p['term_months']):
            y, m = nth_month(p['start_month'], p['start_year'], i)
            installment = calc_installment_amount(p['total_amount'], p['monthly_amount'], p['term_months'], i)
            sched[(y, m)]['layby'] += installment
            if i < p['payments_made']:
                sched[(y, m)]['layby_paid'] += installment

    for item in uc_items:
        amount = money.to_rands(item['amount_cents'])
        sched[(item['due_year'], item['due_month'])]['undercharges'] += amount
        if item['transaction_id'] and not item['voided']:
            sched[(item['due_year'], item['due_month'])]['undercharges_paid'] += amount

    result = []
    for (y, m) in sorted(sched.keys()):
        d = dict(sched[(y, m)])
        d['year'], d['month'] = y, m
        d['total']      = round(d['uniform'] + d['layby'] + d['undercharges'], 2)
        d['total_paid'] = round(d['uniform_paid'] + d['layby_paid'] + d['undercharges_paid'], 2)
        d['uniform']         = round(d['uniform'], 2)
        d['layby']           = round(d['layby'], 2)
        d['undercharges']    = round(d['undercharges'], 2)
        result.append(d)

    return result


# ── Undercharge financial timeline + versioned schedules ────────────────────


def _uc_add_month(year, month, offset):
    idx = int(year) * 12 + int(month) - 1 + int(offset)
    return idx // 12, idx % 12 + 1


def _uc_split_cents(total_cents, count):
    """Split an exact cent total; the final installment absorbs the remainder."""
    total_cents, count = abs(int(total_cents)), int(count)
    if total_cents <= 0 or count <= 0:
        raise ValueError("Schedule amount and installment count must be positive.")
    if count > total_cents:
        raise ValueError("Installment count cannot exceed the number of cents owed.")
    base, remainder = divmod(total_cents, count)
    values = [base] * count
    values[-1] += remainder
    return values


def ensure_undercharge_schedule(conn, undercharge_id):
    """Create the original explicit schedule for a legacy/directly-inserted row.

    Migration 0036 backfills production data and normal route inserts create a
    schedule immediately.  This lazy guard keeps fixtures, imports and any old
    operational scripts safe without making their callers know about the new
    tables.

    This is a WRITE reached from read paths (the dashboard and every list that
    prices an undercharge), so it commits its own repair when — and only when —
    the caller was not already mid-transaction. Both halves of that matter:

      • Committing when we opened the transaction is what stops /admin
        deadlocking against itself. A request shares one connection now, and its
        close() is a no-op until teardown, so an uncommitted INSERT here holds
        SQLite's RESERVED lock for the WHOLE request. The next get_db() sees
        in_transaction and hands out a private connection (correctly — see
        _request_g), which then blocks on the lock its own request is holding,
        waits out busy_timeout and 500s with "database is locked". It also fixes
        a bug that predates the sharing: the repair was rolled back at teardown
        and never persisted, so the same row was re-repaired on every request.

      • NOT committing when the caller was already mid-transaction is what keeps
        a write route's half-finished money change from being made durable by a
        pricing call nested inside it. Those callers own their transaction and
        commit it themselves, exactly as before.
    """
    existing = conn.execute(
        "SELECT 1 FROM undercharge_schedule_revisions WHERE undercharge_id=? LIMIT 1",
        (undercharge_id,)).fetchone()
    if existing:
        return
    plan = conn.execute(
        "SELECT * FROM undercharges_cents WHERE id=?", (undercharge_id,)).fetchone()
    if not plan or (plan['type'] or 'undercharge') != 'undercharge':
        return
    sy = plan['start_year'] or plan['incident_year']
    sm = plan['start_month'] or plan['incident_month']
    if not sy or not sm:
        return
    count = 1 if plan['recovery_method'] == 'full' else max(int(plan['split_months'] or 1), 1)
    terminal = plan['status'] in (
        'recovered', 'written_off', 'accounted_for', 'paid_by_customer', 'reimbursed')
    # Checked BEFORE the first write: sqlite3 only opens an implicit transaction
    # on DML, so a True here means an OUTER caller already has one open and this
    # repair is riding inside their unit of work.
    caller_owns_transaction = conn.in_transaction
    try:
        cur = conn.execute(
            "INSERT INTO undercharge_schedule_revisions "
            "(undercharge_id,version,kind,start_year,start_month,total_cents,"
            "installment_count,reason,actor) VALUES (?,1,'deduction',?,?,?,?,"
            "'Legacy schedule created on demand','system')",
            (undercharge_id, sy, sm, plan['total_amount_cents'], count))
        for seq, amount in enumerate(_uc_split_cents(plan['total_amount_cents'], count), 1):
            year, month = _uc_add_month(sy, sm, seq - 1)
            tx = conn.execute(
                "SELECT id FROM deduction_transactions_cents WHERE plan_type='undercharge' "
                "AND plan_id=? AND year=? AND month=? AND amount_cents>0 "
                "AND COALESCE(voided,0)=0 ORDER BY id DESC LIMIT 1",
                (undercharge_id, year, month)).fetchone()
            state = 'cancelled' if terminal and not tx else 'scheduled'
            conn.execute(
                "INSERT INTO undercharge_schedule_items "
                "(revision_id,undercharge_id,sequence,due_year,due_month,amount_cents,"
                "state,transaction_id,state_reason,state_changed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END)",
                (cur.lastrowid, undercharge_id, seq, year, month, amount, state,
                 tx['id'] if tx else None,
                 'Legacy terminal plan' if state == 'cancelled' else None,
                 'Legacy terminal plan' if state == 'cancelled' else None))
    except Exception:
        # Only unwind what we started. Rolling back someone else's open
        # transaction would discard their work on the way to re-raising.
        if not caller_owns_transaction:
            conn.rollback()
        raise
    if not caller_owns_transaction:
        conn.commit()


def create_undercharge_schedule(conn, undercharge_id, start_year, start_month,
                                installment_count, total_cents, kind='deduction',
                                reason=None, actor=None):
    """Supersede unprocessed items of `kind` and create a new exact schedule.

    The caller owns the transaction.  Completed items are never changed.
    Returns the new revision id and item dicts.
    """
    valid = validate_month_year(start_year, start_month)
    if valid is None:
        raise ValueError("A valid schedule start month is required.")
    start_year, start_month = valid
    installment_count = int(installment_count)
    if not 1 <= installment_count <= 60:
        raise ValueError("Installments must be between 1 and 60 months.")
    if kind not in ('deduction', 'refund'):
        raise ValueError("Invalid undercharge schedule kind.")
    plan = conn.execute(
        "SELECT id, employee_id FROM undercharges_cents WHERE id=?",
        (undercharge_id,)).fetchone()
    if not plan:
        raise ValueError("Undercharge not found.")
    sector = get_employee_sector(plan['employee_id'], conn)
    for offset in range(installment_count):
        year, month = _uc_add_month(start_year, start_month, offset)
        if is_period_locked(year, month, sector):
            raise ValueError(f"{MONTH_FULL[month]} {year} payroll is locked.")

    sign = 1 if kind == 'deduction' else -1
    active = conn.execute(
        "SELECT i.id FROM undercharge_schedule_items i "
        "JOIN undercharge_schedule_revisions r ON r.id=i.revision_id "
        "WHERE i.undercharge_id=? AND i.state='scheduled' "
        "AND i.transaction_id IS NULL AND r.kind=?",
        (undercharge_id, kind)).fetchall()
    for row in active:
        conn.execute(
            "UPDATE undercharge_schedule_items SET state='superseded', "
            "state_reason=?, state_changed_at=datetime('now') WHERE id=?",
            (reason or 'Schedule replaced', row['id']))

    version = conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 FROM undercharge_schedule_revisions "
        "WHERE undercharge_id=?", (undercharge_id,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO undercharge_schedule_revisions "
        "(undercharge_id,version,kind,start_year,start_month,total_cents,"
        "installment_count,reason,actor) VALUES (?,?,?,?,?,?,?,?,?)",
        (undercharge_id, version, kind, start_year, start_month,
         abs(int(total_cents)), installment_count, reason or '', actor or 'system'))
    revision_id = cur.lastrowid
    items = []
    for seq, cents in enumerate(_uc_split_cents(total_cents, installment_count), 1):
        year, month = _uc_add_month(start_year, start_month, seq - 1)
        amount = sign * cents
        item_cur = conn.execute(
            "INSERT INTO undercharge_schedule_items "
            "(revision_id,undercharge_id,sequence,due_year,due_month,amount_cents) "
            "VALUES (?,?,?,?,?,?)",
            (revision_id, undercharge_id, seq, year, month, amount))
        items.append({'id': item_cur.lastrowid, 'year': year, 'month': month,
                      'amount_cents': amount})
    return revision_id, items


def record_undercharge_event(conn, undercharge_id, event_type, amount_cents,
                             effective_year=None, effective_month=None, note=None,
                             actor=None, reverses_event_id=None):
    valid_types = {
        'customer_payment', 'write_off', 'liability_adjustment',
        'external_refund', 'refund_waiver', 'customer_payment_reversal',
    }
    if event_type not in valid_types:
        raise ValueError("Invalid undercharge event type.")
    amount_cents = int(amount_cents)
    if event_type != 'liability_adjustment' and amount_cents <= 0:
        raise ValueError("Event amount must be greater than zero.")
    if event_type == 'liability_adjustment' and amount_cents == 0:
        raise ValueError("Adjustment cannot be zero.")
    if effective_year is not None or effective_month is not None:
        valid = validate_month_year(effective_year, effective_month)
        if valid is None:
            raise ValueError("A valid effective month is required.")
        effective_year, effective_month = valid
    cur = conn.execute(
        "INSERT INTO undercharge_events "
        "(undercharge_id,event_type,amount_cents,effective_year,effective_month,"
        "note,actor,reverses_event_id) VALUES (?,?,?,?,?,?,?,?)",
        (undercharge_id, event_type, amount_cents, effective_year, effective_month,
         note or '', actor or 'system', reverses_event_id))
    return cur.lastrowid


# One place where an undercharge's money is derived from its parts. Both the
# single-id read and the batched read below feed this the SAME four inputs, so
# the fast path cannot drift from the slow one — a drift here would mean the
# dashboard's outstanding total disagreed with the undercharge's own page.
def _undercharge_account_from_parts(undercharge_id, plan, tx, by_event, scheduled):
    adjustments = by_event.get('liability_adjustment', 0)
    customer = (by_event.get('customer_payment', 0)
                - by_event.get('customer_payment_reversal', 0))
    write_off = by_event.get('write_off', 0)
    external_refund = by_event.get('external_refund', 0)
    waived = by_event.get('refund_waiver', 0)
    legacy_paid = int(plan['legacy_paid_cents'] or 0)
    legacy_count = int(plan['legacy_payments_count'] or 0)
    adjusted_total = max(int(plan['total_amount_cents']) + adjustments, 0)
    total_employee_deducted = int(tx['deducted']) + legacy_paid
    net_employee = total_employee_deducted - int(tx['refunded']) - external_refund
    recovery_target = max(adjusted_total - write_off, 0)
    remaining = max(recovery_target - customer - net_employee, 0)
    over_recovered = max(customer + net_employee - recovery_target, 0)
    refund_due = max(over_recovered - waived, 0)
    if plan['type'] == 'overcharge':
        derived_status = plan['status']
    elif plan['status'] == 'written_off' and remaining == 0:
        derived_status = 'written_off'
    elif refund_due > 0:
        derived_status = 'refund_scheduled' if scheduled['refunds'] else 'refund_due'
    elif remaining > 0:
        derived_status = 'partial' if (tx['deducted'] or customer) else 'pending'
    elif customer:
        derived_status = 'reimbursed' if (tx['refunded'] or external_refund) else 'paid_by_customer'
    else:
        derived_status = 'recovered'
    return {
        'undercharge_id': undercharge_id,
        'original_total_cents': int(plan['total_amount_cents']),
        'adjustments_cents': adjustments,
        'adjusted_total_cents': adjusted_total,
        'payroll_deducted_cents': total_employee_deducted,
        'ledger_deducted_cents': int(tx['deducted']),
        'legacy_paid_cents': legacy_paid,
        'payroll_refunded_cents': int(tx['refunded']),
        'external_refunded_cents': external_refund,
        'net_employee_paid_cents': net_employee,
        'customer_paid_cents': customer,
        'written_off_cents': write_off,
        'refund_waived_cents': waived,
        'remaining_cents': remaining,
        'refund_due_cents': refund_due,
        'scheduled_deductions_cents': int(scheduled['deductions']),
        'scheduled_refunds_cents': int(scheduled['refunds']),
        'payment_count': int(tx['payment_count']) + legacy_count,
        'legacy_payments_count': legacy_count,
        'status': derived_status,
    }


_UC_EMPTY_TX = {'deducted': 0, 'refunded': 0, 'payment_count': 0}
_UC_EMPTY_SCHEDULED = {'deductions': 0, 'refunds': 0}

# SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds, so an
# IN (?,?,…) over every undercharge in the company would eventually raise
# "too many SQL variables". Batch in chunks well under that.
_UC_BATCH = 400


def ensure_undercharge_schedules(conn, undercharge_ids):
    """Batched ``ensure_undercharge_schedule`` — one existence probe, not N.

    The lazy guard is a WRITE on a read path, which is why it is kept out of the
    common case here: migration 0036 backfilled production and every route insert
    creates a schedule, so the probe normally finds them all and nothing is
    written. Only genuinely unscheduled rows (a direct fixture insert, an old
    operational script) fall through to the per-row repair. Dropping the guard
    from the batched read instead would silently under-report
    ``scheduled_deductions_cents`` for those rows, so it stays.
    """
    ids = list(dict.fromkeys(int(i) for i in undercharge_ids))
    if not ids:
        return
    have = set()
    for start in range(0, len(ids), _UC_BATCH):
        chunk = ids[start:start + _UC_BATCH]
        marks = ','.join('?' * len(chunk))
        have.update(r[0] for r in conn.execute(
            "SELECT DISTINCT undercharge_id FROM undercharge_schedule_revisions "
            f"WHERE undercharge_id IN ({marks})", chunk))
    for uc_id in ids:
        if uc_id not in have:
            ensure_undercharge_schedule(conn, uc_id)


def get_undercharge_accounts(undercharge_ids, conn=None):
    """Ledger-derived financial state for MANY undercharges: ``{id: account}``.

    Identical output to calling ``get_undercharge_account`` per row (both go
    through ``_undercharge_account_from_parts``), but a fixed handful of queries
    per batch instead of five per undercharge. The per-row version in a loop is
    what made /admin issue 815 statements — use this wherever a page needs more
    than one undercharge. Ids with no matching plan are simply absent from the
    result, mirroring the single-id function's ``None``.
    """
    ids = list(dict.fromkeys(int(i) for i in undercharge_ids))
    if not ids:
        return {}
    own = conn is None
    if own:
        conn = get_db()
    try:
        ensure_undercharge_schedules(conn, ids)
        accounts = {}
        for start in range(0, len(ids), _UC_BATCH):
            chunk = ids[start:start + _UC_BATCH]
            marks = ','.join('?' * len(chunk))

            plans = {r['id']: r for r in conn.execute(
                f"SELECT * FROM undercharges_cents WHERE id IN ({marks})", chunk)}

            tx_by_id = {r['plan_id']: r for r in conn.execute(
                "SELECT plan_id,"
                "COALESCE(SUM(CASE WHEN amount_cents>0 THEN amount_cents ELSE 0 END),0) deducted,"
                "COALESCE(SUM(CASE WHEN amount_cents<0 THEN -amount_cents ELSE 0 END),0) refunded,"
                "COUNT(CASE WHEN amount_cents>0 THEN 1 END) payment_count "
                "FROM deduction_transactions_cents WHERE plan_type='undercharge' "
                f"AND plan_id IN ({marks}) AND COALESCE(voided,0)=0 GROUP BY plan_id",
                chunk)}

            events_by_id = defaultdict(dict)
            for r in conn.execute(
                    "SELECT undercharge_id, event_type, "
                    "COALESCE(SUM(amount_cents),0) amount FROM undercharge_events "
                    f"WHERE undercharge_id IN ({marks}) "
                    "GROUP BY undercharge_id, event_type", chunk):
                events_by_id[r['undercharge_id']][r['event_type']] = int(r['amount'])

            sched_by_id = {r['undercharge_id']: r for r in conn.execute(
                "SELECT undercharge_id,"
                "COALESCE(SUM(CASE WHEN amount_cents>0 THEN amount_cents ELSE 0 END),0) deductions,"
                "COALESCE(SUM(CASE WHEN amount_cents<0 THEN -amount_cents ELSE 0 END),0) refunds "
                f"FROM undercharge_schedule_items WHERE undercharge_id IN ({marks}) "
                "AND state='scheduled' AND transaction_id IS NULL "
                "GROUP BY undercharge_id", chunk)}

            for uc_id in chunk:
                plan = plans.get(uc_id)
                if plan is None:
                    continue
                accounts[uc_id] = _undercharge_account_from_parts(
                    uc_id, plan,
                    tx_by_id.get(uc_id, _UC_EMPTY_TX),
                    events_by_id.get(uc_id, {}),
                    sched_by_id.get(uc_id, _UC_EMPTY_SCHEDULED))
        return accounts
    finally:
        if own:
            conn.close()


def get_undercharge_account(undercharge_id, conn=None):
    """Return the ledger-derived financial state for one undercharge in cents."""
    own = conn is None
    if own:
        conn = get_db()
    try:
        ensure_undercharge_schedule(conn, undercharge_id)
        plan = conn.execute(
            "SELECT * FROM undercharges_cents WHERE id=?", (undercharge_id,)).fetchone()
        if not plan:
            return None
        tx = conn.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN amount_cents>0 THEN amount_cents ELSE 0 END),0) deducted,"
            "COALESCE(SUM(CASE WHEN amount_cents<0 THEN -amount_cents ELSE 0 END),0) refunded,"
            "COUNT(CASE WHEN amount_cents>0 THEN 1 END) payment_count "
            "FROM deduction_transactions_cents WHERE plan_type='undercharge' "
            "AND plan_id=? AND COALESCE(voided,0)=0",
            (undercharge_id,)).fetchone()
        events = conn.execute(
            "SELECT event_type, COALESCE(SUM(amount_cents),0) amount "
            "FROM undercharge_events WHERE undercharge_id=? GROUP BY event_type",
            (undercharge_id,)).fetchall()
        by_event = {r['event_type']: int(r['amount']) for r in events}
        scheduled = conn.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN amount_cents>0 THEN amount_cents ELSE 0 END),0) deductions,"
            "COALESCE(SUM(CASE WHEN amount_cents<0 THEN -amount_cents ELSE 0 END),0) refunds "
            "FROM undercharge_schedule_items WHERE undercharge_id=? "
            "AND state='scheduled' AND transaction_id IS NULL",
            (undercharge_id,)).fetchone()
        return _undercharge_account_from_parts(
            undercharge_id, plan, tx, by_event, scheduled)
    finally:
        if own:
            conn.close()


def get_undercharge_timeline(undercharge_id, conn=None):
    """Chronological schedule, transaction and settlement history."""
    own = conn is None
    if own:
        conn = get_db()
    try:
        ensure_undercharge_schedule(conn, undercharge_id)
        events = [dict(r) for r in conn.execute(
            "SELECT id,event_type,amount_cents,effective_year,effective_month,note,"
            "actor,created_at FROM undercharge_events WHERE undercharge_id=? "
            "ORDER BY created_at,id", (undercharge_id,)).fetchall()]
        schedules = [dict(r) for r in conn.execute(
            "SELECT i.*,r.version,r.kind,r.reason revision_reason,r.actor revision_actor,"
            "r.created_at revision_created_at,t.amount_cents transaction_amount_cents,"
            "t.voided transaction_voided,t.created_at transaction_created_at "
            "FROM undercharge_schedule_items i "
            "JOIN undercharge_schedule_revisions r ON r.id=i.revision_id "
            "LEFT JOIN deduction_transactions_cents t ON t.id=i.transaction_id "
            "WHERE i.undercharge_id=? ORDER BY i.due_year,i.due_month,i.id",
            (undercharge_id,)).fetchall()]
        return {'events': events, 'schedule_items': schedules}
    finally:
        if own:
            conn.close()


def sync_undercharge_state(conn, undercharge_id):
    """Keep legacy summary columns compatible while the new ledger is canonical."""
    account = get_undercharge_account(undercharge_id, conn)
    if not account:
        return
    status = account['status']
    legacy_status = {
        'refund_due': 'paid_by_customer',
        'refund_scheduled': 'paid_by_customer',
    }.get(status, status)
    if legacy_status not in (
        'pending', 'partial', 'recovered', 'paid_by_customer',
        'reimbursed', 'written_off', 'accounted_for'):
        legacy_status = 'partial' if account['remaining_cents'] else 'recovered'
    conn.execute(
        "UPDATE undercharges_cents SET payments_made=?,status=? WHERE id=?",
        (account['payment_count'], legacy_status, undercharge_id))


# Month-index window for a legacy undercharge that should be CHARGED in a given
# month: a 'full' plan charges entirely in its start month; a 'split' plan
# charges in each month from start through start+split_months-1. Consumes the
# month-index (year*12+month) THREE times, in order — once for 'full', twice
# for 'split'. Centralised here so the deduction paths (monthly view, payroll
# sheet, reconcile tick-all, monthly per-employee tick) can never drift apart.
_UC_IDX = "(COALESCE(start_year, incident_year) * 12 + COALESCE(start_month, incident_month))"
UC_MONTH_WINDOW = (
    "((recovery_method = 'full' AND " + _UC_IDX + " = ?) "
    "OR (recovery_method = 'split' AND " + _UC_IDX + " <= ? AND " + _UC_IDX + " + split_months > ?))"
)


def tick_undercharges_due(conn, emp_id, year, month):
    """Process every explicit deduction/refund item due for one employee-month.

    Schedule rows carry exact signed cent amounts.  Completed transactions are
    linked atomically and never recalculated after a reschedule.
    """
    plans = conn.execute(
        "SELECT id,status,reimburse_year,reimburse_month,total_amount_cents,"
        "split_months,payments_made FROM undercharges_cents "
        "WHERE employee_id=? AND COALESCE(type,'undercharge')='undercharge'",
        (emp_id,)).fetchall()
    for plan in plans:
        ensure_undercharge_schedule(conn, plan['id'])
        # Compatibility for a legacy/direct fixture that was set to
        # paid_by_customer without going through the new settlement route.
        if (plan['status'] == 'paid_by_customer'
                and plan['reimburse_year'] == year
                and plan['reimburse_month'] == month):
            has_refund = conn.execute(
                "SELECT 1 FROM undercharge_schedule_items WHERE undercharge_id=? "
                "AND amount_cents<0 LIMIT 1", (plan['id'],)).fetchone()
            if not has_refund:
                deducted = conn.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) FROM deduction_transactions_cents "
                    "WHERE plan_type='undercharge' AND plan_id=? AND amount_cents>0 "
                    "AND COALESCE(voided,0)=0", (plan['id'],)).fetchone()[0]
                if deducted <= 0 and plan['payments_made']:
                    deducted = round(
                        plan['payments_made'] * plan['total_amount_cents']
                        / max(plan['split_months'] or 1, 1))
                if deducted > 0:
                    create_undercharge_schedule(
                        conn, plan['id'], year, month, 1, deducted, kind='refund',
                        reason='Legacy scheduled reimbursement', actor='system')

    rows = conn.execute(
        "SELECT i.id item_id,i.undercharge_id,i.amount_cents "
        "FROM undercharge_schedule_items i "
        "JOIN undercharges_cents u ON u.id=i.undercharge_id "
        "WHERE u.employee_id=? AND i.due_year=? AND i.due_month=? "
        "AND i.state='scheduled' AND i.transaction_id IS NULL "
        "ORDER BY i.id", (emp_id, year, month)).fetchall()
    ticked = 0
    for row in rows:
        exists = conn.execute(
            "SELECT id FROM deduction_transactions_cents "
            "WHERE plan_type='undercharge' AND plan_id=? AND year=? AND month=? "
            "AND COALESCE(voided,0)=0", (row['undercharge_id'], year, month)).fetchone()
        if exists:
            conn.execute(
                "UPDATE undercharge_schedule_items SET transaction_id=? WHERE id=?",
                (exists['id'], row['item_id']))
            sync_undercharge_state(conn, row['undercharge_id'])
            continue
        cur = conn.execute(
            "INSERT INTO deduction_transactions_cents "
            "(plan_type,plan_id,employee_id,amount_cents,year,month) "
            "VALUES ('undercharge',?,?,?,?,?)",
            (row['undercharge_id'], emp_id, row['amount_cents'], year, month))
        conn.execute(
            "UPDATE undercharge_schedule_items SET transaction_id=? WHERE id=?",
            (cur.lastrowid, row['item_id']))
        sync_undercharge_state(conn, row['undercharge_id'])
        ticked += 1
    return ticked


def tick_uniform_due(conn, emp_id, year, month):
    """Charge every uniform installment due for this employee-month, on the
    caller's connection/transaction (does NOT commit). Returns rows ticked.

    Single source of truth shared by the monthly per-employee tick and the
    reconcile 'tick all', so the two paths can never drift. Idempotent per
    month: a plan already carrying a non-voided transaction this month is
    skipped. The final installment absorbs the rounding remainder so the plan
    sums exactly to the entered total.

    All money math is in integer cents (money.py convention) straight off the
    ``uniform_deductions_cents`` table — never float Rands.
    """
    query_idx = year * 12 + month
    plans = conn.execute(
        "SELECT * FROM uniform_deductions_cents WHERE employee_id=? AND status='active'"
        " AND (start_year*12+start_month)<=? AND (start_year*12+start_month+term_months)>?"
        " AND payments_made<term_months",
        (emp_id, query_idx, query_idx)
    ).fetchall()
    ticked = 0
    for p in plans:
        exists = conn.execute(
            "SELECT 1 FROM deduction_transactions "
            "WHERE plan_type='uniform' AND plan_id=? AND year=? AND month=? AND COALESCE(voided,0)=0",
            (p['id'], year, month)).fetchone()
        if exists:
            continue

        payments = p['payments_made']
        term = p['term_months']
        monthly_c = p['monthly_amount_cents']
        total_c = p['total_amount_cents'] if p['total_amount_cents'] is not None else term * monthly_c
        current_bal_c = (p['balance_remaining_cents'] if p['balance_remaining_cents'] is not None
                         else total_c - payments * monthly_c)

        # money.installment_cents' convention (kept inline so the tick's
        # historical handling of a zero monthly amount stays verbatim): every
        # installment is the entered monthly, and the last absorbs the rounding
        # remainder so the plan sums exactly to the entered total.
        if payments == term - 1:
            installment_c = total_c - (term - 1) * monthly_c
        else:
            installment_c = monthly_c

        new_balance_c = max(0, current_bal_c - installment_c)
        new_count = min(payments + 1, term)
        status = 'complete' if (new_count >= term or new_balance_c <= 1) else 'active'
        conn.execute(
            "UPDATE uniform_deductions_cents SET payments_made=?, balance_remaining_cents=?, status=? WHERE id=?",
            (new_count, new_balance_c, status, p['id']))
        conn.execute(
            "INSERT INTO deduction_transactions_cents (plan_type, plan_id, employee_id, amount_cents, year, month) "
            "VALUES ('uniform', ?, ?, ?, ?, ?)",
            (p['id'], emp_id, installment_c, year, month))
        ticked += 1
    return ticked


def tick_layby_due(conn, emp_id, year, month):
    """Charge every lay-by installment due for this employee-month, on the
    caller's connection/transaction (does NOT commit). Returns rows ticked.

    Shared by the monthly per-employee tick and the reconcile 'tick all'. The
    regular monthly amount is capped at the outstanding balance so the final
    month never over-deducts past the total. Idempotent per month.

    All money math is in integer cents (money.py convention) straight off the
    ``layby_deductions_cents`` table — never float Rands. Deliberately
    balance-driven rather than money.installment_cents: a stored
    balance_remaining_cents (e.g. after a plan adjustment) is the source of
    truth for what may still be deducted.
    """
    query_idx = year * 12 + month
    plans = conn.execute(
        "SELECT * FROM layby_deductions_cents WHERE employee_id=? AND status='active'"
        " AND (start_year*12+start_month)<=? AND (start_year*12+start_month+term_months)>?"
        " AND payments_made<term_months",
        (emp_id, query_idx, query_idx)
    ).fetchall()
    ticked = 0
    for p in plans:
        exists = conn.execute(
            "SELECT 1 FROM deduction_transactions "
            "WHERE plan_type='layby' AND plan_id=? AND year=? AND month=? AND COALESCE(voided,0)=0",
            (p['id'], year, month)).fetchone()
        if exists:
            continue

        current_bal_c = (p['balance_remaining_cents'] if p['balance_remaining_cents'] is not None
                         else p['total_amount_cents'])
        # Regular monthly installment, capped at the outstanding balance so the
        # final month never over-deducts past the total.
        monthly_c = p['monthly_amount_cents']
        installment_c = min(monthly_c, current_bal_c) if monthly_c else current_bal_c
        installment_c = max(0, installment_c)

        new_balance_c = max(0, current_bal_c - installment_c)
        new_payments = p['payments_made'] + 1
        status = 'complete' if (new_payments >= p['term_months'] or new_balance_c <= 1) else 'active'
        conn.execute(
            "UPDATE layby_deductions_cents SET payments_made=?, balance_remaining_cents=?, status=? WHERE id=?",
            (new_payments, new_balance_c, status, p['id']))
        conn.execute(
            "INSERT INTO deduction_transactions_cents (plan_type, plan_id, employee_id, amount_cents, year, month) "
            "VALUES ('layby', ?, ?, ?, ?, ?)",
            (p['id'], emp_id, installment_c, year, month))
        ticked += 1
    return ticked


def get_top_undercharge_employees(limit=8):
    """Returns employees with the most pending undercharges, sorted by count then amount."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT uc.id plan_id,e.id,e.full_name,e.current_store "
            "FROM undercharges uc JOIN employees e ON e.id=uc.employee_id "
            "WHERE (uc.type IS NULL OR uc.type='undercharge')").fetchall()
        accounts = get_undercharge_accounts([r['plan_id'] for r in rows], conn)
        grouped = {}
        for row in rows:
            account = accounts[row['plan_id']]
            if account['remaining_cents'] <= 0:
                continue
            item = grouped.setdefault(row['id'], {
                'id': row['id'], 'full_name': row['full_name'],
                'current_store': row['current_store'], 'uc_count': 0,
                'uc_outstanding': 0.0})
            item['uc_count'] += 1
            item['uc_outstanding'] += money.to_rands(account['remaining_cents'])
        result = sorted(grouped.values(),
                        key=lambda r: (-r['uc_count'], -r['uc_outstanding']))
        return result[:limit]
    finally:
        conn.close()


def get_repeat_offenders(limit=8, sector='retail'):
    """Per-employee undercharge offence tally for the dashboard warning: how
    many undercharge incidents each person has caused and the total rand value.
    Counts every undercharge ever recorded for the employee, regardless of
    status — undercharges are deducted, but this panel is a behaviour signal."""
    conn = get_db()
    try:
        rows = conn.execute('''
            SELECT e.id, e.full_name, e.current_store,
                   COUNT(uc.id) AS offences,
                   ROUND(SUM(uc.total_amount), 2) AS total_amount
            FROM undercharges uc
            JOIN employees e ON e.id = uc.employee_id
            WHERE (uc.type IS NULL OR uc.type = 'undercharge')
              AND e.sector = ?
            GROUP BY uc.employee_id
            ORDER BY offences DESC, total_amount DESC
            LIMIT ?
        ''', (sector, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_error_hotspots_by_store(limit=8, sector='retail'):
    """Per-store undercharge ('cash miss') tally for the dashboard hotspots
    panel: how many undercharge incidents each store has logged, the total rand
    value, and how many distinct staff were involved. Counts every undercharge
    ever recorded (any status), the same basis as get_repeat_offenders, so the
    'people' and 'stores' views of the same errors stay consistent."""
    conn = get_db()
    try:
        rows = conn.execute('''
            SELECT e.current_store AS store,
                   COUNT(uc.id) AS errors,
                   COUNT(DISTINCT uc.employee_id) AS staff,
                   ROUND(SUM(uc.total_amount), 2) AS total_amount
            FROM undercharges uc
            JOIN employees e ON e.id = uc.employee_id
            WHERE (uc.type IS NULL OR uc.type = 'undercharge')
              AND e.sector = ?
              AND e.current_store IS NOT NULL AND TRIM(e.current_store) <> ''
            GROUP BY e.current_store
            ORDER BY errors DESC, total_amount DESC
            LIMIT ?
        ''', (sector, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_top_debtors(limit=10, sector='retail'):
    """Returns top N active employees by total outstanding balance in a sector."""
    conn = get_db()
    try:
        outstanding = _outstanding_by_employee(conn, sector=sector)
        emps = conn.execute(
            "SELECT id, full_name, current_store, job_title FROM employees "
            "WHERE status = 'active' AND sector = ?", (sector,)).fetchall()
    finally:
        conn.close()
    debtors = []
    for e in emps:
        t = outstanding.get(e['id'])
        if not t:
            continue
        total = round(t['uniform'] + t['layby'] + t['undercharges'], 2)
        if total <= 0:
            continue
        debtors.append({'id': e['id'], 'full_name': e['full_name'],
                        'current_store': e['current_store'], 'job_title': e['job_title'],
                        'total': total, 'uniform_out': t['uniform'],
                        'layby_out': t['layby'], 'uc_out': t['undercharges']})
    debtors.sort(key=lambda d: d['total'], reverse=True)
    return debtors[:limit]


def get_all_outstanding_totals(sector='retail'):
    """Returns {emp_id: total_outstanding} for every employee in a sector."""
    conn = get_db()
    try:
        outstanding = _outstanding_by_employee(conn, sector=sector)
        emp_ids = [r['id'] for r in conn.execute(
            "SELECT id FROM employees WHERE sector = ?", (sector,)).fetchall()]
    finally:
        conn.close()
    result = {}
    for eid in emp_ids:
        t = outstanding.get(eid)
        result[eid] = round(t['uniform'] + t['layby'] + t['undercharges'], 2) if t else 0.0
    return result


def get_monthly_invoice_data(year, month):
    """
    For a given month, return uniform deductions grouped by sale number.
    Used for the 'By Invoice' tab — lets the user see exactly how much to
    allocate against each SO when processing payroll.
    Returns a list of dicts, one per sale number, sorted by store then SO.
    """
    conn = get_db()
    try:
        query_idx = year * 12 + month

        # Pull every active uniform plan active in this month
        rows = conn.execute('''
            SELECT u.sale_number,
                   u.employee_id,
                   e.full_name,
                   e.current_store,
                   u.monthly_amount,
                   u.total_amount,
                   u.term_months,
                   u.payments_made,
                   u.start_month,
                   u.start_year,
                   u.description
            FROM uniform_deductions u
            JOIN employees e ON e.id = u.employee_id
            WHERE u.status IN ('active', 'complete')
              AND e.status = 'active'
              AND (u.start_year * 12 + u.start_month) <= ?
              AND (u.start_year * 12 + u.start_month + u.term_months) > ?
            ORDER BY e.current_store, u.sale_number, e.full_name
        ''', (query_idx, query_idx)).fetchall()
    finally:
        conn.close()

    # Group by sale_number
    groups = {}   # sale_number -> dict
    order  = []   # preserve insertion order

    for r in rows:
        so = r['sale_number'] or '(no sale number)'
        if so not in groups:
            groups[so] = {
                'sale_number':   so,
                'store':         r['current_store'],
                'monthly_total': 0.0,
                'emp_count':     0,
                'employees':     [],
            }
            order.append(so)

        inst_idx = query_idx - (r['start_year'] * 12 + r['start_month'])
        this_month = calc_installment_amount(r['total_amount'], r['monthly_amount'], r['term_months'], inst_idx)

        groups[so]['monthly_total'] = round(groups[so]['monthly_total'] + this_month, 2)
        groups[so]['emp_count']    += 1
        groups[so]['employees'].append({
            'id':     r['employee_id'],
            'name':   r['full_name'],
            'amount': this_month,
        })

        # If multiple stores share the same SO, flag it
        if groups[so]['store'] != r['current_store']:
            groups[so]['store'] = groups[so]['store'] + ' / ' + r['current_store']

    return [groups[so] for so in order]


def get_payroll_history(months=6, sector='retail'):
    """Returns grand totals for the last N months for dashboard history (one sector).

    A month that has ledger rows is summed straight from
    deduction_transactions_cents — the authoritative record of what was
    actually paid — so a plan that has since completed/adjusted never shifts a
    past month retroactively. A month with no ledger rows yet (typically the
    current, un-ticked month) falls back to the live projection from current
    plan state.
    """
    conn = get_db()
    try:
        # Parameterised sector filter — the value is bound, never interpolated.
        sector_val = 'hq' if sector == 'hq' else 'retail'
        sector_emp = "employee_id IN (SELECT id FROM employees WHERE sector = ?)"
        today = date.today()

        month_keys = []
        for i in range(months - 1, -1, -1):
            m = today.month - i
            y = today.year
            while m <= 0:
                m += 12
                y -= 1
            month_keys.append((y, m))

        # One grouped query over the ledger for the whole window.
        start_idx = month_keys[0][0] * 12 + month_keys[0][1]
        end_idx = month_keys[-1][0] * 12 + month_keys[-1][1]
        ledger = {}   # (year, month) -> {plan_type: cents}
        for r in conn.execute('''
            SELECT t.year, t.month, t.plan_type, SUM(t.amount_cents) AS cents
            FROM deduction_transactions_cents t
            JOIN employees e ON e.id = t.employee_id
            WHERE COALESCE(t.voided, 0) = 0
              AND e.sector = ?
              AND (t.year * 12 + t.month) BETWEEN ? AND ?
            GROUP BY t.year, t.month, t.plan_type
        ''', (sector_val, start_idx, end_idx)):
            ledger.setdefault((r['year'], r['month']), {})[r['plan_type']] = r['cents']

        result = []
        for y, m in month_keys:
            idx = y * 12 + m
            if (y, m) in ledger:
                by_type = ledger[(y, m)]
                u = money.to_rands(by_type.get('uniform', 0))
                l = money.to_rands(by_type.get('layby', 0))
                uc = money.to_rands(by_type.get('undercharge', 0))
            else:
                # No ledger rows for this month — project from current plan state.
                uniform_rows = conn.execute(f'''
                    SELECT total_amount, monthly_amount, term_months, start_month, start_year, payments_made
                    FROM uniform_deductions
                    WHERE status='active'
                      AND (start_year*12+start_month)<=?
                      AND (start_year*12+start_month+term_months)>?
                      AND payments_made<term_months
                      AND {sector_emp}
                ''', (idx, idx, sector_val)).fetchall()

                u = 0.0
                for r in uniform_rows:
                    inst_idx = idx - (r['start_year'] * 12 + r['start_month'])
                    inst_amt = calc_installment_amount(r['total_amount'], r['monthly_amount'], r['term_months'], inst_idx)
                    u += inst_amt

                l = conn.execute(f'''
                    SELECT COALESCE(SUM(monthly_amount),0) FROM layby_deductions
                    WHERE status='active'
                      AND (start_year*12+start_month)<=?
                      AND (start_year*12+start_month+term_months)>?
                      AND payments_made<term_months
                      AND {sector_emp}
                ''', (idx, idx, sector_val)).fetchone()[0]

                uc_cents = conn.execute('''
                    SELECT COALESCE(SUM(i.amount_cents),0)
                    FROM undercharge_schedule_items i
                    JOIN undercharges_cents u ON u.id=i.undercharge_id
                    JOIN employees e ON e.id=u.employee_id
                    WHERE i.due_year=? AND i.due_month=?
                      AND i.state='scheduled' AND e.sector=?
                ''', (y, m, sector_val)).fetchone()[0]
                uc = money.to_rands(uc_cents)
            result.append({'year': y, 'month': m, 'month_name': MONTH_NAMES[m],
                           'uniform': round(u, 2), 'layby': round(l, 2),
                           'undercharges': round(uc, 2), 'total': round(u + l + uc, 2)})
    finally:
        conn.close()
    return result


def _undercharge_month_amount(r, txn_amt):
    """Amount a single undercharge row contributes to the selected month.

    Factored out of get_monthly_data's per-employee loop so a sheet can list one
    row per undercharge and have those rows sum EXACTLY to the employee's
    undercharge_total. Sign conventions are preserved verbatim:
      - a reimbursement (paid_by_customer / reimbursed) is NEGATIVE — what was
        already deducted is paid back;
      - a row already ticked this month uses the real (signed) transaction amount;
      - a pending/partial row contributes its full total ('full') or one even
        installment ('split');
      - a recovered / accounted-for row that is not ticked this month contributes
        nothing (0.0).
    `txn_amt` is the {(plan_type, plan_id): amount} map of this month's non-voided
    transactions built by the caller.
    """
    if ('undercharge', r['id']) in txn_amt:
        return txn_amt[('undercharge', r['id'])]
    if r.get('scheduled_amount') is not None:
        return r['scheduled_amount']
    split = r['split_months'] or 1  # legacy fallback for direct fixtures
    if r['status'] in ('paid_by_customer', 'reimbursed'):
        return -r['payments_made'] * (r['total_amount'] / split)
    if r['status'] in ('pending', 'partial'):
        if r['recovery_method'] == 'full':
            return r['total_amount']
        return r['total_amount'] / split
    # 'recovered' / 'accounted_for' and not ticked this month -> nothing to charge
    return 0.0


def get_monthly_data(year, month, sector='retail'):
    conn = get_db()
    try:
        query_idx = year * 12 + month

        # Active staff only — a terminated/written-off employee has left, so they
        # must never be pulled into a payroll deduction run (the monthly view,
        # the "export all" sheet, reconcile/apply, and the dashboard all read
        # this). Their plans still exist in the DB and remain visible on the
        # uniforms/lay-bys/undercharges list views for outstanding-balance
        # tracking; they just aren't deducted from a payroll they're no longer on.
        employees = conn.execute(
            "SELECT id, full_name, current_store, job_title, status FROM employees "
            "WHERE sector = ? AND status = 'active' ORDER BY id", (sector,)
        ).fetchall()

        # Query all active uniform plans for the month in one optimized batch
        all_uniforms = conn.execute('''
            SELECT employee_id, id, total_amount, monthly_amount, term_months, payments_made, description, sku, sale_number, start_month, start_year, status
            FROM uniform_deductions
            WHERE status IN ('active', 'complete')
              AND (start_year * 12 + start_month) <= ?
              AND (start_year * 12 + start_month + term_months) > ?
        ''', (query_idx, query_idx)).fetchall()

        # Query all active layby plans for the month in one optimized batch
        all_laybys = conn.execute('''
            SELECT employee_id, id, monthly_amount, term_months, payments_made, description, sale_number, start_month, start_year, total_amount, balance_remaining, status
            FROM layby_deductions
            WHERE status IN ('active', 'complete')
              AND (start_year * 12 + start_month) <= ?
              AND (start_year * 12 + start_month + term_months) > ?
        ''', (query_idx, query_idx)).fetchall()

        # Ensure direct-import/test rows also have an explicit schedule. Normal
        # route inserts and migration 0036 have already done this, so the batched
        # probe normally confirms them all in one query and writes nothing.
        ensure_undercharge_schedules(conn, [row['id'] for row in conn.execute(
            "SELECT id FROM undercharges_cents "
            "WHERE COALESCE(type,'undercharge')='undercharge'")])

        # One exact schedule item per plan/month. Its signed amount is the
        # projection; once ticked the immutable transaction amount wins below.
        all_ucs = conn.execute('''
            SELECT u.employee_id, u.id, u.total_amount, u.recovery_method,
                   u.split_months, u.payments_made, u.reason, u.incident_month,
                   u.incident_year, u.status, u.reimburse_month, u.reimburse_year,
                   u.start_month, u.start_year, u.sale_number,
                   i.amount_cents / 100.0 AS scheduled_amount
            FROM undercharges u
            JOIN undercharge_schedule_items i ON i.undercharge_id=u.id
            WHERE i.due_year=? AND i.due_month=? AND i.state='scheduled'
              AND (u.type IS NULL OR u.type='undercharge')
        ''', (year, month)).fetchall()

        # Plans that already carry a (non-voided) transaction for this month, and
        # the exact amount taken. Lets the view show "paid" state, and lets a
        # completed plan contribute its real paid amount (not a re-projected
        # installment) for this month while contributing nothing in later months.
        txn_amt = {(r['plan_type'], r['plan_id']): r['amount'] for r in conn.execute(
            "SELECT plan_type, plan_id, amount FROM deduction_transactions "
            "WHERE year = ? AND month = ? AND COALESCE(voided, 0) = 0", (year, month))}
        ticked = set(txn_amt)
    finally:
        conn.close()

    # Pre-group plans by employee_id using fast dictionary index lookups
    def _month_amount(p_dict, plan_type):
        """Amount this plan contributes to the selected month: the real paid
        amount if already ticked, the projected installment while still active,
        and nothing once complete (a paid-off plan in a later in-window month)."""
        key = (plan_type, p_dict['id'])
        if key in txn_amt:
            return txn_amt[key]
        if p_dict['status'] != 'active':
            return 0.0
        # Lay-bys with a per-month figure: show the agreed regular monthly
        # installment for every scheduled month of the term, until the term's
        # payments are complete. We deliberately do NOT shrink it to the stored
        # balance — a stale or mis-set balance must not make the monthly look
        # smaller than what is actually being deducted (that is what made the
        # sheet under-state the final month). The plan finishes after `term`
        # payments; a genuinely inconsistent plan is surfaced by
        # layby_schedule_issues() for review.
        if plan_type == 'layby' and p_dict['monthly_amount']:
            months_left = (p_dict.get('term_months') or 0) - (p_dict.get('payments_made') or 0)
            return round(p_dict['monthly_amount'], 2) if months_left > 0 else 0.0
        inst_idx = query_idx - (p_dict['start_year'] * 12 + p_dict['start_month'])
        return calc_installment_amount(p_dict['total_amount'], p_dict['monthly_amount'],
                                       p_dict['term_months'], inst_idx)

    uniforms_by_emp = defaultdict(list)
    for p in all_uniforms:
        p_dict = dict(p)
        # Preserve the plan's agreed regular monthly BEFORE monthly_amount is
        # replaced with this month's (possibly final/paid) installment, so a
        # sheet can compute Remaining = total - payments_made*regular_monthly.
        p_dict['regular_monthly'] = p_dict['monthly_amount']
        p_dict['monthly_amount'] = _month_amount(p_dict, 'uniform')
        uniforms_by_emp[p_dict['employee_id']].append(p_dict)

    laybys_by_emp = defaultdict(list)
    for p in all_laybys:
        p_dict = dict(p)
        p_dict['regular_monthly'] = p_dict['monthly_amount']
        p_dict['monthly_amount'] = _month_amount(p_dict, 'layby')
        laybys_by_emp[p_dict['employee_id']].append(p_dict)

    ucs_by_emp = defaultdict(list)
    for r in all_ucs:
        ucs_by_emp[r['employee_id']].append(dict(r))

    result = []
    for emp in employees:
        eid = emp['id']
        uniform_plans = uniforms_by_emp[eid]
        layby_plans = laybys_by_emp[eid]

        uc_rows = ucs_by_emp[eid]

        uniform_total = sum(p['monthly_amount'] for p in uniform_plans)
        layby_total = sum(p['monthly_amount'] for p in layby_plans)
        undercharge_total = 0.0
        for r in uc_rows:
            # Per-row month contribution (module-level helper) — attaching it to
            # the row lets a per-undercharge sheet sum exactly to this total.
            r['month_amount'] = _undercharge_month_amount(r, txn_amt)
            undercharge_total += r['month_amount']

        uniform_done = bool(uniform_plans) and all(('uniform', p['id']) in ticked for p in uniform_plans)
        layby_done = bool(layby_plans) and all(('layby', p['id']) in ticked for p in layby_plans)
        uc_done = bool(uc_rows) and all(('undercharge', r['id']) in ticked for r in uc_rows)

        result.append({
            'employee': dict(emp),
            'uniform_total': round(uniform_total, 2),
            'uniform_plans': uniform_plans,
            'uniform_done': uniform_done,
            'layby_total': round(layby_total, 2),
            'layby_plans': layby_plans,
            'layby_done': layby_done,
            'undercharge_total': round(undercharge_total, 2),
            'undercharge_rows': uc_rows,
            'undercharge_done': uc_done,
            'all_done': ((not uniform_plans or uniform_done) and
                         (not layby_plans or layby_done) and
                         (not uc_rows or uc_done)),
            'total': round(uniform_total + layby_total + undercharge_total, 2)
        })

    return result


def is_period_locked(year, month, sector='retail'):
    """Checks if a given payroll period (year and month) is locked for a sector."""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM locked_periods WHERE year = ? AND month = ? AND sector = ?",
        (year, month, sector)
    ).fetchone()
    conn.close()
    return row is not None


def get_employee_sector(emp_id, conn=None):
    """Return an employee's sector ('retail' or 'hq'), defaulting to 'retail'."""
    own = conn is None
    if own:
        conn = get_db()
    try:
        row = conn.execute("SELECT sector FROM employees WHERE id=?", (emp_id,)).fetchone()
    finally:
        if own:
            conn.close()
    return (row['sector'] if row and row['sector'] else 'retail')


# ── Auth helpers ──────────────────────────────────────────────────────────────
# The single canonical identity + credential store is the `users` table (see
# migration 0025). A person is one `users` row (login = username OR email) with
# one password; admin capability is held in `user_roles`. The old admin_users /
# cc_users tables are frozen (kept for rollback) — every helper below now reads
# and writes `users`. Portal scope (cards, RM stores) still joins by email off
# cc_card_users / rm_stores.

# Valid admin access tiers. Anything outside this set is rejected at the route
# layer and would, if it ever reached the gate, be treated as no access.
ADMIN_ROLES = ('super', 'retail', 'hq')

# When a helper must present a single "primary" role for a user that could hold
# several grants, this is the precedence (most-powerful first).
_ROLE_PRIMARY_SQL = (
    "(SELECT r.role FROM user_roles r WHERE r.user_id = u.id "
    " ORDER BY CASE r.role WHEN 'super' THEN 0 WHEN 'retail' THEN 1 "
    "                      WHEN 'hq' THEN 2 ELSE 3 END LIMIT 1) AS role")


def get_admin_user(username):
    """Look up an ADMIN by login (case-insensitive). Returns a row shaped like
    the old admin_users row (id, username, display_name, password_hash, role,
    …) or None if the login is unknown or holds no admin role."""
    conn = get_db()
    try:
        return conn.execute(
            f"SELECT u.id, u.login AS username, u.display_name, u.password_hash, "
            f"       u.email, u.is_active, u.auth_version, u.created_at, {_ROLE_PRIMARY_SQL} "
            f"FROM users u "
            f"WHERE u.login = ? "
            f"  AND EXISTS (SELECT 1 FROM user_roles r WHERE r.user_id = u.id)",
            ((username or '').strip(),)).fetchone()
    finally:
        conn.close()


def get_all_admin_users():
    """Every admin (a user holding at least one role), newest role summarised."""
    conn = get_db()
    try:
        return conn.execute(
            f"SELECT u.id, u.login AS username, u.display_name, u.created_at, "
            f"       {_ROLE_PRIMARY_SQL} "
            f"FROM users u "
            f"WHERE EXISTS (SELECT 1 FROM user_roles r WHERE r.user_id = u.id) "
            f"ORDER BY u.id"
        ).fetchall()
    finally:
        conn.close()


def create_admin_user(username, display_name, password_hash, role='super'):
    """Create a person WITH an admin role. login = username (or email if it is
    one). Raises on a duplicate login (callers check get_admin_user first)."""
    if role not in ADMIN_ROLES:
        role = 'super'
    login = (username or '').strip()
    email = login if '@' in login else None
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO users (login, email, display_name, password_hash) "
            "VALUES (?, ?, ?, ?)", (login, email, display_name, password_hash))
        conn.execute("INSERT INTO user_roles (user_id, role) VALUES (?, ?)",
                     (cur.lastrowid, role))
        conn.commit()
    finally:
        conn.close()


def delete_admin_user(user_id):
    """Remove admin capability, preserving a shared portal/store identity."""
    conn = get_db()
    try:
        with conn:
            row = conn.execute("SELECT login, email FROM users WHERE id=?", (user_id,)).fetchone()
            conn.execute("DELETE FROM user_roles WHERE user_id = ?", (user_id,))
            conn.execute(
                "UPDATE users SET auth_version=auth_version+1, updated_at=datetime('now') "
                "WHERE id=?", (user_id,))
            if row:
                email = (row['email'] or row['login'] or '').strip().lower()
                conn.execute(
                    "DELETE FROM users WHERE id=? "
                    "AND NOT EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_id=users.id) "
                    "AND NOT EXISTS (SELECT 1 FROM cc_card_users cu WHERE cu.email=?) "
                    "AND NOT EXISTS (SELECT 1 FROM rm_users rm WHERE rm.email=?) "
                    "AND NOT EXISTS (SELECT 1 FROM store_emails se WHERE se.email=?)",
                    (user_id, email, email, email))
    finally:
        conn.close()


def admin_user_count():
    """How many people hold an admin role."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM users u "
            "WHERE EXISTS (SELECT 1 FROM user_roles r WHERE r.user_id = u.id)"
        ).fetchone()[0]
    finally:
        conn.close()


# ── Unified identity primitives (used by login + admin management) ────────────

def get_user(login):
    """Unified identity lookup by login (a username or an email), any kind."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE login = ?", ((login or '').strip(),)
        ).fetchone()
    finally:
        conn.close()


def get_session_user(user_id):
    """Identity state used to validate a signed login session on every request."""
    if not user_id:
        return None
    conn = get_db()
    try:
        return conn.execute(
            f"SELECT u.*, {_ROLE_PRIMARY_SQL} FROM users u WHERE u.id=?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()


def get_user_roles(user_id):
    """The set of admin role grants a user holds (empty for portal-only people)."""
    conn = get_db()
    try:
        return {r['role'] for r in conn.execute(
            "SELECT role FROM user_roles WHERE user_id = ?", (user_id,)).fetchall()}
    finally:
        conn.close()


def set_admin_role(user_id, role):
    """Replace a user's admin roles with a single role (admin management)."""
    if role not in ADMIN_ROLES:
        role = 'super'
    conn = get_db()
    try:
        conn.execute("DELETE FROM user_roles WHERE user_id = ?", (user_id,))
        conn.execute("INSERT INTO user_roles (user_id, role) VALUES (?, ?)",
                     (user_id, role))
        conn.execute(
            "UPDATE users SET auth_version=auth_version+1, updated_at=datetime('now') "
            "WHERE id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def set_user_password(user_id, password_hash):
    """Reset any user's password by id (admin reset + self-service change)."""
    conn = get_db()
    try:
        conn.execute("UPDATE users SET password_hash=?, auth_version=auth_version+1, "
                     "updated_at=datetime('now') "
                     "WHERE id=?", (password_hash, user_id))
        conn.commit()
    finally:
        conn.close()


def set_user_active(user_id, active):
    """Enable/disable a login. A disabled user can't sign in (checked at login)."""
    conn = get_db()
    try:
        conn.execute("UPDATE users SET is_active=?, auth_version=auth_version+1, "
                     "updated_at=datetime('now') "
                     "WHERE id=?", (1 if active else 0, user_id))
        conn.commit()
    finally:
        conn.close()


def active_super_count():
    """How many ENABLED super admins exist (guards against locking everyone out)."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM users u WHERE u.is_active=1 AND EXISTS "
            "(SELECT 1 FROM user_roles r WHERE r.user_id=u.id AND r.role='super')"
        ).fetchone()[0]
    finally:
        conn.close()


def user_capabilities(login):
    """Everything a login can do, unified across admin roles + portal scopes.
    Returns None if the login is unknown. `super` implies both retail and hq."""
    u = get_user(login)
    if not u:
        return None
    roles = get_user_roles(u['id'])
    email = (u['email'] or u['login'] or '').strip().lower()
    cards = find_cc_cards_for_email(email) if email else []
    rm = get_rm_user(email) if email else None
    is_rm = bool(rm and rm['active'])
    # A store login is a `users` row whose login IS a configured store email.
    staff_store = get_store_by_email(u['login']) if u['login'] else None
    return {
        'id': u['id'], 'login': u['login'], 'email': u['email'],
        'display_name': u['display_name'], 'is_active': bool(u['is_active']),
        'roles': roles, 'is_admin': bool(roles), 'super': 'super' in roles,
        'sectors': ({'retail', 'hq'} if 'super' in roles else (roles & {'retail', 'hq'})),
        'has_cards': bool(cards), 'card_count': len(cards),
        'is_rm': is_rm, 'rm_stores': (get_rm_stores(email) if is_rm else []),
        'staff_store': staff_store,
    }


def list_all_users():
    """Roster for the People & Access console: every login with everything it can
    do — admin roles, cards, RM scope, store login.

    Six queries for the whole roster, not five per person: the console is also the
    place access is EDITED now, so each row carries its cards and assigned stores,
    and doing that per user would have been ~250 statements on a 46-login page
    (batched deliberately).
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, login, email, display_name, is_active, created_at "
            "FROM users ORDER BY login COLLATE NOCASE").fetchall()
        roles_by_user = {}
        for r in conn.execute("SELECT user_id, role FROM user_roles").fetchall():
            roles_by_user.setdefault(r['user_id'], set()).add(r['role'])
        cards_by_email = {}
        for r in conn.execute(
                "SELECT cu.email, c.id, c.display_name, c.card_name FROM cc_card_users cu "
                "JOIN cc_cards c ON c.id = cu.card_id WHERE c.active=1 "
                "ORDER BY c.display_name, c.card_name").fetchall():
            cards_by_email.setdefault((r['email'] or '').strip().lower(), []).append(
                {'id': r['id'], 'label': r['display_name'] or r['card_name']})
        rm_by_email = {(r['email'] or '').strip().lower(): r
                       for r in conn.execute("SELECT * FROM rm_users").fetchall()}
        stores_by_email = {}
        for r in conn.execute("SELECT store, email FROM rm_stores ORDER BY store").fetchall():
            stores_by_email.setdefault((r['email'] or '').strip().lower(), []).append(r['store'])
        store_by_login = {(r['email'] or '').strip().lower(): r['store']
                          for r in conn.execute("SELECT store, email FROM store_emails").fetchall()}
    finally:
        conn.close()

    out = []
    for r in rows:
        email = (r['email'] or r['login'] or '').strip().lower()
        roles = roles_by_user.get(r['id'], set())
        rm = rm_by_email.get(email)
        rm_active = bool(rm and rm['active'])
        cards = cards_by_email.get(email, [])
        out.append({
            **dict(r),
            'roles': sorted(roles),
            'is_admin': bool(roles),
            'role': ('super' if 'super' in roles else
                     next(iter(sorted(roles))) if roles else ''),
            'card_count': len(cards),
            'cards': cards,
            'is_rm': rm_active,
            'rm_known': rm is not None,
            'rm_stores': stores_by_email.get(email, []),
            'rm_store_count': len(stores_by_email.get(email, [])),
            'staff_store': store_by_login.get((r['login'] or '').strip().lower()),
        })
    return out


def set_user_display_name(user_id, display_name):
    """Rename a person. Identity (login/email) is deliberately NOT editable here —
    it is what cards, RM scope and store logins are keyed on."""
    conn = get_db()
    try:
        conn.execute("UPDATE users SET display_name=?, updated_at=datetime('now') "
                     "WHERE id=?", ((display_name or '').strip() or None, user_id))
        conn.commit()
    finally:
        conn.close()


# ── Store email helpers ───────────────────────────────────────────────────────

def get_store_by_email(email):
    """Return store name for a given email, or None."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT store FROM store_emails WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
        return row['store'] if row else None
    finally:
        conn.close()


def get_all_store_emails():
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM store_emails ORDER BY store"
        ).fetchall()
    finally:
        conn.close()


def upsert_store_email(store, email):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO store_emails (store, email) VALUES (?, ?) "
            "ON CONFLICT(email) DO UPDATE SET store = excluded.store",
            (store, email.strip().lower()))
        conn.commit()
    finally:
        conn.close()


def delete_store_email(email_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM store_emails WHERE id = ?", (email_id,))
        conn.commit()
    finally:
        conn.close()


def store_password_logins():
    """Set of store-email logins that have their OWN password in `users` (i.e. no
    longer rely on the shared staff password). Used by the staff-logins console."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT u.login FROM users u JOIN store_emails s ON s.email = u.login"
        ).fetchall()
        return {r['login'] for r in rows}
    finally:
        conn.close()


def clear_store_password(email):
    """Remove a store's individual credential (revert it to the shared password).
    Only drops a store-email login that holds no admin role."""
    email = (email or '').strip().lower()
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM users WHERE login=? AND NOT EXISTS "
            "(SELECT 1 FROM user_roles r WHERE r.user_id=users.id)", (email,))
        conn.commit()
    finally:
        conn.close()


def bulk_import_store_emails(data):
    """Import store emails in bulk. data = list of (store, email)."""
    conn = get_db()
    try:
        count = 0
        for store, email in data:
            conn.execute(
                "INSERT INTO store_emails (store, email) VALUES (?, ?) "
                "ON CONFLICT(email) DO UPDATE SET store = excluded.store",
                (store.strip(), email.strip().lower()))
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()


# ── Staff allowances (HQ/DC) ─────────────────────────────────────────────────
# An annual goods budget per employee. Purchases draw it down; remaining is
# always allocated - spent, computed here so it can never drift. Overspend is
# allowed (remaining goes negative) — the UI flags it, policy decides.

def set_allowance(emp_id, year, allocated_rands, notes=None):
    """Create or update an employee's allocation for a year."""
    conn = get_db()
    try:
        # COALESCE keeps an existing note when the caller doesn't send one
        # (e.g. re-saving the amount from the profile must not wipe it).
        conn.execute(
            "INSERT INTO allowances (employee_id, year, allocated_cents, notes) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(employee_id, year) DO UPDATE SET "
            "allocated_cents = excluded.allocated_cents, "
            "notes = COALESCE(excluded.notes, notes)",
            (emp_id, year, to_cents(allocated_rands), notes))
        conn.commit()
    finally:
        conn.close()


def get_allowance_summary(emp_id, year):
    """{'allocated','spent','remaining'} in rands, or None if no allocation
    exists for that year (and there are no purchases either)."""
    conn = get_db()
    try:
        alloc = conn.execute(
            "SELECT allocated_cents FROM allowances WHERE employee_id=? AND year=?",
            (emp_id, year)).fetchone()
        spent = conn.execute(
            "SELECT COALESCE(SUM(line_total_cents), 0) FROM allowance_purchases "
            "WHERE employee_id=? AND year=?", (emp_id, year)).fetchone()[0]
    finally:
        conn.close()
    if alloc is None and spent == 0:
        return None
    allocated = alloc['allocated_cents'] if alloc else 0
    return {'allocated': money.to_rands(allocated),
            'spent': money.to_rands(spent),
            'remaining': money.to_rands(allocated - spent)}


def get_allowance_purchases(emp_id, year):
    """Purchase lines for one employee-year, oldest first, money in rands."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT id, purchase_date, sku, description, quantity, location, "
            "       sale_number, notes, "
            "       unit_price_cents / 100.0 AS unit_price, "
            "       line_total_cents / 100.0 AS line_total "
            "FROM allowance_purchases WHERE employee_id=? AND year=? "
            "ORDER BY purchase_date, id", (emp_id, year)).fetchall()
    finally:
        conn.close()


def add_allowance_purchases(emp_id, year, purchase_date, items,
                            location=None, sale_number=None, notes=None):
    """Insert purchase lines. items = [{'sku','desc','price','qty'}] in rands."""
    conn = get_db()
    try:
        for it in items:
            unit = to_cents(it['price'])
            qty = int(it.get('qty', 1) or 1)
            conn.execute(
                "INSERT INTO allowance_purchases "
                "(employee_id, year, purchase_date, sku, description, quantity, "
                " unit_price_cents, line_total_cents, location, sale_number, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (emp_id, year, purchase_date, it.get('sku') or None, it['desc'],
                 qty, unit, unit * qty, location, sale_number, notes))
        conn.commit()
        return len(items)
    finally:
        conn.close()


def delete_allowance_purchase(purchase_id):
    """Remove one purchase line; returns the employee_id for the redirect."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT employee_id FROM allowance_purchases WHERE id=?",
            (purchase_id,)).fetchone()
        if row:
            conn.execute("DELETE FROM allowance_purchases WHERE id=?", (purchase_id,))
            conn.commit()
        return row['employee_id'] if row else None
    finally:
        conn.close()


def get_allowances_overview(year, sector='hq'):
    """One row per active employee in the sector: allocation, spent, remaining
    (rands). Employees without an allocation still appear with allocated=0 so
    nobody silently falls off the page."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT e.id, e.full_name, e.job_title, e.current_store, "
            "       COALESCE(a.allocated_cents, 0) AS allocated_cents, "
            "       COALESCE(p.spent_cents, 0) AS spent_cents "
            "FROM employees e "
            "LEFT JOIN allowances a ON a.employee_id = e.id AND a.year = ? "
            "LEFT JOIN (SELECT employee_id, SUM(line_total_cents) AS spent_cents "
            "           FROM allowance_purchases WHERE year = ? GROUP BY employee_id) p "
            "       ON p.employee_id = e.id "
            "WHERE e.sector = ? AND e.status = 'active' "
            "ORDER BY e.current_store, e.full_name",
            (year, year, sector)).fetchall()
    finally:
        conn.close()
    return [{'id': r['id'], 'full_name': r['full_name'],
             'job_title': r['job_title'], 'current_store': r['current_store'],
             'allocated': money.to_rands(r['allocated_cents']),
             'spent': money.to_rands(r['spent_cents']),
             'remaining': money.to_rands(r['allocated_cents'] - r['spent_cents'])}
            for r in rows]


# ── Cash Reconciliation (store float ledger) ─────────────────────────────────

def get_recon_categories():
    """Active reconciliation categories (the fixed picklist), ordered."""
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, name, kind, xero_code, requires_receipt, reason_hint "
            "FROM recon_categories "
            "WHERE active = 1 ORDER BY kind, sort_order, name")]
    finally:
        conn.close()


def _recon_entries(conn, store, year, month):
    """Raw entries for a store-month, oldest first (date then insert order)."""
    return conn.execute(
        "SELECT e.id, e.entry_date, e.category_id, e.description, e.direction, e.amount_cents, "
        "       e.note, e.receipt_id, e.created_by, c.xero_code, c.kind, c.vat_type "
        "FROM cash_recon_entries e LEFT JOIN recon_categories c ON c.id = e.category_id "
        "WHERE e.store = ? AND substr(e.entry_date,1,7) = ? "
        "ORDER BY e.entry_date, e.id",
        (store, f"{year:04d}-{month:02d}")).fetchall()


# ── App-level settings (key/value) ───────────────────────────────────────────

def get_setting(key, default=None):
    """A single app_settings value, or `default` if unset."""
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row['value'] if row is not None else default
    finally:
        conn.close()


def set_setting(key, value):
    """Upsert an app_settings value (stored as text)."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, '' if value is None else str(value)))
        conn.commit()
    finally:
        conn.close()


# ── Consolidated cash-sales figures (per store, per month) ────────────────────

# Which income category counts as a "cash sale" for the sales journal + recon —
# the till-takings/sales category, NOT float top-ups. Matched case-insensitively
# on the name containing "sale" (so a rename like "Till sales" or "Sales – cash"
# still qualifies, while "Cash Float Top Up" / "Cash Top Up" are excluded).
CASH_SALE_NAME_LIKE = '%sale%'

# scripts/seed_demo_cash_recon.py names its throwaway stores "ZZ DEMO - …".
# Those stores carry realistic cash sales, so without this they land in the
# consolidated cash-sales journal and its comparison workbook as real money
# against an imported ledger workbook. Blanking their POS code is not enough — a store
# with sales but no code makes the whole export fail closed instead. The demo
# stays fully visible in the cash dashboards; it just never reaches Xero.
DEMO_STORE_PREFIX = 'ZZ DEMO - '


def _demo_like():
    """LIKE pattern for demo store names, with `%`/`_` in the prefix escaped."""
    escaped = DEMO_STORE_PREFIX.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return escaped + '%'


def is_demo_store(store):
    """True for a store seeded by scripts/seed_demo_cash_recon.py.

    The Python-side twin of `_demo_like()`, so "may this store's money reach a
    Xero export?" has ONE answer: the aggregate SQL helpers filter with the LIKE
    pattern, the journal routes that build a file per store filter with this."""
    return (store or '').startswith(DEMO_STORE_PREFIX)


def get_xero_export_stores():
    """`get_stores()` minus the demo stores — the stores a Xero journal may cover.

    The expense journals are built store-by-store off `get_stores()`, which is
    how demo money reached the batch ZIP: since the seeder stopped assigning a
    POS code, one demo store with expenses made the fail-closed preflight
    refuse EVERY store's journal. The demo stays fully visible in the cash
    dashboards and ledgers; it is only the Xero exports that exclude it."""
    return [s for s in get_stores() if not is_demo_store(s)]


def get_cash_sales_journal_stores(year, month):
    """Every store relevant to the month's cash-split journal:
        {store, tracking_name, store_code, sales_cents}

    Mapped stores are retained at zero to match finance's established monthly
    template. An unmapped store is included only when it has cash sales, so a
    real amount can never disappear merely because setup is incomplete.

    The second leg of the UNION is the reason this isn't a plain stores-driven
    query: an entry whose store is no longer in the `stores` table would then be
    silently dropped from the journal while still showing up in
    get_cash_sales_line_items (the recon-export detail sheet), so sheet 2 would
    out-total sheet 1 with no warning. Such an orphan comes back with a NULL
    store_code, which the export route's fail-closed mapping check rejects —
    the download is refused rather than emitting the #### placeholder.
    """
    conn = get_db()
    try:
        ym = f"{year:04d}-{month:02d}"
        rows = conn.execute(
            "SELECT s.name AS store, COALESCE(NULLIF(trim(s.cash_sales_label),''),s.name) tracking_name, "
            "       s.store_code AS store_code, "
            "       COALESCE(SUM(CASE WHEN e.direction='in' AND c.kind='income' "
            "         AND lower(c.name) LIKE ? THEN e.amount_cents ELSE 0 END), 0) sales_cents "
            "FROM stores s "
            "LEFT JOIN cash_recon_entries e ON e.store=s.name AND substr(e.entry_date,1,7)=? "
            "LEFT JOIN recon_categories c ON c.id=e.category_id "
            "WHERE s.name NOT LIKE ? ESCAPE '\\' "
            "GROUP BY s.id, s.name, s.cash_sales_label, s.store_code "
            "HAVING trim(COALESCE(s.store_code,''))!='' OR sales_cents!=0 "
            "UNION ALL "
            "SELECT e.store AS store, e.store AS tracking_name, NULL AS store_code, "
            "       COALESCE(SUM(e.amount_cents), 0) AS sales_cents "
            "FROM cash_recon_entries e "
            "JOIN recon_categories c ON c.id=e.category_id "
            "WHERE substr(e.entry_date,1,7)=? AND e.direction='in' "
            "  AND c.kind='income' AND lower(c.name) LIKE ? "
            "  AND e.store NOT LIKE ? ESCAPE '\\' "
            "  AND NOT EXISTS (SELECT 1 FROM stores s2 WHERE s2.name=e.store) "
            "GROUP BY e.store "
            "HAVING sales_cents!=0 "
            "ORDER BY store",
            (CASH_SALE_NAME_LIKE, ym, _demo_like(), ym, CASH_SALE_NAME_LIKE,
             _demo_like())).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_cash_sales_line_items(year, month):
    """Every individual 'Cash Sale' entry for the month, for the per-store
    drill-down: {store, date, sale_no, amount_cents}. `sale_no` is the entry's
    note — stores record the till/receipt/sale number there (the category is
    'Cash Sale (specify receipt number)'). Ordered by store then date."""
    conn = get_db()
    try:
        ym = f"{year:04d}-{month:02d}"
        rows = conn.execute(
            "SELECT e.store AS store, substr(e.entry_date,1,10) AS date, "
            "       e.note AS sale_no, e.amount_cents AS amount_cents "
            "FROM cash_recon_entries e JOIN recon_categories c ON c.id = e.category_id "
            "WHERE substr(e.entry_date,1,7) = ? AND e.direction = 'in' "
            "  AND c.kind = 'income' AND lower(c.name) LIKE ? "
            # Same demo exclusion as get_cash_sales_journal_stores, so the recon
            # workbook's detail sheet keeps tying to its summary sheet.
            "  AND e.store NOT LIKE ? ESCAPE '\\' "
            "ORDER BY e.store, e.entry_date, e.id",
            (ym, CASH_SALE_NAME_LIKE, _demo_like())).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def save_cash_shopify_upload(year, month, filename, sha256, rows, uploaded_by=None):
    """Atomically replace one month's normalized Shopify cash-payment rows."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM cash_shopify_uploads WHERE year=? AND month=?", (year, month))
        cur = conn.execute(
            "INSERT INTO cash_shopify_uploads "
            "(year,month,source_filename,source_sha256,row_count,uploaded_by) VALUES (?,?,?,?,?,?)",
            (year, month, filename, sha256, len(rows), uploaded_by))
        upload_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO cash_shopify_rows "
            "(upload_id,source_row,pos_location_name,payment_gateway,order_name,transactions,"
            "gross_cents,refunded_cents,net_cents) VALUES (?,?,?,?,?,?,?,?,?)",
            [(upload_id, r['source_row'], r['pos_location_name'], r['payment_gateway'],
              r['order_name'], r['transactions'], r['gross_cents'],
              r['refunded_cents'], r['net_cents']) for r in rows])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_cash_shopify_upload(year, month):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM cash_shopify_uploads WHERE year=? AND month=?", (year, month)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_cash_shopify_summary(year, month):
    """Uploaded Shopify cash totals grouped by raw location and resolved store."""
    conn = get_db()
    try:
        rows = conn.execute('''
            SELECT r.pos_location_name, m.store,
                   SUM(r.transactions) transactions,
                   SUM(r.gross_cents) gross_cents,
                   SUM(r.refunded_cents) refunded_cents,
                   SUM(r.net_cents) net_cents
            FROM cash_shopify_rows r
            JOIN cash_shopify_uploads u ON u.id=r.upload_id
            LEFT JOIN cash_shopify_store_mappings m
              ON m.shopify_location=r.pos_location_name COLLATE NOCASE
            WHERE u.year=? AND u.month=?
            GROUP BY r.pos_location_name, m.store
            ORDER BY r.pos_location_name COLLATE NOCASE
        ''', (year, month)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def save_cash_shopify_mapping(shopify_location, store):
    shopify_location, store = (shopify_location or '').strip(), (store or '').strip()
    if not shopify_location or store not in get_stores():
        raise ValueError('Choose a valid Shopify location and NORTHWIND store.')
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO cash_shopify_store_mappings (shopify_location,store) VALUES (?,?) "
            "ON CONFLICT(shopify_location) DO UPDATE SET store=excluded.store",
            (shopify_location, store))
        conn.commit()
    finally:
        conn.close()


def get_cash_sales_variance_reasons(year, month):
    conn = get_db()
    try:
        return {r['store']: r['reason'] for r in conn.execute(
            "SELECT store,reason FROM cash_sales_variance_reasons WHERE year=? AND month=?",
            (year, month)).fetchall()}
    finally:
        conn.close()


def save_cash_sales_variance_reasons(year, month, reasons, updated_by=None):
    conn = get_db()
    try:
        for store, reason in reasons.items():
            reason = (reason or '').strip()
            if reason:
                conn.execute('''
                    INSERT INTO cash_sales_variance_reasons
                    (year,month,store,reason,updated_by) VALUES (?,?,?,?,?)
                    ON CONFLICT(year,month,store) DO UPDATE SET
                      reason=excluded.reason, updated_by=excluded.updated_by,
                      updated_at=datetime('now')
                ''', (year, month, store, reason, updated_by))
            else:
                conn.execute(
                    "DELETE FROM cash_sales_variance_reasons WHERE year=? AND month=? AND store=?",
                    (year, month, store))
        conn.commit()
    finally:
        conn.close()


# ── Xero MJ export: admin config + line builder ──────────────────────────────

def get_recon_categories_admin():
    """Every category (active + inactive) with its Xero mapping, for the admin
    Xero-setup page. Ordered by kind then sort order then name."""
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, name, kind, xero_code, vat_type, requires_receipt, active, "
            "       sort_order, reason_hint "
            "FROM recon_categories ORDER BY kind, sort_order, name")]
    finally:
        conn.close()


def add_recon_category(name, xero_code=None, vat_type=None, kind='expense'):
    """Create a new reconciliation category (default an expense) so stores can
    pick it and the MJ export can code it. Returns (ok, message). Names are
    unique case-insensitively — a duplicate is rejected, not silently added."""
    name = (name or '').strip()
    if not name:
        return False, 'Enter a category name.'
    code = (xero_code or '').strip() or None
    vt = vat_type if vat_type in ('standard', 'novat') else None
    conn = get_db()
    try:
        dupe = conn.execute(
            "SELECT 1 FROM recon_categories WHERE lower(name) = lower(?)", (name,)).fetchone()
        if dupe:
            return False, f'“{name}” already exists.'
        nxt = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM recon_categories WHERE kind = ?",
            (kind,)).fetchone()[0]
        conn.execute(
            "INSERT INTO recon_categories (name, kind, xero_code, vat_type, "
            "requires_receipt, active, sort_order) VALUES (?,?,?,?,0,1,?)",
            (name, kind, code, vt, nxt))
        conn.commit()
        return True, f'Added “{name}”.'
    finally:
        conn.close()


def set_recon_category_active(cat_id, active):
    """Archive (active=0) or restore (active=1) a category. Archived categories
    stay in the DB (and on past entries) but drop out of the stores' picker."""
    conn = get_db()
    try:
        conn.execute("UPDATE recon_categories SET active = ? WHERE id = ?",
                     (1 if active else 0, cat_id))
        conn.commit()
    finally:
        conn.close()


def update_recon_category_xero(cat_id, xero_code, vat_type):
    """Admin edit of a category's Xero account code + default VAT type.

    `xero_code` is stored as given (stripped; empty -> NULL). `vat_type` is
    normalised to 'standard' or 'novat' (anything else -> NULL)."""
    code = (xero_code or '').strip() or None
    vt = vat_type if vat_type in ('standard', 'novat') else None
    conn = get_db()
    try:
        conn.execute(
            "UPDATE recon_categories SET xero_code = ?, vat_type = ? WHERE id = ?",
            (code, vt, cat_id))
        conn.commit()
    finally:
        conn.close()


def get_store_xero_map():
    """All stores with their Xero tracking name + cash (POS) code, for the
    admin Xero-setup page. Ordered by store name."""
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT name, xero_tracking_name, store_code, cash_sales_label "
            "FROM stores ORDER BY name")]
    finally:
        conn.close()


def get_store_tracking_name(store):
    """The Xero tracking option for a store (TrackingOption1), or None."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT xero_tracking_name FROM stores WHERE name = ?", (store,)).fetchone()
        return row['xero_tracking_name'] if row else None
    finally:
        conn.close()


def get_store_xero(store):
    """A store's Xero mapping: {tracking_name, store_code} (either may be None)."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT xero_tracking_name, store_code FROM stores WHERE name = ?",
            (store,)).fetchone()
        if row is None:
            return {'tracking_name': None, 'store_code': None}
        return {'tracking_name': row['xero_tracking_name'], 'store_code': row['store_code']}
    finally:
        conn.close()


# "argument not supplied" — distinct from None/'', which mean "clear the column".
_UNSET = object()


def set_store_xero(store, tracking_name, store_code=None, cash_sales_label=_UNSET):
    """Admin edit of a store's expense tracking + cash-split mappings.

    `cash_sales_label` defaults to a sentinel rather than None so a caller that
    only means to set the expense mapping doesn't silently wipe finance's
    cash-split label. Passing it explicitly (including '') still clears it."""
    tn = (tracking_name or '').strip() or None
    sc = (store_code or '').strip() or None
    conn = get_db()
    try:
        if cash_sales_label is _UNSET:
            conn.execute(
                "UPDATE stores SET xero_tracking_name=?, store_code=? WHERE name=?",
                (tn, sc, store))
        else:
            conn.execute(
                "UPDATE stores SET xero_tracking_name=?, store_code=?, cash_sales_label=? "
                "WHERE name=?",
                (tn, sc, (cash_sales_label or '').strip() or None, store))
        conn.commit()
    finally:
        conn.close()


def get_recon_expense_lines(store, year, month):
    """The store-month's EXPENSE entries as raw MJ-line dicts (one per entry),
    oldest first. Income / transfers (Banked) / adjustments are excluded — only
    real expenses go on the store-expenses manual journal.

    Each dict carries the gross (VAT-inclusive) amount in cents plus the
    category's Xero code + default VAT type; the route computes net/VAT and
    formats the Xero columns. Amounts are the absolute cash out."""
    conn = get_db()
    try:
        rows = []
        for r in _recon_entries(conn, store, year, month):
            if r['kind'] != 'expense':
                continue
            rows.append({
                'id': r['id'],
                'date': (r['entry_date'] or '')[:10],
                'category': r['description'],
                'note': r['note'] or '',
                'xero_code': r['xero_code'],
                'vat_type': r['vat_type'] or 'novat',
                'gross_cents': abs(r['amount_cents']),
            })
        return rows
    finally:
        conn.close()


def _recon_close_cents(conn, store, year, month):
    """Closing float (cents) for a store-month = opening + Σ(in − out)."""
    opening = _recon_opening_cents(conn, store, year, month)
    net = 0
    for r in _recon_entries(conn, store, year, month):
        net += r['amount_cents'] if r['direction'] == 'in' else -r['amount_cents']
    return opening + net


def _recon_opening_cents(conn, store, year, month):
    """Opening float (cents): the explicit value if set, otherwise the prior
    month's closing balance carried forward."""
    row = conn.execute(
        "SELECT opening_cents FROM cash_recon_opening WHERE store=? AND year=? AND month=?",
        (store, year, month)).fetchone()
    if row is not None:
        return row['opening_cents']
    py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
    # One level back: prior month's explicit opening (or 0) plus its entries.
    prow = conn.execute(
        "SELECT opening_cents FROM cash_recon_opening WHERE store=? AND year=? AND month=?",
        (store, py, pm)).fetchone()
    prior_open = prow['opening_cents'] if prow is not None else 0
    net = 0
    for r in _recon_entries(conn, store, py, pm):
        net += r['amount_cents'] if r['direction'] == 'in' else -r['amount_cents']
    return prior_open + net


def get_recon_month(store, year, month):
    """Full ledger for a store-month: opening, each entry with its running
    balance, totals, and the closing balance. All values in rands."""
    conn = get_db()
    try:
        opening = _recon_opening_cents(conn, store, year, month)
        explicit = conn.execute(
            "SELECT 1 FROM cash_recon_opening WHERE store=? AND year=? AND month=?",
            (store, year, month)).fetchone() is not None
        running = opening
        total_in = total_out = 0
        rows = []
        for r in _recon_entries(conn, store, year, month):
            signed = r['amount_cents'] if r['direction'] == 'in' else -r['amount_cents']
            running += signed
            if r['direction'] == 'in':
                total_in += r['amount_cents']
            else:
                total_out += r['amount_cents']
            day_iso = (r['entry_date'] or '')[:10]
            try:
                day_label = date.fromisoformat(day_iso).strftime('%a %d %b')
            except ValueError:
                day_label = day_iso
            rows.append({
                'id': r['id'], 'date': r['entry_date'], 'description': r['description'],
                'day': day_iso, 'day_label': day_label,
                # category_id + cents identify a line exactly as the add form posted
                # it — the ledger template stamps them on the row so the browser can
                # tell an already-saved entry from an unsaved draft (cash-ledger.js).
                'category_id': r['category_id'], 'amount_cents': r['amount_cents'],
                'direction': r['direction'], 'note': r['note'], 'xero_code': r['xero_code'],
                'kind': r['kind'], 'receipt_id': r['receipt_id'], 'created_by': r['created_by'],
                'income': money.to_rands(r['amount_cents']) if r['direction'] == 'in' else None,
                'expense': money.to_rands(r['amount_cents']) if r['direction'] == 'out' else None,
                'running': money.to_rands(running),
            })
        return {
            'opening': money.to_rands(opening), 'opening_explicit': explicit,
            'total_in': money.to_rands(total_in), 'total_out': money.to_rands(total_out),
            'closing': money.to_rands(running), 'entries': rows,
        }
    finally:
        conn.close()


def add_recon_entry(store, entry_date, category_id, amount_rands, note, created_by, direction=None):
    """Add a ledger line. Direction defaults from the category's kind (income →
    in, everything else → out) unless explicitly overridden."""
    conn = get_db()
    try:
        cat = conn.execute(
            "SELECT name, kind FROM recon_categories WHERE id = ?", (category_id,)).fetchone()
        if cat is None:
            raise ValueError("Unknown category")
        if direction not in ('in', 'out'):
            direction = 'in' if cat['kind'] == 'income' else 'out'
        conn.execute(
            "INSERT INTO cash_recon_entries "
            "(store, entry_date, category_id, description, direction, amount_cents, note, created_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (store, entry_date, category_id, cat['name'], direction,
             money.to_cents(abs(amount_rands)), (note or '').strip() or None, created_by))
        conn.commit()
    finally:
        conn.close()


def set_recon_opening(store, year, month, opening_rands):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO cash_recon_opening (store, year, month, opening_cents) VALUES (?,?,?,?) "
            "ON CONFLICT(store, year, month) DO UPDATE SET opening_cents = excluded.opening_cents",
            (store, year, month, money.to_cents(opening_rands)))
        conn.commit()
    finally:
        conn.close()


def get_recon_entry_store(entry_id):
    """The store a cash-recon entry belongs to, or None if it doesn't exist.
    Used to scope deletes to the entry's real owner (never the posted field)."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT store FROM cash_recon_entries WHERE id = ?", (entry_id,)).fetchone()
        return row['store'] if row else None
    finally:
        conn.close()


def delete_recon_entry(entry_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM cash_recon_entries WHERE id = ?", (entry_id,))
        conn.commit()
    finally:
        conn.close()


def update_recon_entry(entry_id, entry_date, category_id, amount, note):
    """Edit a ledger line (admin only). Re-snapshots the description + direction
    from the (possibly changed) category, writes amount_cents as a positive
    magnitude, and stores the note. Targets the real cash_recon_entries table
    (never a view). Amounts are Rands in / cents out."""
    conn = get_db()
    try:
        cat = conn.execute(
            "SELECT name, kind FROM recon_categories WHERE id = ?", (category_id,)).fetchone()
        if cat is None:
            raise ValueError("Unknown category")
        direction = 'in' if cat['kind'] == 'income' else 'out'
        conn.execute(
            "UPDATE cash_recon_entries SET entry_date=?, category_id=?, description=?, "
            "direction=?, amount_cents=?, note=? WHERE id=?",
            (entry_date, category_id, cat['name'], direction,
             money.to_cents(abs(amount)), (note or '').strip() or None, entry_id))
        conn.commit()
    finally:
        conn.close()


def _bucket_by_kind(kind, amount_cents, buckets):
    """Add a positive magnitude into the right per-kind bucket dict in place."""
    if kind == 'income':
        buckets['in'] += amount_cents
    elif kind == 'expense':
        buckets['expense'] += amount_cents
    elif kind == 'transfer':
        buckets['banked'] += amount_cents
    elif kind == 'adjustment':
        buckets['adjust'] += amount_cents


def get_recon_overview(year, month):
    """All-stores overview for a month: one dict per store (sorted by name) with
    per-kind totals and the closing float. Built for an eventual Shopify 1:1
    balance compare (the closing float is the number to reconcile against). All
    money in Rands."""
    conn = get_db()
    try:
        out = []
        for store in get_stores():
            opening = _recon_opening_cents(conn, store, year, month)
            buckets = {'in': 0, 'expense': 0, 'banked': 0, 'adjust': 0}
            net = 0
            count = 0
            for r in _recon_entries(conn, store, year, month):
                count += 1
                amt = r['amount_cents']
                net += amt if r['direction'] == 'in' else -amt
                _bucket_by_kind(r['kind'], amt, buckets)
            closing = opening + net
            out.append({
                'store': store,
                'opening': money.to_rands(opening),
                'total_in': money.to_rands(buckets['in']),
                'total_expense': money.to_rands(buckets['expense']),
                'total_banked': money.to_rands(buckets['banked']),
                'total_adjust': money.to_rands(buckets['adjust']),
                'closing': money.to_rands(closing),
                'entry_count': count,
            })
        out.sort(key=lambda d: d['store'])
        return out
    finally:
        conn.close()


def get_recon_daily_summary(store, year, month):
    """Per-day roll-up for a store-month. `day_opening` is the running float at
    the START of that day, `day_closing` at the END; the float carries through
    days with no entries (only days that have entries get rows). All money in
    Rands."""
    conn = get_db()
    try:
        opening = _recon_opening_cents(conn, store, year, month)
        running = opening
        totals = {'in': 0, 'expense': 0, 'banked': 0, 'adjust': 0}
        days = {}
        order = []
        for r in _recon_entries(conn, store, year, month):
            d = r['entry_date']
            if d not in days:
                days[d] = {'date': d, 'day_opening_cents': running,
                           'buckets': {'in': 0, 'expense': 0, 'banked': 0, 'adjust': 0},
                           'net': 0, 'count': 0}
                order.append(d)
            amt = r['amount_cents']
            signed = amt if r['direction'] == 'in' else -amt
            running += signed
            day = days[d]
            day['net'] += signed
            day['count'] += 1
            _bucket_by_kind(r['kind'], amt, day['buckets'])
            _bucket_by_kind(r['kind'], amt, totals)
        day_rows = []
        for d in sorted(order):
            day = days[d]
            day_open = day['day_opening_cents']
            day_close = day_open + day['net']
            b = day['buckets']
            day_rows.append({
                'date': d,
                'day_opening': money.to_rands(day_open),
                'in': money.to_rands(b['in']),
                'expense': money.to_rands(b['expense']),
                'banked': money.to_rands(b['banked']),
                'adjust': money.to_rands(b['adjust']),
                'day_closing': money.to_rands(day_close),
                'count': day['count'],
            })
        return {
            'opening': money.to_rands(opening),
            'closing': money.to_rands(running),
            'total_in': money.to_rands(totals['in']),
            'total_expense': money.to_rands(totals['expense']),
            'total_banked': money.to_rands(totals['banked']),
            'total_adjust': money.to_rands(totals['adjust']),
            'days': day_rows,
        }
    finally:
        conn.close()


def get_recon_day(store, date):
    """A single day's entries with the running month float. `date` is an ISO
    'YYYY-MM-DD' string. Entry shape matches get_recon_month entries; `running`
    is the running float within the whole month up to and including that entry
    (consistent with the ledger). All money in Rands."""
    conn = get_db()
    try:
        year, mon = int(date[0:4]), int(date[5:7])
        opening = _recon_opening_cents(conn, store, year, mon)
        running = opening
        day_opening_cents = opening
        day_closing_cents = None
        set_open = False
        totals = {'in': 0, 'expense': 0, 'banked': 0, 'adjust': 0}
        entries = []
        for r in _recon_entries(conn, store, year, mon):
            d = r['entry_date']
            # Opening float at the START of the target day = running just before
            # the first entry dated on-or-after that day.
            if not set_open and d >= date:
                day_opening_cents = running
                set_open = True
            amt = r['amount_cents']
            signed = amt if r['direction'] == 'in' else -amt
            running += signed
            if d == date:
                day_closing_cents = running
                _bucket_by_kind(r['kind'], amt, totals)
                entries.append({
                    'id': r['id'], 'date': r['entry_date'],
                    'description': r['description'], 'note': r['note'],
                    'kind': r['kind'],
                    'income': money.to_rands(amt) if r['direction'] == 'in' else None,
                    'expense': money.to_rands(amt) if r['direction'] == 'out' else None,
                    'running': money.to_rands(running),
                })
        # No entry ever reached on-or-after the target day -> it sits after every
        # entry, so its opening float is the month's running close.
        if not set_open:
            day_opening_cents = running
        if day_closing_cents is None:
            day_closing_cents = day_opening_cents
        return {
            'date': date,
            'day_opening': money.to_rands(day_opening_cents),
            'day_closing': money.to_rands(day_closing_cents),
            'total_in': money.to_rands(totals['in']),
            'total_expense': money.to_rands(totals['expense']),
            'total_banked': money.to_rands(totals['banked']),
            'entries': entries,
        }
    finally:
        conn.close()


def _recon_entries_range(conn, store, start, end):
    """Raw entries for a store across an INCLUSIVE ISO date range, oldest first.
    'YYYY-MM-DD' strings sort chronologically so plain >=/<= works."""
    return conn.execute(
        "SELECT e.id, e.entry_date, e.description, e.direction, e.amount_cents, "
        "       e.note, e.receipt_id, e.created_by, c.xero_code, c.kind "
        "FROM cash_recon_entries e LEFT JOIN recon_categories c ON c.id = e.category_id "
        "WHERE e.store = ? AND e.entry_date >= ? AND e.entry_date <= ? "
        "ORDER BY e.entry_date, e.id",
        (store, start, end)).fetchall()


def _recon_balance_before_cents(conn, store, date):
    """Running float immediately BEFORE the given ISO date = the opening float of
    that date's month plus the net of that month's entries dated before it. This
    is the correct opening balance for a range that starts on `date`."""
    year, mon = int(date[0:4]), int(date[5:7])
    base = _recon_opening_cents(conn, store, year, mon)
    month_start = f"{year:04d}-{mon:02d}-01"
    net = 0
    for r in conn.execute(
            "SELECT direction, amount_cents FROM cash_recon_entries "
            "WHERE store = ? AND entry_date >= ? AND entry_date < ?",
            (store, month_start, date)):
        net += r['amount_cents'] if r['direction'] == 'in' else -r['amount_cents']
    return base + net


def _chunk_marks(names):
    """(placeholders, params) for an IN clause. SQLite's limit is ~999 variables;
    store lists are far below it, so a single chunk is always enough here."""
    return ','.join('?' * len(names)), list(names)


def _recon_openings_batch(conn, scope, start):
    """Explicit month openings for every store, for `start`'s month and the one
    before it, in ONE query. Mirrors the two lookups in _recon_opening_cents."""
    year, mon = int(start[0:4]), int(start[5:7])
    py, pm = (year - 1, 12) if mon == 1 else (year, mon - 1)
    out = {'current': {}, 'prior': {}}
    if not scope:
        return out
    marks, params = _chunk_marks(scope)
    for r in conn.execute(
            "SELECT store, year, month, opening_cents FROM cash_recon_opening "
            f"WHERE store IN ({marks}) AND ((year=? AND month=?) OR (year=? AND month=?))",
            params + [year, mon, py, pm]):
        key = 'current' if (r['year'], r['month']) == (year, mon) else 'prior'
        out[key][r['store']] = r['opening_cents']
    return out


def _recon_prior_month_net_batch(conn, scope, start):
    """Net movement of the month BEFORE `start`'s month, per store, in ONE query.
    Mirrors the _recon_entries loop in _recon_opening_cents (same YYYY-MM match)."""
    year, mon = int(start[0:4]), int(start[5:7])
    py, pm = (year - 1, 12) if mon == 1 else (year, mon - 1)
    out = {}
    if not scope:
        return out
    marks, params = _chunk_marks(scope)
    for r in conn.execute(
            "SELECT store, direction, amount_cents FROM cash_recon_entries "
            f"WHERE store IN ({marks}) AND substr(entry_date,1,7) = ?",
            params + [f"{py:04d}-{pm:02d}"]):
        amt = r['amount_cents'] if r['direction'] == 'in' else -r['amount_cents']
        out[r['store']] = out.get(r['store'], 0) + amt
    return out


def _recon_month_to_date_net_batch(conn, scope, start):
    """Net of entries in [month_start, start), per store, in ONE query. Mirrors
    the second half of _recon_balance_before_cents."""
    year, mon = int(start[0:4]), int(start[5:7])
    month_start = f"{year:04d}-{mon:02d}-01"
    out = {}
    if not scope:
        return out
    marks, params = _chunk_marks(scope)
    for r in conn.execute(
            "SELECT store, direction, amount_cents FROM cash_recon_entries "
            f"WHERE store IN ({marks}) AND entry_date >= ? AND entry_date < ?",
            params + [month_start, start]):
        amt = r['amount_cents'] if r['direction'] == 'in' else -r['amount_cents']
        out[r['store']] = out.get(r['store'], 0) + amt
    return out


def _recon_entries_range_batch(conn, scope, start, end):
    """Range entries grouped by store, in ONE query. Same columns, filter and
    ordering as _recon_entries_range so per-store results are identical."""
    out = {}
    if not scope:
        return out
    marks, params = _chunk_marks(scope)
    for r in conn.execute(
            "SELECT e.store, e.id, e.entry_date, e.description, e.direction, e.amount_cents, "
            "       e.note, e.receipt_id, e.created_by, c.xero_code, c.kind "
            "FROM cash_recon_entries e LEFT JOIN recon_categories c ON c.id = e.category_id "
            f"WHERE e.store IN ({marks}) AND e.entry_date >= ? AND e.entry_date <= ? "
            "ORDER BY e.store, e.entry_date, e.id",
            params + [start, end]):
        out.setdefault(r['store'], []).append(r)
    return out


def get_recon_overview_range(start, end, stores=None):
    """All-stores overview for an INCLUSIVE [start, end] date range. `opening` is
    the running float just before `start`; `closing` = opening + net over the
    range. Per-kind buckets are magnitudes. One dict per store, sorted by name.
    Money in Rands. (A running-balance view: explicit month openings set strictly
    inside the range are not re-applied — the float carries continuously.)

    `stores` optionally restricts the overview to a subset of store names (used by
    the Regional Manager dashboard to scope to an RM's assigned stores). When None
    (default) every store is included — existing callers are unaffected."""
    conn = get_db()
    try:
        out = []
        scope = stores if stores is not None else get_stores()
        # Prefetched across ALL stores rather than five statements per store: this
        # page issued 162 for 31 stores and grows with the estate. The arithmetic
        # below is unchanged — only the number of round trips is. Each helper's
        # per-store form is kept for single-store callers.
        openings = _recon_openings_batch(conn, scope, start)
        prior_net = _recon_prior_month_net_batch(conn, scope, start)
        before_net = _recon_month_to_date_net_batch(conn, scope, start)
        ranged = _recon_entries_range_batch(conn, scope, start, end)
        for store in scope:
            # _recon_balance_before_cents, inlined over prefetched data: the
            # month's explicit opening if set, else the prior month's carried
            # closing, plus this month's entries dated before `start`.
            base = openings['current'].get(store)
            if base is None:
                base = openings['prior'].get(store, 0) + prior_net.get(store, 0)
            opening = base + before_net.get(store, 0)
            buckets = {'in': 0, 'expense': 0, 'banked': 0, 'adjust': 0}
            net = 0
            count = 0
            for r in ranged.get(store, ()):
                count += 1
                amt = r['amount_cents']
                net += amt if r['direction'] == 'in' else -amt
                _bucket_by_kind(r['kind'], amt, buckets)
            out.append({
                'store': store,
                'opening': money.to_rands(opening),
                'total_in': money.to_rands(buckets['in']),
                'total_expense': money.to_rands(buckets['expense']),
                'total_banked': money.to_rands(buckets['banked']),
                'total_adjust': money.to_rands(buckets['adjust']),
                'closing': money.to_rands(opening + net),
                'entry_count': count,
            })
        out.sort(key=lambda d: d['store'])
        return out
    finally:
        conn.close()


def get_recon_range(store, start, end):
    """Per-day breakdown for ONE store across [start, end], each day carrying its
    own entries for inline (read-only) drill-down. `opening` = balance before
    `start`; only days with entries get rows. Money in Rands."""
    conn = get_db()
    try:
        opening = _recon_balance_before_cents(conn, store, start)
        running = opening
        totals = {'in': 0, 'expense': 0, 'banked': 0, 'adjust': 0}
        days = {}
        order = []
        for r in _recon_entries_range(conn, store, start, end):
            d = r['entry_date']
            if d not in days:
                days[d] = {'date': d, 'day_opening_cents': running,
                           'buckets': {'in': 0, 'expense': 0, 'banked': 0, 'adjust': 0},
                           'net': 0, 'count': 0, 'entries': []}
                order.append(d)
            amt = r['amount_cents']
            signed = amt if r['direction'] == 'in' else -amt
            running += signed
            day = days[d]
            day['net'] += signed
            day['count'] += 1
            _bucket_by_kind(r['kind'], amt, day['buckets'])
            _bucket_by_kind(r['kind'], amt, totals)
            day['entries'].append({
                'id': r['id'], 'date': r['entry_date'],
                'description': r['description'], 'note': r['note'], 'kind': r['kind'],
                'income': money.to_rands(amt) if r['direction'] == 'in' else None,
                'expense': money.to_rands(amt) if r['direction'] == 'out' else None,
                'running': money.to_rands(running),
            })
        day_rows = []
        for d in sorted(order):
            day = days[d]
            day_open = day['day_opening_cents']
            b = day['buckets']
            day_rows.append({
                'date': d,
                'day_opening': money.to_rands(day_open),
                'in': money.to_rands(b['in']),
                'expense': money.to_rands(b['expense']),
                'banked': money.to_rands(b['banked']),
                'adjust': money.to_rands(b['adjust']),
                'day_closing': money.to_rands(day_open + day['net']),
                'count': day['count'],
                'entries': day['entries'],
            })
        return {
            'opening': money.to_rands(opening),
            'closing': money.to_rands(running),
            'total_in': money.to_rands(totals['in']),
            'total_expense': money.to_rands(totals['expense']),
            'total_banked': money.to_rands(totals['banked']),
            'total_adjust': money.to_rands(totals['adjust']),
            'days': day_rows,
        }
    finally:
        conn.close()


def get_recon_category_breakdown(stores, start, end):
    """Per-category totals across `stores` over the INCLUSIVE [start, end] range.

    One dict per (category) that has any entries: {name, kind, xero_code, total}
    where `total` is a positive magnitude in Rands. Rows keep their `kind`
    ('income'/'expense'/'transfer'/'adjustment') so a caller can hide income rows
    when the cash-sales toggle is off. Sorted by kind then name. `stores` is the
    list the caller has already scoped (e.g. an RM's assigned stores); an empty
    list yields no rows."""
    conn = get_db()
    try:
        agg = {}
        for store in (stores or []):
            for r in _recon_entries_range(conn, store, start, end):
                key = (r['description'], r['kind'], r['xero_code'])
                agg[key] = agg.get(key, 0) + r['amount_cents']
        rows = [{'name': name, 'kind': kind, 'xero_code': xero_code,
                 'total': money.to_rands(cents)}
                for (name, kind, xero_code), cents in agg.items()]
        # Stable, readable order: group by kind, then by category name.
        _kind_order = {'income': 0, 'expense': 1, 'transfer': 2, 'adjustment': 3}
        rows.sort(key=lambda d: (_kind_order.get(d['kind'], 9), (d['name'] or '')))
        return rows
    finally:
        conn.close()


def get_recon_category_store_breakdown(stores, start, end):
    """Expense-category totals split across an already-scoped store list.

    Returns one row per category as ``{name, xero_code, total, stores}``, where
    ``stores`` includes every requested store (including zero-value stores) as
    ``{store, total}``. Aggregation stays in integer cents until the return
    boundary so the regional drill-down always ties to the cash ledger.
    """
    scoped_stores = list(dict.fromkeys(stores or []))
    if not scoped_stores:
        return []
    conn = get_db()
    try:
        categories = {}
        for store in scoped_stores:
            for row in _recon_entries_range(conn, store, start, end):
                if row['kind'] != 'expense':
                    continue
                name = row['description'] or '(Uncategorised)'
                key = (name, row['xero_code'] or '')
                item = categories.setdefault(key, {
                    'name': name, 'xero_code': row['xero_code'],
                    'total_cents': 0, 'store_cents': {s: 0 for s in scoped_stores},
                })
                cents = int(row['amount_cents'] or 0)
                item['total_cents'] += cents
                item['store_cents'][store] += cents

        result = []
        for item in categories.values():
            store_rows = [
                {'store': store, 'total': money.to_rands(item['store_cents'][store])}
                for store in scoped_stores
            ]
            store_rows.sort(key=lambda row: (-row['total'], row['store'].lower()))
            result.append({
                'name': item['name'], 'xero_code': item['xero_code'],
                'total': money.to_rands(item['total_cents']), 'stores': store_rows,
            })
        result.sort(key=lambda row: (-row['total'], row['name'].lower()))
        return result
    finally:
        conn.close()


def get_recon_cumulative_range(stores, start, end):
    """Cumulative cash summary across `stores` over the INCLUSIVE [start, end]
    range. Opening = Σ each store's balance just before `start`; closing = Σ each
    store's closing. Per-kind buckets are magnitudes. All summed in integer cents
    (no float math) and returned in Rands. `stores` is the already-scoped list."""
    conn = get_db()
    try:
        opening = 0
        buckets = {'in': 0, 'expense': 0, 'banked': 0, 'adjust': 0}
        net = 0
        for store in (stores or []):
            opening += _recon_balance_before_cents(conn, store, start)
            for r in _recon_entries_range(conn, store, start, end):
                amt = r['amount_cents']
                net += amt if r['direction'] == 'in' else -amt
                _bucket_by_kind(r['kind'], amt, buckets)
        return {
            'opening': money.to_rands(opening),
            'total_in': money.to_rands(buckets['in']),
            'total_expense': money.to_rands(buckets['expense']),
            'total_banked': money.to_rands(buckets['banked']),
            'total_adjust': money.to_rands(buckets['adjust']),
            'closing': money.to_rands(opening + net),
        }
    finally:
        conn.close()


def get_recon_activity_summary(stores, start, end):
    """Freshness and exception metadata for an already-scoped store list.

    This is deliberately read-only and contains no income totals, so it is safe
    for the RM dashboard even while the cash-sales visibility toggle is off.
    Dates are ISO strings; callers format them for display at the boundary.
    """
    scope = list(stores or [])
    if not scope:
        return {
            'latest_entry_date': None, 'latest_created_at': None,
            'latest_banking_date': None, 'adjustment_count': 0,
            'adjustment_total': 0.0, 'by_store': {},
        }
    conn = get_db()
    try:
        marks = ','.join('?' for _ in scope)
        rows = conn.execute(
            f"SELECT e.store, MAX(e.entry_date) AS latest_entry_date, "
            f"MAX(e.created_at) AS latest_created_at, "
            f"MAX(CASE WHEN c.kind='transfer' THEN e.entry_date END) AS latest_banking_date, "
            f"SUM(CASE WHEN c.kind='adjustment' THEN 1 ELSE 0 END) AS adjustment_count, "
            f"SUM(CASE WHEN c.kind='adjustment' THEN e.amount_cents ELSE 0 END) AS adjustment_cents "
            f"FROM cash_recon_entries e "
            f"LEFT JOIN recon_categories c ON c.id=e.category_id "
            f"WHERE e.store IN ({marks}) AND e.entry_date>=? AND e.entry_date<=? "
            f"GROUP BY e.store",
            (*scope, start, end)).fetchall()
        by_store = {}
        for r in rows:
            by_store[r['store']] = {
                'latest_entry_date': r['latest_entry_date'],
                'latest_created_at': r['latest_created_at'],
                'latest_banking_date': r['latest_banking_date'],
                'adjustment_count': int(r['adjustment_count'] or 0),
                'adjustment_total': money.to_rands(r['adjustment_cents'] or 0),
            }
        return {
            'latest_entry_date': max(
                (r['latest_entry_date'] for r in rows if r['latest_entry_date']),
                default=None),
            'latest_created_at': max(
                (r['latest_created_at'] for r in rows if r['latest_created_at']),
                default=None),
            'latest_banking_date': max(
                (r['latest_banking_date'] for r in rows if r['latest_banking_date']),
                default=None),
            'adjustment_count': sum(int(r['adjustment_count'] or 0) for r in rows),
            'adjustment_total': money.to_rands(sum(r['adjustment_cents'] or 0 for r in rows)),
            'by_store': by_store,
        }
    finally:
        conn.close()


# ── Regional Managers ────────────────────────────────────────────────────────
# Extracted to northwind/data/repositories/regional.py; re-exported at the bottom of
# this module so db.<name>() keeps working. (rm_users / rm_stores domain.)

# ── Credit Card Reconciliation ───────────────────────────────────────────────
# Cardholders are auto-provisioned by importing their Xero recon export
# (credit_card_parser.CardSnapshot). Re-import is an idempotent merge keyed on
# (statement_id, fingerprint, occurrence): genuine repeats are preserved,
# duplicates are never created, and receipts already attached are never wiped.

def import_card_snapshot(snap):
    """Merge one parsed CardSnapshot into the DB. Returns a summary dict.

    - upserts the card (identity = card_name) and the statement (card + month);
    - inserts new lines, updates existing ones in place (keeping their receipt);
    - any previously-outstanding line absent from this upload is marked
      'cleared' (reconciled/removed in Xero) but keeps its receipt for audit.
    """
    ref_date = snap.as_at or snap.period_end or snap.period_start
    if ref_date is None:
        from datetime import date as _date
        ref_date = _date.today()
    year, month = ref_date.year, ref_date.month

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM cc_cards WHERE card_name = ?", (snap.card_name,)).fetchone()
        if row:
            card_id = row['id']
            created = False
            conn.execute(
                "UPDATE cc_cards SET display_name = COALESCE(display_name, ?) WHERE id = ?",
                (snap.display_name, card_id))
        else:
            cur = conn.execute(
                "INSERT INTO cc_cards (card_name, display_name) VALUES (?, ?)",
                (snap.card_name, snap.display_name))
            card_id = cur.lastrowid
            created = True

        iso = lambda d: d.isoformat() if d else None

        def _month_bounds(y, m):
            from datetime import date as _d, timedelta
            start = _d(y, m, 1)
            nxt = _d(y + (1 if m == 12 else 0), (m % 12) + 1, 1)
            return start, nxt - timedelta(days=1)

        # The PRIMARY statement is the file's own period (as_at / period end).
        # It owns the file-level metadata (period, as_at, source, dup count).
        conn.execute(
            "INSERT INTO cc_statements "
            "(card_id, year, month, period_start, period_end, as_at, source_filename, "
            " duplicates_removed_by_xero, imported_at) "
            "VALUES (?,?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(card_id, year, month) DO UPDATE SET "
            "  period_start=excluded.period_start, period_end=excluded.period_end, "
            "  as_at=excluded.as_at, source_filename=excluded.source_filename, "
            "  duplicates_removed_by_xero=excluded.duplicates_removed_by_xero, "
            "  imported_at=datetime('now')",
            (card_id, year, month, iso(snap.period_start), iso(snap.period_end),
             iso(snap.as_at), snap.source_filename, snap.duplicates_removed_by_xero))
        primary_sid = conn.execute(
            "SELECT id FROM cc_statements WHERE card_id=? AND year=? AND month=?",
            (card_id, year, month)).fetchone()['id']
        stmt_ids = {(year, month): primary_sid}

        def _statement_for(y, m):
            """Statement id for (y, m). An 'as at' recon lists lines carried over
            from earlier months; each is filed under the month it actually
            occurred so it merges with that month (no cross-month duplicates).
            A carried-over month is created if missing but NEVER has its own
            metadata clobbered by this file."""
            sid = stmt_ids.get((y, m))
            if sid is not None:
                return sid
            s, e = _month_bounds(y, m)
            conn.execute(
                "INSERT INTO cc_statements (card_id, year, month, period_start, "
                "period_end, imported_at) VALUES (?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(card_id, year, month) DO NOTHING",
                (card_id, y, m, iso(s), iso(e)))
            sid = conn.execute(
                "SELECT id FROM cc_statements WHERE card_id=? AND year=? AND month=?",
                (card_id, y, m)).fetchone()['id']
            stmt_ids[(y, m)] = sid
            return sid

        incoming_by_stmt = {}
        new = updated = 0
        for l in snap.lines:
            # Bank transfers / money-in (card funding, balance carry-overs) are
            # not merchant spend and those amounts should not live on
            # the app — never store them, and purge any that were stored before.
            if l.category == 'transfer':
                continue
            d = l.line_date
            sid = _statement_for(d.year, d.month) if d else primary_sid
            incoming_by_stmt.setdefault(sid, set()).add((l.fingerprint, l.occurrence))
            needs = 1 if (l.category == 'spend' and not l.reconciled) else 0
            status = 'cleared' if l.reconciled else 'outstanding'
            # Belt-and-braces: never let a card number land in a stored reference,
            # even if a bank labels a line oddly. Keeps only the last four digits.
            ref = scrub.mask_pans(l.reference)
            existing = conn.execute(
                "SELECT id FROM cc_lines WHERE statement_id=? AND fingerprint=? AND occurrence=?",
                (sid, l.fingerprint, l.occurrence)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE cc_lines SET line_date=?, reference=?, amount_cents=?, category=?, "
                    "reconciled=?, needs_receipt=?, status=?, last_seen_at=datetime('now') WHERE id=?",
                    (iso(l.line_date), ref, l.amount_cents, l.category,
                     1 if l.reconciled else 0, needs, status, existing['id']))
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO cc_lines (statement_id, card_id, line_date, reference, amount_cents, "
                    "category, reconciled, needs_receipt, status, fingerprint, occurrence) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, card_id, iso(l.line_date), ref, l.amount_cents,
                     l.category, 1 if l.reconciled else 0, needs, status, l.fingerprint, l.occurrence))
                new += 1

        # Purge any transfer lines stored by an earlier import in the statements
        # we touched — those amounts must not live on the app. Drop receipt links
        # first (defensive; transfers never carry receipts).
        touched = list(stmt_ids.values())
        ph = ",".join("?" * len(touched))
        conn.execute(
            f"DELETE FROM cc_receipt_lines WHERE line_id IN "
            f"(SELECT id FROM cc_lines WHERE statement_id IN ({ph}) AND category='transfer')",
            touched)
        conn.execute(
            f"DELETE FROM cc_lines WHERE statement_id IN ({ph}) AND category='transfer'",
            touched)

        # Clear lines that were outstanding but are absent from this upload
        # (reconciled/removed in Xero). Two rules make this safe:
        #   • NEVER clear a line that already has a receipt attached — those stay
        #     visible (lines with an attached receipt stay visible). A cleared line
        #     keeps its row + receipt regardless, but we don't even move these.
        #   • An "as at" reconciliation lists EVERY still-outstanding line as of
        #     that date, so it is the complete truth for the whole card → clear
        #     anything outstanding it doesn't list, across all months. A plain
        #     single-month export is only complete for its OWN month, so it only
        #     clears within the primary month (carried-over months are add-only).
        def _has_receipt(line_id):
            return conn.execute(
                "SELECT 1 FROM cc_receipt_lines WHERE line_id=? LIMIT 1",
                (line_id,)).fetchone() is not None

        cleared = 0
        if snap.as_at is not None:
            incoming_all = set()
            for fps in incoming_by_stmt.values():
                incoming_all |= fps
            targets = conn.execute(
                "SELECT id, fingerprint, occurrence FROM cc_lines "
                "WHERE card_id=? AND status='outstanding'", (card_id,)).fetchall()
        else:
            incoming_all = incoming_by_stmt.get(primary_sid, set())
            targets = conn.execute(
                "SELECT id, fingerprint, occurrence FROM cc_lines "
                "WHERE statement_id=? AND status='outstanding'", (primary_sid,)).fetchall()
        for r in targets:
            if (r['fingerprint'], r['occurrence']) in incoming_all:
                continue
            if _has_receipt(r['id']):
                continue
            conn.execute(
                "UPDATE cc_lines SET status='cleared', needs_receipt=0, reconciled=1, "
                "last_seen_at=datetime('now') WHERE id=?", (r['id'],))
            cleared += 1

        conn.commit()
        # Spends still needing a receipt — same predicate as list_cc_cards:
        # coverage is via the cc_receipt_lines join table (cc_lines.receipt_id is
        # the abandoned 1:1 column from before migration 0015), personal charges
        # don't need a receipt, and a line already reconciled in Xero is DONE
        # (the card working view hides reconciled lines, so the tile must too).
        outstanding = conn.execute(
            "SELECT COUNT(*) c FROM cc_lines l WHERE l.card_id=? AND l.needs_receipt=1 "
            "AND l.status='outstanding' AND l.personal=0 AND l.xero_reconciled=0 "
            "AND NOT EXISTS (SELECT 1 FROM cc_receipt_lines rl WHERE rl.line_id=l.id)",
            (card_id,)).fetchone()['c']
        return {'card_id': card_id, 'card_name': snap.card_name,
                'display_name': snap.display_name, 'created': created,
                'year': year, 'month': month, 'lines_new': new,
                'lines_updated': updated, 'lines_cleared': cleared,
                'receipts_outstanding': outstanding}
    finally:
        conn.close()


def list_cc_cards(active_only=True):
    """Cards plus task counts for the operations landing page."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT c.id, c.card_name, c.display_name, c.active, "
            "  (SELECT COUNT(*) FROM cc_lines l WHERE l.card_id=c.id AND l.needs_receipt=1 "
            "     AND l.status='outstanding' AND l.personal=0 AND l.xero_reconciled=0 "
            "     AND NOT EXISTS (SELECT 1 FROM cc_receipt_lines rl WHERE rl.line_id=l.id)"
            "  ) AS receipts_outstanding, "
            "  (SELECT COUNT(*) FROM cc_card_users u WHERE u.card_id=c.id) AS user_count "
            ", (SELECT COUNT(*) FROM cc_lines l WHERE l.card_id=c.id "
            "     AND l.category='spend' AND l.status='outstanding' "
            "     AND l.xero_reconciled=0 AND l.submitted_at IS NULL "
            "     AND TRIM(COALESCE(l.reason,''))<>'' "
            "     AND (l.personal=1 OR EXISTS "
            "          (SELECT 1 FROM cc_receipt_lines rl WHERE rl.line_id=l.id))"
            "  ) AS ready_to_submit "
            ", (SELECT COUNT(*) FROM cc_lines l WHERE l.card_id=c.id "
            "     AND l.category='spend' AND l.status='outstanding' "
            "     AND l.personal=0 AND l.xero_reconciled=0 "
            "     AND l.xero_account_code IS NULL"
            "  ) AS coding_missing "
            ", (SELECT COUNT(*) FROM cc_receipts r WHERE r.card_id=c.id "
            "     AND r.statement_id IS NULL) AS inbox_count "
            ", (SELECT COUNT(*) FROM cc_receipts r WHERE r.card_id=c.id "
            "     AND r.ai_status IN ('failed','unreadable')) AS ai_failed_count "
            "FROM cc_cards c " + ("WHERE c.active=1 " if active_only else "") +
            "ORDER BY c.display_name COLLATE NOCASE, c.card_name COLLATE NOCASE").fetchall()
    finally:
        conn.close()


def get_cc_card(card_id):
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM cc_cards WHERE id=?", (card_id,)).fetchone()
    finally:
        conn.close()


def set_cc_card_display_name(card_id, display_name):
    """Edit the friendly label without changing the stable Xero card identity."""
    name = (display_name or '').strip()
    conn = get_db()
    try:
        conn.execute("UPDATE cc_cards SET display_name=? WHERE id=?",
                     (name or None, card_id))
        conn.commit()
    finally:
        conn.close()


def set_cc_card_active(card_id, active):
    conn = get_db()
    try:
        conn.execute("UPDATE cc_cards SET active=? WHERE id=?", (1 if active else 0, card_id))
        conn.commit()
    finally:
        conn.close()


def get_cc_card_lines(card_id, category=None, only_outstanding=False, statement_id=None,
                      date_from=None, date_to=None):
    """Statement lines for a card's detail view (newest period first). Pass
    `statement_id` to scope to a single month (the admin per-month workspace), or
    `date_from`/`date_to` (ISO 'YYYY-MM-DD') to scope by transaction date across
    ALL months (the admin date filter — line_date is stored ISO so string
    comparison is a correct date comparison)."""
    conn = get_db()
    try:
        sql = ("SELECT l.*, s.year, s.month FROM cc_lines l "
               "JOIN cc_statements s ON s.id = l.statement_id WHERE l.card_id=?")
        params = [card_id]
        if statement_id is not None:
            sql += " AND l.statement_id=?"
            params.append(statement_id)
        if category:
            sql += " AND l.category=?"
            params.append(category)
        if only_outstanding:
            sql += " AND l.status='outstanding'"
        if date_from:
            sql += " AND l.line_date >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND l.line_date <= ?"
            params.append(date_to)
        sql += " ORDER BY s.year DESC, s.month DESC, l.line_date, l.id"
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def list_cc_card_users(card_id):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT cu.*, u.id AS user_id, (u.id IS NOT NULL) AS has_login, "
            "COALESCE(u.is_active,0) AS is_active, "
            "EXISTS(SELECT 1 FROM user_roles ur WHERE ur.user_id=u.id) AS is_admin, "
            "EXISTS(SELECT 1 FROM rm_users rm WHERE rm.email=cu.email) AS is_rm, "
            "EXISTS(SELECT 1 FROM store_emails se WHERE se.email=cu.email) AS is_store_login, "
            "(SELECT COUNT(*) FROM cc_card_users allcu WHERE allcu.email=cu.email) AS card_count "
            "FROM cc_card_users cu LEFT JOIN users u ON u.login = cu.email "
            "WHERE cu.card_id=? ORDER BY cu.email COLLATE NOCASE",
            (card_id,)).fetchall()
    finally:
        conn.close()


def cc_card_user_has_access(card_id, email):
    """Whether ``email`` has an explicit access grant for this card."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT 1 FROM cc_card_users WHERE card_id=? AND email=? LIMIT 1",
            (card_id, (email or '').strip().lower())).fetchone() is not None
    finally:
        conn.close()


def add_cc_card_user(card_id, email, name=None, access_note=None):
    """Grant access to a card.

    General cardholders may access several cards. A person configured as an RM
    may hold exactly one; their assignment must be changed from the Regional
    Managers admin page so the replacement is explicit.
    """
    email = (email or '').strip().lower()
    conn = get_db()
    try:
        is_rm = conn.execute(
            "SELECT 1 FROM rm_users WHERE email = ? LIMIT 1", (email,)).fetchone()
        other = conn.execute(
            "SELECT c.id, COALESCE(c.display_name,c.card_name) AS name "
            "FROM cc_card_users u JOIN cc_cards c ON c.id=u.card_id "
            "WHERE u.email=? AND u.card_id<>? LIMIT 1", (email, card_id)).fetchone()
        if is_rm and other:
            raise ValueError(
                f'This Regional Manager already has {other["name"]}. '
                'Change their single card under Regional Managers.')
        conn.execute(
            "INSERT INTO cc_card_users (card_id, email, name, access_note) VALUES (?,?,?,?) "
            "ON CONFLICT(card_id, email) DO UPDATE SET "
            "  name=excluded.name, access_note=excluded.access_note",
            (card_id, email, (name or '').strip() or None,
             (access_note or '').strip() or None))
        conn.commit()
    finally:
        conn.close()


def delete_cc_card_user(user_id):
    """Revoke a cardholder's access to one card. If that was their last card,
    also delete their login so they can no longer sign in. Their uploaded
    receipts and the transactions stay (company expense/audit records)."""
    conn = get_db()
    try:
        row = conn.execute("SELECT email FROM cc_card_users WHERE id=?", (user_id,)).fetchone()
        with conn:
            conn.execute("DELETE FROM cc_card_users WHERE id=?", (user_id,))
            if row and not conn.execute(
                    "SELECT 1 FROM cc_card_users WHERE email=? LIMIT 1", (row['email'],)).fetchone():
                # Their last card is gone — drop the portal login, but never an
                # admin identity that happens to share this email.
                conn.execute(
                    "DELETE FROM users WHERE login=? AND NOT EXISTS "
                    "(SELECT 1 FROM user_roles r WHERE r.user_id = users.id) "
                    "AND NOT EXISTS (SELECT 1 FROM rm_users rm WHERE rm.email=?) "
                    "AND NOT EXISTS (SELECT 1 FROM store_emails se WHERE se.email=?)",
                    (row['email'], row['email'], row['email']))
    finally:
        conn.close()


def delete_cc_card(card_id):
    """Hard-delete a card and everything under it — statements, lines, receipts,
    receipt<->line links, AI suggestions, and access grants — plus any cardholder
    login left with no remaining card access. Returns the receipt file paths the
    caller should remove from storage (files live outside the DB)."""
    conn = get_db()
    try:
        files = [r['file_path'] for r in conn.execute(
            "SELECT file_path FROM cc_receipts WHERE card_id=?", (card_id,)).fetchall()]
        emails = [r['email'] for r in conn.execute(
            "SELECT email FROM cc_card_users WHERE card_id=?", (card_id,)).fetchall()]
        with conn:
            conn.execute("DELETE FROM cc_line_receipt_suggestions WHERE receipt_id IN "
                         "(SELECT id FROM cc_receipts WHERE card_id=?)", (card_id,))
            conn.execute("DELETE FROM cc_line_receipt_suggestions WHERE line_id IN "
                         "(SELECT id FROM cc_lines WHERE card_id=?)", (card_id,))
            conn.execute("DELETE FROM cc_receipt_lines WHERE receipt_id IN "
                         "(SELECT id FROM cc_receipts WHERE card_id=?)", (card_id,))
            conn.execute("DELETE FROM cc_receipts WHERE card_id=?", (card_id,))
            conn.execute("DELETE FROM cc_lines WHERE card_id=?", (card_id,))
            conn.execute("DELETE FROM cc_statements WHERE card_id=?", (card_id,))
            conn.execute("DELETE FROM cc_card_users WHERE card_id=?", (card_id,))
            conn.execute("DELETE FROM cc_cards WHERE id=?", (card_id,))
            for e in set(emails):
                if not conn.execute("SELECT 1 FROM cc_card_users WHERE email=? LIMIT 1", (e,)).fetchone():
                    # Drop the now-cardless portal login, but never an admin identity.
                    conn.execute(
                        "DELETE FROM users WHERE login=? AND NOT EXISTS "
                        "(SELECT 1 FROM user_roles r WHERE r.user_id = users.id) "
                        "AND NOT EXISTS (SELECT 1 FROM rm_users rm WHERE rm.email=?) "
                        "AND NOT EXISTS (SELECT 1 FROM store_emails se WHERE se.email=?)",
                        (e, e, e))
        return files
    finally:
        conn.close()


def find_cc_cards_for_email(email):
    """Cards a given email may access (for the cardholder portal login)."""
    email = (email or '').strip().lower()
    if not email:
        return []
    conn = get_db()
    try:
        return conn.execute(
            "SELECT c.* FROM cc_cards c JOIN cc_card_users u ON u.card_id=c.id "
            "WHERE u.email=? AND c.active=1 ORDER BY c.display_name", (email,)
        ).fetchall()
    finally:
        conn.close()


def get_cc_portal_task_count(email):
    """Incomplete transactions in each accessible card's newest statement.

    A card task is complete by the same rule as the portal UI: it has a reason
    and is either marked personal or has a linked receipt. Historical statement
    months do not keep a permanent badge lit; opening a card still exposes them.
    A transaction the admin has marked reconciled in Xero is off the
    cardholder's plate, so it never lights the badge either.
    """
    email = (email or '').strip().lower()
    if not email:
        return 0
    conn = get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM cc_lines l "
            "JOIN cc_cards c ON c.id=l.card_id AND c.active=1 "
            "JOIN cc_card_users u ON u.card_id=c.id AND u.email=? "
            "WHERE l.category='spend' AND l.status='outstanding' "
            "AND l.xero_reconciled=0 "
            "AND l.statement_id=(SELECT s.id FROM cc_statements s "
            "                    WHERE s.card_id=l.card_id "
            "                    ORDER BY s.year DESC, s.month DESC, s.id DESC LIMIT 1) "
            "AND NOT (TRIM(COALESCE(l.reason,''))<>'' AND "
            "         (l.personal=1 OR EXISTS (SELECT 1 FROM cc_receipt_lines rl "
            "                                   WHERE rl.line_id=l.id)))",
            (email,)).fetchone()[0]
    finally:
        conn.close()


# ── Credit Card — cardholder portal (month bucket of receipts) ───────────────

def get_cc_card_for_user(card_id, email):
    """The card row if `email` may access it, else None (portal access guard)."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT c.* FROM cc_cards c JOIN cc_card_users u ON u.card_id=c.id "
            "WHERE c.id=? AND u.email=? AND c.active=1",
            (card_id, (email or '').strip().lower())).fetchone()
    finally:
        conn.close()


def list_cc_statements(card_id):
    """All statement periods for a card, newest first (for the month switcher)."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM cc_statements WHERE card_id=? ORDER BY year DESC, month DESC",
            (card_id,)).fetchall()
    finally:
        conn.close()


def count_cc_outstanding_by_statement(card_id):
    """{statement_id: number of transactions still needing something} for a card.

    Answers "which month should the cardholder land on" in one query. Landing on
    the newest month hid real work: once finance loads August, July's
    outstanding transactions became invisible unless the cardholder thought to
    change the dropdown.

    The line filter matches get_cc_statement_lines(needing_receipts_only=True,
    exclude_reconciled=True) and the suggestion clause matches the portal's own
    AI-state query, so these counts agree with what the page renders rather than
    being a second, drifting definition of "outstanding".
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT s.id AS statement_id, COUNT(l.id) AS outstanding "
            "FROM cc_statements s "
            "LEFT JOIN cc_lines l ON l.statement_id=s.id "
            "     AND l.category='spend' AND l.status='outstanding' "
            "     AND l.xero_reconciled=0 "
            "     AND (COALESCE(l.reason,'')='' OR COALESCE(l.location,'')='' "
            "          OR l.vat_invoice_required=1 "
            "          OR (COALESCE(l.personal,0)=0 AND NOT EXISTS ("
            "                SELECT 1 FROM cc_receipt_lines rl WHERE rl.line_id=l.id)) "
            "          OR EXISTS (SELECT 1 FROM cc_line_receipt_suggestions sg "
            "                     WHERE sg.line_id=l.id AND sg.status='suggested' "
            "                       AND NOT EXISTS ("
            "                           SELECT 1 FROM cc_receipt_lines rl2 "
            "                           WHERE rl2.line_id=sg.line_id "
            "                             AND rl2.receipt_id=sg.receipt_id))) "
            "WHERE s.card_id=? GROUP BY s.id", (card_id,)).fetchall()
        return {r['statement_id']: r['outstanding'] for r in rows}
    finally:
        conn.close()


def list_cc_reminder_lines(card_id):
    """Every transaction on a card still waiting on its cardholder, oldest first.

    The chase list behind the reminder email. The WHERE clause is deliberately
    the same one as count_cc_outstanding_by_statement() — if the email listed
    transactions the portal considers done (or missed ones it is counting), the
    recipient would open the link and find a different story, and would rightly
    stop trusting the mail.

    Each row also carries WHY it is outstanding, so the email can say "receipt"
    or "reason" rather than an unhelpful "action needed".
    """
    conn = get_db()
    try:
        return conn.execute(
            "SELECT l.id, l.line_date, l.reference, l.amount_cents, "
            "       s.year, s.month, s.id AS statement_id, "
            "       (COALESCE(l.personal,0)=0 AND NOT EXISTS ("
            "           SELECT 1 FROM cc_receipt_lines rl WHERE rl.line_id=l.id)"
            "       ) AS needs_receipt, "
            "       (TRIM(COALESCE(l.reason,''))='') AS needs_reason, "
            "       (TRIM(COALESCE(l.location,''))='') AS needs_location, "
            "       (l.vat_invoice_required=1) AS needs_vat_invoice, "
            "       EXISTS (SELECT 1 FROM cc_line_receipt_suggestions sg "
            "               WHERE sg.line_id=l.id AND sg.status='suggested' "
            "                 AND NOT EXISTS ("
            "                     SELECT 1 FROM cc_receipt_lines rl2 "
            "                     WHERE rl2.line_id=sg.line_id "
            "                       AND rl2.receipt_id=sg.receipt_id)) AS has_suggestion "
            "FROM cc_lines l JOIN cc_statements s ON s.id=l.statement_id "
            "WHERE l.card_id=? AND l.category='spend' AND l.status='outstanding' "
            "  AND l.xero_reconciled=0 "
            "  AND (COALESCE(l.reason,'')='' OR COALESCE(l.location,'')='' "
            "       OR l.vat_invoice_required=1 "
            "       OR (COALESCE(l.personal,0)=0 AND NOT EXISTS ("
            "             SELECT 1 FROM cc_receipt_lines rl WHERE rl.line_id=l.id)) "
            "       OR EXISTS (SELECT 1 FROM cc_line_receipt_suggestions sg "
            "                  WHERE sg.line_id=l.id AND sg.status='suggested' "
            "                    AND NOT EXISTS ("
            "                        SELECT 1 FROM cc_receipt_lines rl2 "
            "                        WHERE rl2.line_id=sg.line_id "
            "                          AND rl2.receipt_id=sg.receipt_id))) "
            "ORDER BY s.year, s.month, l.line_date, l.id", (card_id,)).fetchall()
    finally:
        conn.close()


def get_cc_statement(statement_id):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM cc_statements WHERE id=?", (statement_id,)).fetchone()
    finally:
        conn.close()


def get_cc_statement_lines(statement_id, needing_receipts_only=False,
                           exclude_reconciled=False):
    """Lines for one month. Default: the cardholder checklist (spends needing a
    receipt). The bucket itself is month-wide, so this is just the to-gather list.

    ``exclude_reconciled`` drops the transactions the admin has marked done in
    Xero. Every cardholder-facing caller passes it, so a transaction the admin
    has closed off disappears from their checklist exactly as it does from the
    admin's working view."""
    conn = get_db()
    try:
        sql = ("SELECT * FROM cc_lines WHERE statement_id=? AND category='spend' "
               "AND status='outstanding'") if needing_receipts_only else \
              ("SELECT * FROM cc_lines WHERE statement_id=? AND category='spend'")
        if exclude_reconciled:
            sql += " AND xero_reconciled=0"
        sql += " ORDER BY line_date, id"
        return conn.execute(sql, (statement_id,)).fetchall()
    finally:
        conn.close()


def add_cc_receipt(card_id, statement_id, file_path, original_filename,
                   content_type, uploaded_by, content_hash=None):
    """Drop a receipt/invoice file into a month's bucket. Returns the new id."""
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO cc_receipts (card_id, statement_id, line_id, file_path, "
            "original_filename, content_type, uploaded_by, content_hash) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (card_id, statement_id, None, file_path, original_filename,
             content_type, uploaded_by, content_hash))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def add_and_link_cc_receipt(card_id, statement_id, line_id, file_path,
                            original_filename, content_type, uploaded_by,
                            content_hash=None, actor=None):
    """Create a receipt and its exact transaction link in one DB transaction.

    This is the direct-to-transaction upload boundary.  A successful return
    means the join row exists; a failure leaves neither a new receipt row nor a
    half-finished link.  A content-identical receipt already in this statement
    is re-used and linked idempotently instead of being duplicated.

    Blob storage is deliberately outside SQLite.  The caller saves the blob
    first and removes it if this function fails or reports that an existing
    receipt was re-used.
    """
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute(
            "SELECT 1 FROM cc_lines WHERE id=? AND card_id=? AND statement_id=?",
            (line_id, card_id, statement_id)).fetchone()
        if not target:
            raise ValueError('Receipt target no longer belongs to this card statement.')

        existing = None
        if content_hash:
            existing = conn.execute(
                "SELECT id FROM cc_receipts WHERE statement_id=? AND content_hash=? "
                "ORDER BY id LIMIT 1", (statement_id, content_hash)).fetchone()
        if existing:
            receipt_id = existing['id']
            receipt_created = False
        else:
            cur = conn.execute(
                "INSERT INTO cc_receipts (card_id, statement_id, line_id, file_path, "
                "original_filename, content_type, uploaded_by, content_hash) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (card_id, statement_id, None, file_path, original_filename,
                 content_type, uploaded_by, content_hash))
            receipt_id = cur.lastrowid
            receipt_created = True

        cur = conn.execute(
            "INSERT OR IGNORE INTO cc_receipt_lines (receipt_id, line_id, actor, "
            "linked_at) VALUES (?, ?, ?, datetime('now'))",
            (receipt_id, line_id, actor))
        link_created = cur.rowcount == 1
        conn.execute(
            "UPDATE cc_line_receipt_suggestions SET status='rejected' "
            "WHERE receipt_id=? AND line_id!=? AND status='suggested'",
            (receipt_id, line_id))
        if link_created:
            _mark_cc_line_coding_dirty(conn, line_id)

        linked = conn.execute(
            "SELECT 1 FROM cc_receipt_lines WHERE receipt_id=? AND line_id=?",
            (receipt_id, line_id)).fetchone()
        if not linked:
            raise RuntimeError('Receipt link was not created.')
        conn.commit()
        return {
            'receipt_id': receipt_id,
            'receipt_created': receipt_created,
            'link_created': link_created,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def find_cc_receipt_by_hash(card_id, content_hash):
    """An existing receipt on this card with the same file content, or None
    (duplicate-upload guard)."""
    if not content_hash:
        return None
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM cc_receipts WHERE card_id=? AND content_hash=? LIMIT 1",
            (card_id, content_hash)).fetchone()
    finally:
        conn.close()


def find_cc_receipt_by_hash_in_statement(statement_id, content_hash):
    """An existing receipt in THIS month's bucket with the same file content, or
    None. Scoped to the statement so a genuine within-month re-upload is caught,
    while the same file (e.g. one quarterly/annual invoice covering charges in
    several months) can still be added to another month's bucket."""
    if not content_hash:
        return None
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM cc_receipts WHERE statement_id=? AND content_hash=? LIMIT 1",
            (statement_id, content_hash)).fetchone()
    finally:
        conn.close()


# ── receipt ↔ line links (many-to-many) ──────────────────────────────────────

def link_cc_receipt(receipt_id, line_id, actor=None):
    """Attach a receipt to a transaction (idempotent). One receipt may link to
    many lines and one line to many receipts. Any *other* still-open suggestions
    for this receipt (proposing it against different lines) are rejected, so a
    stale 'confirm' button can't later mis-link the same receipt elsewhere.

    `actor` is stamped into the link (migration 0043) so a connector write is
    distinguishable from a browser one: an admin username, 'mcp:claude', or 'ai'.
    Returns True if this call created the link, False if it already existed —
    the caller needs to know, because re-linking is a no-op and reporting it as a
    change would be a lie.
    """
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO cc_receipt_lines (receipt_id, line_id, actor, "
            "linked_at) VALUES (?, ?, ?, datetime('now'))",
            (receipt_id, line_id, actor))
        created = cur.rowcount == 1
        conn.execute("UPDATE cc_line_receipt_suggestions SET status='rejected' "
                     "WHERE receipt_id=? AND line_id!=? AND status='suggested'",
                     (receipt_id, line_id))
        if created:
            _mark_cc_line_coding_dirty(conn, line_id)
        conn.commit()
        return created
    finally:
        conn.close()


def _mark_cc_line_coding_dirty(conn, line_id):
    """Queue a line for re-coding on the same open transaction.

    Called when a receipt is newly linked: the receipt's line items are the
    strongest coding signal we have, and a line coded before its receipt arrived was
    coded off the merchant string alone. Only touches the ``ai_*`` suggestion, never
    ``xero_account_code`` — a human's own coding is not disturbed — and skips lines
    already reconciled in Xero, which are closed and must not be churned.
    """
    conn.execute("UPDATE cc_lines SET coding_dirty=1 WHERE id=? AND xero_reconciled=0",
                 (line_id,))


def auto_link_cc_receipt_if_uncovered(receipt_id, line_id, actor='ai'):
    """Atomically auto-link only when the transaction has no receipt yet.

    Human actions may deliberately attach several receipts to one transaction;
    this stricter helper is solely for the AI worker's one-clear-match policy.
    ``BEGIN IMMEDIATE`` closes the check-then-insert race across worker processes.
    Returns True when this call created the link.

    Stamps ``actor='ai'`` by default (migration 0043), so an auto-match is
    distinguishable from a human link and from a connector one.
    """
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
                "SELECT 1 FROM cc_receipt_lines WHERE line_id=? LIMIT 1",
                (line_id,)).fetchone() is not None:
            conn.rollback()
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO cc_receipt_lines (receipt_id, line_id, actor, "
            "linked_at) VALUES (?, ?, ?, datetime('now'))",
            (receipt_id, line_id, actor))
        if cur.rowcount != 1:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE cc_line_receipt_suggestions SET status='rejected' "
            "WHERE receipt_id=? AND line_id!=? AND status='suggested'",
            (receipt_id, line_id))
        _mark_cc_line_coding_dirty(conn, line_id)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def count_cc_receipt_links(receipt_id):
    """How many transactions this receipt is linked to (for the _+Nmore label)."""
    conn = get_db()
    try:
        return conn.execute("SELECT COUNT(*) c FROM cc_receipt_lines WHERE receipt_id=?",
                            (receipt_id,)).fetchone()['c']
    finally:
        conn.close()


def cc_receipt_line_exists(receipt_id, line_id, conn=None):
    """True if this exact receipt<->charge link already exists.

    Needed to tell "created the link" apart from "the link was already there". The
    INSERT is `OR IGNORE`, so without checking first, an idempotent re-link is
    indistinguishable from a real one — and a caller reporting a change that did not
    happen is worse than one that reports nothing.
    """
    own = conn is None
    if own:
        conn = get_db()
    try:
        return conn.execute(
            "SELECT 1 FROM cc_receipt_lines WHERE receipt_id=? AND line_id=? LIMIT 1",
            (receipt_id, line_id)).fetchone() is not None
    finally:
        if own:
            conn.close()


def cc_line_has_receipt(line_id):
    """True if a transaction already has at least one receipt linked — used to
    stop the AI auto-linking a second receipt onto an already-covered line."""
    conn = get_db()
    try:
        return conn.execute("SELECT 1 FROM cc_receipt_lines WHERE line_id=? LIMIT 1",
                            (line_id,)).fetchone() is not None
    finally:
        conn.close()


# ── Credit Card — AI extraction + match suggestions (see cc_ai.py) ───────────

def list_cc_receipts_pending_ai(limit=100, statement_id=None):
    """Receipts not yet processed by the AI extractor (oldest first).

    Pass `statement_id` to scope to a single month — used by the on-demand
    "Match now" button so a click only works the current statement.
    """
    conn = get_db()
    try:
        # 'failed' is transient (an object-store outage, or the AI key not yet loaded when the
        # first batch ran) so it IS retried; 'unreadable'/'processed' are terminal.
        # Dropped-off inbox receipts (statement_id IS NULL) are skipped — there are
        # no lines to match against yet; they become eligible once assigned to a month.
        sql = ("SELECT id, statement_id, card_id, file_path, content_type "
               "FROM cc_receipts WHERE (ai_status IS NULL OR ai_status IN ('pending','failed')) "
               "AND statement_id IS NOT NULL")
        params = []
        if statement_id is not None:
            sql += " AND statement_id=?"
            params.append(statement_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# Failure reasons that will NEVER succeed on a retry: a PDF the API cannot decode
# stays undecodable, and a slip the safety filter blocks stays blocked. Everything
# else (quota, timeout, network, a dead key, a garbled response) is worth another go,
# so it is deliberately NOT listed here.
CC_TERMINAL_AI_ERRORS = ('unsupported_media', 'blocked_by_safety')

# How long to wait before re-attempting a receipt that failed for a retryable
# reason. The worker runs hourly; retrying every run meant one corrupt upload was
# re-read every hour indefinitely, and a backend that was refusing work re-hit
# itself immediately.
CC_AI_RETRY_BACKOFF_HOURS = 6


def claim_cc_receipts_pending_ai(limit=100, statement_id=None, stale_minutes=30,
                                 force=False):
    """Atomically claim pending receipts for one worker.

    ``ai_status='processing'`` is a short lease. If a worker dies mid-request,
    another run may reclaim it after ``stale_minutes`` instead of leaving the
    receipt stuck forever. The immediate transaction serialises competing
    worker processes around the select+update pair.

    Previously-failed receipts are re-attempted, but not unconditionally: a
    terminal ``ai_error`` (see ``CC_TERMINAL_AI_ERRORS``) is never retried, and a
    retryable one waits ``CC_AI_RETRY_BACKOFF_HOURS`` first. Fresh receipts
    (``ai_status`` NULL/'pending') are always claimed immediately.

    ``force=True`` drops BOTH of those gates, and exists because they are wrong for
    a human who has just asked for a retry. A cardholder's iPhone upload that the
    API could not decode would otherwise be permanently unretryable, and an admin
    clicking "Match now" a minute after a timeout would be told there was nothing
    waiting. The gates are there to stop the *hourly cron* wasting calls, not to
    overrule someone standing at the screen.
    """
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        terminal_marks = ','.join('?' for _ in CC_TERMINAL_AI_ERRORS)
        params = []
        if force:
            failed_clause = "ai_status='failed'"
        else:
            failed_clause = (
                "ai_status='failed' "
                f"AND (ai_error IS NULL OR ai_error NOT IN ({terminal_marks})) "
                "AND (ai_processed_at IS NULL OR ai_processed_at <= datetime('now', ?))")
            params.extend(CC_TERMINAL_AI_ERRORS)
            params.append(f'-{max(1, int(CC_AI_RETRY_BACKOFF_HOURS))} hours')
        sql = (
            "SELECT id, statement_id, card_id, file_path, content_type "
            "FROM cc_receipts WHERE statement_id IS NOT NULL AND ("
            "ai_status IS NULL OR ai_status='pending' OR ("
            f"{failed_clause}"
            ") OR ("
            "ai_status='processing' AND (ai_processed_at IS NULL OR "
            "ai_processed_at <= datetime('now', ?))))")
        params.append(f'-{max(1, int(stale_minutes))} minutes')
        if statement_id is not None:
            sql += " AND statement_id=?"
            params.append(statement_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(max(0, int(limit)))
        rows = conn.execute(sql, params).fetchall()
        if rows:
            marks = ','.join('?' for _ in rows)
            conn.execute(
                f"UPDATE cc_receipts SET ai_status='processing', "
                f"ai_processed_at=datetime('now') WHERE id IN ({marks})",
                [r['id'] for r in rows])
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_cc_receipt_ai(receipt_id, vendor, date_iso, total_cents, raw_json, status):
    """Write back the AI-extracted fields + processing status for one receipt.

    The vendor string and the raw JSON blob come straight from the receipt-OCR
    model, so PII-scrub them here (the DB write boundary) before they land — a
    card number, email, phone or SA ID number a model might echo off a scanned
    slip must never be stored in the clear. See scrub.py (scrub_pii); this
    honours its documented 'before it touches the DB' contract."""
    conn = get_db()
    try:
        # ai_error is cleared here: this row is the SUCCESS path, so a reason recorded
        # by an earlier failed attempt (the worker retries 'failed') must not linger and
        # make a healthy receipt look broken.
        conn.execute(
            "UPDATE cc_receipts SET ai_vendor=?, ai_date=?, ai_total_cents=?, "
            "ai_raw_json=?, ai_status=?, ai_error=NULL, "
            "ai_processed_at=datetime('now') WHERE id=?",
            (scrub.scrub_pii(vendor), date_iso, total_cents,
             scrub.scrub_pii(raw_json), status, receipt_id))
        conn.commit()
    finally:
        conn.close()


def set_cc_receipt_ai_status(receipt_id, status, error=None):
    """Mark a receipt's extraction state, optionally with WHY it failed.

    `error` is a short classification from northwind.cards.ai.classify_ai_error (migration
    0044) — 'quota_exhausted', 'no_api_key', 'timeout', … A successful status clears any
    previous error, so a receipt that failed once and then processed does not keep a
    stale reason attached to it.
    """
    conn = get_db()
    try:
        conn.execute(
            "UPDATE cc_receipts SET ai_status=?, ai_error=?, "
            "ai_processed_at=datetime('now') WHERE id=?",
            (status, error, receipt_id))
        conn.commit()
    finally:
        conn.close()


def get_cc_ai_error_tally(card_id=None, conn=None):
    """{reason: count} over receipts that have a recorded extraction failure.

    The point of the classification is being able to group by it: '31 x quota_exhausted'
    is an operational fact you can act on, where '31 failed' is not.
    """
    own = conn is None
    if own:
        conn = get_db()
    try:
        sql = ("SELECT ai_error AS reason, COUNT(*) AS n FROM cc_receipts "
               "WHERE ai_error IS NOT NULL")
        params = []
        if card_id is not None:
            sql += " AND card_id=?"
            params.append(int(card_id))
        sql += " GROUP BY ai_error ORDER BY n DESC"
        return {r['reason']: r['n'] for r in conn.execute(sql, params)}
    finally:
        if own:
            conn.close()


def get_cc_ai_status(card_id, statement_id=None):
    """Aggregate receipt-AI state for an admin card/month status panel."""
    conn = get_db()
    try:
        sql = ("SELECT SUM(CASE WHEN ai_status IS NULL OR ai_status='pending' THEN 1 ELSE 0 END) pending, "
               "SUM(CASE WHEN ai_status='processing' THEN 1 ELSE 0 END) processing, "
               "SUM(CASE WHEN ai_status IN ('failed','unreadable') THEN 1 ELSE 0 END) failed, "
               "MAX(ai_processed_at) last_processed_at FROM cc_receipts WHERE card_id=?")
        params = [card_id]
        if statement_id is not None:
            sql += " AND statement_id=?"
            params.append(statement_id)
        row = conn.execute(sql, params).fetchone()
        return {k: (row[k] or 0) for k in ('pending', 'processing', 'failed')} | {
            'last_processed_at': row['last_processed_at']}
    finally:
        conn.close()


def retry_cc_receipt_ai(card_id, statement_id=None):
    """Queue failed/unreadable receipts again; returns the number queued."""
    conn = get_db()
    try:
        sql = ("UPDATE cc_receipts SET ai_status='pending', ai_processed_at=NULL "
               "WHERE card_id=? AND ai_status IN ('failed','unreadable')")
        params = [card_id]
        if statement_id is not None:
            sql += " AND statement_id=?"
            params.append(statement_id)
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def set_cc_receipt_download_name(receipt_id, name):
    """Override the served download filename (self-labelling after a match)."""
    conn = get_db()
    try:
        conn.execute("UPDATE cc_receipts SET download_name=? WHERE id=?", (name, receipt_id))
        conn.commit()
    finally:
        conn.close()


def get_cc_spend_lines_for_matching(statement_id):
    """Outstanding spend lines in a statement — the candidates a receipt could
    match. Personal charges are excluded: the cardholder repays them and they
    never need a receipt, so the AI must not auto-link or suggest against them.
    Transactions already marked reconciled in Xero are excluded for the same
    reason — they are closed, and a suggestion against one would surface on a
    row nobody is looking at any more."""
    conn = get_db()
    try:
        # COALESCE on both flags rather than `personal=0 AND xero_reconciled=0`: a NULL
        # fails a bare `=0`, so a single un-defaulted row would drop out of the
        # candidate pool entirely and its receipt could never match anything. Today
        # every live row is 0, which is exactly why this would have been invisible
        # until the day it wasn't. The coding claim uses the same form.
        return conn.execute(
            "SELECT id, line_date, reference, amount_cents FROM cc_lines "
            "WHERE statement_id=? AND category='spend' AND status='outstanding' "
            "AND COALESCE(personal,0)=0 AND COALESCE(xero_reconciled,0)=0",
            (statement_id,)).fetchall()
    finally:
        conn.close()


def add_cc_suggestion(line_id, receipt_id, score, status='suggested'):
    """Record (or update) an AI-proposed receipt<->line match. Kept separate
    from confirmed links (cc_receipt_lines) until promoted, so a suggestion
    never counts as 'covered'."""
    conn = get_db()
    try:
        # On re-run, refresh score/status — but never downgrade a row that's
        # already 'confirmed' (an auto-link or admin confirm) back to 'suggested'.
        conn.execute(
            "INSERT INTO cc_line_receipt_suggestions (line_id, receipt_id, score, status) "
            "VALUES (?,?,?,?) ON CONFLICT(line_id, receipt_id) DO UPDATE SET "
            "score=excluded.score, status=excluded.status "
            "WHERE cc_line_receipt_suggestions.status != 'confirmed'",
            (line_id, receipt_id, score, status))
        conn.commit()
    finally:
        conn.close()


def list_cc_suggestions_for_statement(statement_id):
    """Pending suggestions for a statement, with both sides of the proposed
    match. Shared by the admin review and the cardholder confirmation UI."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT s.id, s.line_id, s.receipt_id, s.score, s.status, "
            "       r.original_filename, r.ai_vendor, r.ai_date, r.ai_total_cents, "
            "       l.reference AS line_reference, l.line_date, l.amount_cents "
            "FROM cc_line_receipt_suggestions s "
            "JOIN cc_receipts r ON r.id = s.receipt_id "
            "JOIN cc_lines l ON l.id = s.line_id "
            "WHERE r.statement_id = ? AND s.status = 'suggested' "
            "  AND NOT EXISTS (SELECT 1 FROM cc_receipt_lines rl "
            "                  WHERE rl.line_id = s.line_id AND rl.receipt_id = s.receipt_id) "
            "ORDER BY s.line_id, s.score DESC", (statement_id,)).fetchall()
    finally:
        conn.close()


def get_cc_suggestion(suggestion_id):
    """One suggestion plus its card/statement ownership for route guards."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT s.*, r.card_id, r.statement_id, r.file_path, "
            "       r.original_filename, l.reference AS line_reference, "
            "       l.line_date, l.amount_cents "
            "FROM cc_line_receipt_suggestions s "
            "JOIN cc_receipts r ON r.id=s.receipt_id "
            "JOIN cc_lines l ON l.id=s.line_id "
            "WHERE s.id=? AND l.card_id=r.card_id AND l.statement_id=r.statement_id",
            (suggestion_id,)).fetchone()
    finally:
        conn.close()


def confirm_cc_suggestion(suggestion_id, actor=None):
    """Promote a suggestion into a real link and mark it confirmed. Returns
    (receipt_id, line_id) or None if the suggestion is gone.

    `actor` (migration 0043) records who accepted the AI's guess — a human confirming a
    suggestion is a different act from the worker auto-linking, and the audit trail
    should say which happened.
    """
    conn = get_db()
    try:
        s = conn.execute("SELECT line_id, receipt_id FROM cc_line_receipt_suggestions "
                         "WHERE id=? AND status='suggested'", (suggestion_id,)).fetchone()
        if not s:
            return None
        with conn:
            conn.execute("INSERT OR IGNORE INTO cc_receipt_lines (receipt_id, line_id, "
                         "actor, linked_at) VALUES (?, ?, ?, datetime('now'))",
                         (s['receipt_id'], s['line_id'], actor))
            conn.execute("UPDATE cc_line_receipt_suggestions SET status='confirmed' WHERE id=?",
                         (suggestion_id,))
            conn.execute("UPDATE cc_line_receipt_suggestions SET status='rejected' "
                         "WHERE receipt_id=? AND id!=? AND status='suggested'",
                         (s['receipt_id'], suggestion_id))
        return (s['receipt_id'], s['line_id'])
    finally:
        conn.close()


def reject_cc_suggestion(suggestion_id):
    """Admin dismisses a wrong AI suggestion. Returns the owning card_id (for the
    redirect) or None. The receipt stays in the bucket; only the guess is cleared."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT r.card_id FROM cc_line_receipt_suggestions s "
            "JOIN cc_receipts r ON r.id = s.receipt_id WHERE s.id=?",
            (suggestion_id,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE cc_line_receipt_suggestions SET status='rejected' WHERE id=?",
                     (suggestion_id,))
        conn.commit()
        return row['card_id']
    finally:
        conn.close()


def unlink_cc_receipt(receipt_id, line_id):
    """Detach a receipt from one transaction (other links are untouched; if it
    ends up with no links it falls back into the bucket).

    Returns True if a link was actually removed. The caller needs to distinguish
    "unlinked it" from "there was nothing there" — an agent reporting that it cut a
    link it never found would be worse than useless.
    """
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM cc_receipt_lines WHERE receipt_id=? AND line_id=?",
            (receipt_id, line_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_cc_receipt_links(statement_id):
    """[(receipt_id, line_id)] for every link in a statement."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT rl.receipt_id, rl.line_id FROM cc_receipt_lines rl "
            "JOIN cc_receipts r ON r.id = rl.receipt_id WHERE r.statement_id=?",
            (statement_id,)).fetchall()
    finally:
        conn.close()


def list_cc_receipts(statement_id):
    """Files already dropped into a month's bucket, newest first."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM cc_receipts WHERE statement_id=? ORDER BY uploaded_at DESC, id DESC",
            (statement_id,)).fetchall()
    finally:
        conn.close()


def count_cc_receipts(statement_id):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) c FROM cc_receipts WHERE statement_id=?",
            (statement_id,)).fetchone()['c']
    finally:
        conn.close()


def get_cc_receipt(receipt_id):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM cc_receipts WHERE id=?", (receipt_id,)).fetchone()
    finally:
        conn.close()


def delete_cc_receipt(receipt_id):
    """Delete a receipt row (links + suggestions cascade via FK). Returns the
    stored file_path so the caller can remove the blob — the DB layer doesn't
    touch storage, so a caller that forgets to delete the file would orphan it."""
    conn = get_db()
    try:
        row = conn.execute("SELECT file_path FROM cc_receipts WHERE id=?",
                           (receipt_id,)).fetchone()
        conn.execute("DELETE FROM cc_receipts WHERE id=?", (receipt_id,))
        conn.commit()
        return row['file_path'] if row else None
    finally:
        conn.close()


# ── Credit Card — receipt drop-off / inbox (receipts ahead of a statement) ───
# A receipt with statement_id IS NULL (and no line link) is "dropped off" — the
# cardholder uploaded it before the matching month's transactions were loaded.
# It waits in the card's inbox until they assign it to a month to match + reason.

def list_cc_inbox_receipts(card_id):
    """Dropped-off receipts waiting to be matched (statement_id IS NULL), newest
    first. These are card-level, not tied to any month yet."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM cc_receipts WHERE card_id=? AND statement_id IS NULL "
            "ORDER BY uploaded_at DESC, id DESC", (card_id,)).fetchall()
    finally:
        conn.close()


def count_cc_inbox_receipts(card_id):
    """How many receipts are waiting in this card's drop-off inbox."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM cc_receipts WHERE card_id=? AND statement_id IS NULL",
            (card_id,)).fetchone()[0]
    finally:
        conn.close()


def assign_cc_receipt_to_statement(receipt_id, statement_id):
    """Move a dropped-off receipt into a month's bucket so it can be linked to a
    transaction and given a reason. Only acts on a still-unassigned receipt
    (statement_id IS NULL). Returns True if it moved."""
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE cc_receipts SET statement_id=? WHERE id=? AND statement_id IS NULL",
            (statement_id, receipt_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── Credit Card — cardholder login credentials (per email) ───────────────────

def get_cc_user(email):
    """A portal credential by email. Now backed by the unified `users` table
    (login = email); returns a row exposing ['email'] and ['password_hash']."""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT id, login AS email, display_name, password_hash, is_active, auth_version "
            "FROM users WHERE login = ?", ((email or '').strip().lower(),)
        ).fetchone()
    finally:
        conn.close()


def set_cc_user_password(email, password_hash):
    """Create or reset a portal person's login credential (hash computed by the
    caller). Upserts into the unified `users` table keyed on login = email."""
    email = (email or '').strip().lower()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (login, email, password_hash) VALUES (?, ?, ?) "
            "ON CONFLICT(login) DO UPDATE SET password_hash=excluded.password_hash, "
            "auth_version=users.auth_version+1, "
            "updated_at=datetime('now')",
            (email, email, password_hash))
        conn.commit()
    finally:
        conn.close()


# ── Credit Card — receipt↔line matching & per-line finance requests ─────────

def get_cc_line(line_id):
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM cc_lines WHERE id=?", (line_id,)).fetchone()
    finally:
        conn.close()


# The cardholder-completeness rule, in one place. Every guard that asks "is this
# transaction finished?" must use exactly this, so the portal, the submit routes,
# the review queue and both reconcile paths can never drift apart.
_CC_LINE_READY_SQL = (
    "l.category='spend' AND l.status='outstanding' "
    "AND trim(COALESCE(l.reason, '')) != '' "
    "AND trim(COALESCE(l.location, '')) != '' "
    "AND COALESCE(l.vat_invoice_required, 0)=0 "
    "AND (l.personal=1 OR EXISTS ("
    "    SELECT 1 FROM cc_receipt_lines rl WHERE rl.line_id=l.id)) "
    "AND NOT EXISTS ("
    "    SELECT 1 FROM cc_line_receipt_suggestions s "
    "    WHERE s.line_id=l.id AND s.status='suggested' "
    "      AND NOT EXISTS ("
    "          SELECT 1 FROM cc_receipt_lines linked "
    "          WHERE linked.line_id=s.line_id "
    "            AND linked.receipt_id=s.receipt_id))"
)


def _cc_ready_line_ids(line_ids, *, require_submitted):
    """Shared body for the two readiness helpers below — one query, one connection."""
    ids = list(dict.fromkeys(int(i) for i in line_ids if i is not None))
    if not ids:
        return set()
    conn = get_db()
    try:
        marks = ','.join('?' for _ in ids)
        sql = (f"SELECT l.id FROM cc_lines l WHERE l.id IN ({marks}) AND "
               + _CC_LINE_READY_SQL)
        if require_submitted:
            sql += " AND l.submitted_at IS NOT NULL"
        return {r['id'] for r in conn.execute(sql, ids).fetchall()}
    finally:
        conn.close()


def get_cc_ready_line_ids(line_ids):
    """Return the submitted-ready subset of ``line_ids``.

    A transaction is ready only when it has a reason, a location, either a
    linked receipt or is marked personal, no unresolved AI receipt suggestion,
    and no open VAT tax-invoice request from finance. This is the server-side
    source of truth used by both individual and bulk submit routes; disabled
    buttons alone are not a guard.
    """
    return _cc_ready_line_ids(line_ids, require_submitted=False)


def get_cc_xero_ready_line_ids(line_ids):
    """Return the subset that is complete *and* submitted to finance.

    Xero account coding is advisory and intentionally does not participate in
    this readiness rule.
    """
    return _cc_ready_line_ids(line_ids, require_submitted=True)


def list_cc_review_periods(card_ids=None):
    """Distinct statement periods available to the multi-card admin review."""
    ids = sorted({int(i) for i in (card_ids or []) if i})
    if card_ids is not None and not ids:
        return []
    conn = get_db()
    try:
        sql = (
            "SELECT DISTINCT s.year, s.month FROM cc_statements s "
            "JOIN cc_cards c ON c.id=s.card_id WHERE c.active=1"
        )
        params = []
        if ids:
            marks = ','.join('?' for _ in ids)
            sql += f" AND s.card_id IN ({marks})"
            params.extend(ids)
        sql += " ORDER BY s.year DESC, s.month DESC"
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def list_cc_review_lines(card_ids, year=None, month=None, status='unreconciled',
                         search=None):
    """Atomic transaction rows for the admin's multi-card Xero review queue.

    ``ready_for_xero`` is calculated from the transaction's own state only:
    receipt-or-personal, reason, location, submitted marker, and no pending AI
    receipt suggestion, and no open VAT tax-invoice request. A confirmed or
    AI-suggested Xero account is surfaced for convenience but intentionally
    does not participate in readiness.
    """
    ids = sorted({int(i) for i in (card_ids or []) if i})
    if not ids:
        return []
    marks = ','.join('?' for _ in ids)
    has_receipt = (
        "EXISTS (SELECT 1 FROM cc_receipt_lines rl WHERE rl.line_id=l.id)"
    )
    has_pending = (
        "EXISTS (SELECT 1 FROM cc_line_receipt_suggestions sg "
        "WHERE sg.line_id=l.id AND sg.status='suggested' "
        "AND NOT EXISTS (SELECT 1 FROM cc_receipt_lines linked "
        "                WHERE linked.line_id=sg.line_id "
        "                  AND linked.receipt_id=sg.receipt_id))"
    )
    cardholder_complete = (
        f"(l.personal=1 OR {has_receipt}) "
        "AND TRIM(COALESCE(l.reason,''))<>'' "
        "AND TRIM(COALESCE(l.location,''))<>'' "
        "AND COALESCE(l.vat_invoice_required,0)=0 "
        "AND l.submitted_at IS NOT NULL"
    )
    ready = f"({cardholder_complete}) AND NOT {has_pending}"
    conn = get_db()
    try:
        sql = (
            "SELECT l.*, s.year, s.month, c.card_name, c.display_name, "
            f"CASE WHEN {has_receipt} THEN 1 ELSE 0 END AS has_receipt, "
            "(SELECT COUNT(*) FROM cc_receipt_lines rl WHERE rl.line_id=l.id) "
            "AS receipt_count, "
            "CASE WHEN TRIM(COALESCE(l.reason,''))<>'' THEN 1 ELSE 0 END "
            "AS has_reason, "
            "CASE WHEN TRIM(COALESCE(l.location,''))<>'' THEN 1 ELSE 0 END "
            "AS has_location, "
            f"CASE WHEN {has_pending} THEN 1 ELSE 0 END AS has_pending_suggestion, "
            f"CASE WHEN {ready} THEN 1 ELSE 0 END AS ready_for_xero "
            "FROM cc_lines l "
            "JOIN cc_statements s ON s.id=l.statement_id "
            "JOIN cc_cards c ON c.id=l.card_id "
            f"WHERE c.active=1 AND l.card_id IN ({marks}) "
            "AND l.category='spend' AND l.status='outstanding'"
        )
        params = list(ids)
        if year is not None:
            sql += " AND s.year=?"
            params.append(int(year))
        if month is not None:
            sql += " AND s.month=?"
            params.append(int(month))

        status = (status or 'unreconciled').strip().lower()
        if status == 'reconciled':
            sql += " AND l.xero_reconciled=1"
        else:
            sql += " AND l.xero_reconciled=0"
            if status == 'ready':
                sql += f" AND {ready}"
            elif status == 'needs_ai':
                sql += f" AND {has_pending}"
            elif status == 'needs_cardholder':
                sql += f" AND NOT ({cardholder_complete})"
            elif status == 'personal':
                sql += " AND l.personal=1"

        term = (search or '').strip()
        if term:
            like = f"%{term}%"
            sql += (
                " AND (l.reference LIKE ? COLLATE NOCASE "
                "OR COALESCE(l.reason,'') LIKE ? COLLATE NOCASE "
                "OR COALESCE(l.location,'') LIKE ? COLLATE NOCASE "
                "OR COALESCE(c.display_name,c.card_name) LIKE ? COLLATE NOCASE)"
            )
            params.extend([like, like, like, like])
        sql += " ORDER BY l.line_date DESC, l.id DESC"
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def list_cc_review_receipts(line_ids):
    """Receipt rows grouped by the caller for the selected transaction ids."""
    ids = sorted({int(i) for i in (line_ids or []) if i})
    if not ids:
        return []
    marks = ','.join('?' for _ in ids)
    conn = get_db()
    try:
        return conn.execute(
            "SELECT rl.line_id, r.* FROM cc_receipt_lines rl "
            "JOIN cc_receipts r ON r.id=rl.receipt_id "
            f"WHERE rl.line_id IN ({marks}) "
            "ORDER BY rl.line_id, r.uploaded_at DESC, r.id DESC",
            ids).fetchall()
    finally:
        conn.close()


def list_cc_review_suggestions(line_ids):
    """Pending AI receipt suggestions for selected transaction ids.

    The r.card_id/r.statement_id equality checks below are defence in depth, not
    a live filter: the worker only ever suggests a receipt against lines in that
    receipt's OWN statement (get_cc_spend_lines_for_matching), a receipt's
    statement_id is write-once (assign_cc_receipt_to_statement only moves a row
    whose statement_id IS NULL) and a line's statement_id is never rewritten. So
    they always agree, which is what keeps this query's result set consistent
    with the `has_pending` gate in list_cc_review_lines — that gate omits them.
    Keep the two in step if the ownership rules above ever change."""
    ids = sorted({int(i) for i in (line_ids or []) if i})
    if not ids:
        return []
    marks = ','.join('?' for _ in ids)
    conn = get_db()
    try:
        return conn.execute(
            "SELECT s.id, s.line_id, s.receipt_id, s.score, "
            "r.original_filename, r.ai_vendor, r.ai_date, r.ai_total_cents "
            "FROM cc_line_receipt_suggestions s "
            "JOIN cc_receipts r ON r.id=s.receipt_id "
            "JOIN cc_lines l ON l.id=s.line_id "
            f"WHERE s.line_id IN ({marks}) AND s.status='suggested' "
            "AND r.card_id=l.card_id AND r.statement_id=l.statement_id "
            "AND NOT EXISTS ("
            "SELECT 1 FROM cc_receipt_lines linked "
            "WHERE linked.line_id=s.line_id AND linked.receipt_id=s.receipt_id"
            ") "
            "ORDER BY s.line_id, s.score DESC",
            ids).fetchall()
    finally:
        conn.close()


def reconcile_cc_review_lines(card_ids, line_ids, year=None, month=None,
                              actor=None):
    """Mark eligible selected transactions reconciled in one statement.

    Both the selected cards and line ids are re-checked server-side against the
    SAME readiness rule the queue displays (see list_cc_review_lines): a row
    that is incomplete, still carrying an open VAT tax-invoice request, already
    reconciled, on an archived card, or on a card outside the selection is
    skipped. ``datetime('now')`` is fixed for the whole transaction, so the rows
    reconciled together share one timestamp. Returns ``(done, skipped)``.
    """
    scoped_cards = sorted({int(i) for i in (card_ids or []) if i})
    ids = sorted({int(i) for i in (line_ids or []) if i})
    if not scoped_cards or not ids:
        return 0, len(ids)
    line_marks = ','.join('?' for _ in ids)
    card_marks = ','.join('?' for _ in scoped_cards)
    period_sql = ''
    period_params = []
    if year is not None and month is not None:
        period_sql = (
            "AND EXISTS (SELECT 1 FROM cc_statements s "
            "            WHERE s.id=l.statement_id AND s.year=? AND s.month=?) "
        )
        period_params = [int(year), int(month)]
    conn = get_db()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE cc_lines AS l SET xero_reconciled=1, "
                "xero_reconciled_at=datetime('now'), xero_reconciled_by=?, "
                "xero_reconciled_override=0 "
                "WHERE l.id IN (" + line_marks + ") "
                "AND l.card_id IN (" + card_marks + ") "
                "AND EXISTS (SELECT 1 FROM cc_cards c "
                "            WHERE c.id=l.card_id AND c.active=1) "
                + period_sql +
                "AND l.category='spend' AND l.status='outstanding' "
                "AND l.xero_reconciled=0 "
                "AND (l.personal=1 OR EXISTS ("
                "    SELECT 1 FROM cc_receipt_lines rl WHERE rl.line_id=l.id)) "
                "AND TRIM(COALESCE(l.reason,''))<>'' "
                "AND TRIM(COALESCE(l.location,''))<>'' "
                "AND COALESCE(l.vat_invoice_required,0)=0 "
                "AND l.submitted_at IS NOT NULL "
                "AND NOT EXISTS ("
                "    SELECT 1 FROM cc_line_receipt_suggestions sg "
                "    WHERE sg.line_id=l.id AND sg.status='suggested' "
                "      AND NOT EXISTS ("
                "          SELECT 1 FROM cc_receipt_lines linked "
                "          WHERE linked.line_id=sg.line_id "
                "            AND linked.receipt_id=sg.receipt_id))",
                [(actor or None), *ids, *scoped_cards, *period_params])
            done = cur.rowcount
        return done, len(ids) - done
    finally:
        conn.close()


def set_cc_line_require(line_id, required):
    conn = get_db()
    try:
        conn.execute("UPDATE cc_lines SET require_individual=? WHERE id=?",
                     (1 if required else 0, line_id))
        conn.commit()
    finally:
        conn.close()


def set_cc_line_vat_invoice_required(line_id, required, actor=None):
    """Open or clear finance's request for a compliant supplier tax invoice.

    The original request timestamp/actor remain after clearing for lightweight
    audit context. Re-opening the request records the new request moment.
    """
    conn = get_db()
    try:
        conn.execute(
            "UPDATE cc_lines SET vat_invoice_required=?, "
            "vat_invoice_requested_at=CASE WHEN ? THEN datetime('now') "
            "    ELSE vat_invoice_requested_at END, "
            "vat_invoice_requested_by=CASE WHEN ? THEN ? "
            "    ELSE vat_invoice_requested_by END "
            "WHERE id=?",
            (1 if required else 0, 1 if required else 0,
             1 if required else 0, (actor or '').strip() or 'admin', line_id))
        conn.commit()
    finally:
        conn.close()


def set_cc_line_reason(line_id, reason):
    """Cardholder's reason/description for a transaction (for Xero coding)."""
    conn = get_db()
    try:
        conn.execute("UPDATE cc_lines SET reason=?, coding_dirty=1 WHERE id=?",
                     ((reason or '').strip() or None, line_id))
        conn.commit()
    finally:
        conn.close()


def set_cc_line_location(line_id, location):
    """Cardholder's location tag(s) for a transaction (e.g. 'Summit, Rosewood').

    Stored as a free-text string on the line: the portal's multi-select chip
    picker (restricted to get_cc_locations — no free-typed values) writes the
    chosen names comma-joined, so changing the store list never touches
    existing lines."""
    conn = get_db()
    try:
        conn.execute("UPDATE cc_lines SET location=? WHERE id=?",
                     ((location or '').strip() or None, line_id))
        conn.commit()
    finally:
        conn.close()


# Permanent CC locations that always appear in the picker even if they aren't in
# the `stores` table. Kept in picker order (before the mirrored store list).
PINNED_CC_LOCATIONS = ['HQ']


def get_cc_locations():
    """Locations offered in the cardholder portal picker: the **live store list**
    (`stores`) plus the permanent pinned entries (HQ). Live-mirrors stores — add a
    store on the Stores page and it shows here automatically. Selection is
    restricted to this list (the picker only searches it; no free-typed one-offs).
    Returns [{'name': ...}], pinned entries first, then stores A→Z (deduped
    case-insensitively)."""
    names = list(PINNED_CC_LOCATIONS)
    have = {n.lower() for n in names}
    for s in get_stores():
        if s and s.lower() not in have:
            names.append(s)
            have.add(s.lower())
    return [{'name': n} for n in names]


def set_cc_lines_submitted(line_ids, by, submitted=True):
    """Cardholder marks one or more transactions as submitted to finance.

    A SOFT 'sent' marker, not a lock (see migration 0029) — the line stays
    editable afterwards. Pass ``submitted=False`` to move them back to draft.
    ``line_ids`` should already be scoped to the cardholder's card by the caller;
    this just writes. Returns the number of ids acted on."""
    ids = [int(i) for i in line_ids if i]
    if not ids:
        return 0
    conn = get_db()
    try:
        ph = ",".join("?" * len(ids))
        if submitted:
            conn.execute(
                f"UPDATE cc_lines SET submitted_at=datetime('now'), submitted_by=? "
                f"WHERE id IN ({ph})", (by, *ids))
        else:
            conn.execute(
                f"UPDATE cc_lines SET submitted_at=NULL, submitted_by=NULL "
                f"WHERE id IN ({ph})", ids)
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def set_cc_line_personal(line_id, personal):
    """Flag/unflag a transaction as a personal charge (employee to repay)."""
    conn = get_db()
    try:
        # Personal lines are excluded from coding, so clear the dirty flag when
        # flagging personal; re-dirty when un-flagging so it gets re-coded.
        conn.execute("UPDATE cc_lines SET personal=?, coding_dirty=? WHERE id=?",
                     (1 if personal else 0, 0 if personal else 1, line_id))
        conn.commit()
    finally:
        conn.close()


def get_cc_lines_needing_coding(limit=200, statement_id=None, conn=None):
    """Spend lines that still need an account suggestion (coding_dirty=1),
    excluding personal charges. Pass `conn` to share one connection across a batch."""
    own = conn is None
    if own:
        conn = get_db()
    try:
        sql = ("SELECT id, card_id, reference, amount_cents, reason, line_date "
               "FROM cc_lines WHERE category='spend' AND coding_dirty=1 "
               "AND (personal IS NULL OR personal=0)")
        params = []
        if statement_id is not None:
            sql += " AND statement_id=?"
            params.append(statement_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()
    finally:
        if own:
            conn.close()


def get_cc_lines_missing_coding(card_id=None, limit=500, conn=None):
    """Spend lines that still have NO confirmed Xero account code — the admin's
    actual coding to-do list.

    Distinct from get_cc_lines_needing_coding(), which keys off `coding_dirty` (the
    AI *suggestion queue*, cleared once the worker has run whether or not a human
    ever confirmed a code). This one keys off `xero_account_code IS NULL` and uses
    the SAME predicate as the `coding_missing` tile count in list_cc_cards(), so the
    two can never disagree. Filtering happens in SQL, so a card_id scope is exact
    rather than whatever survived a global LIMIT.

    Returns the AI's suggestion alongside each line so a caller can confirm or
    override it.
    """
    own = conn is None
    if own:
        conn = get_db()
    try:
        sql = ("SELECT l.id, l.card_id, l.statement_id, l.line_date, l.reference, "
               "       l.amount_cents, l.reason, l.needs_receipt, l.personal, "
               "       l.ai_account_code, l.ai_account_name, l.ai_confidence, "
               "       l.ai_rationale, l.submitted_at, "
               "       c.display_name AS card_display_name, c.card_name, "
               "       s.year, s.month "
               "  FROM cc_lines l "
               "  JOIN cc_cards c ON c.id = l.card_id "
               "  LEFT JOIN cc_statements s ON s.id = l.statement_id "
               " WHERE l.category='spend' AND l.status='outstanding' "
               "   AND l.personal=0 AND l.xero_reconciled=0 "
               "   AND l.xero_account_code IS NULL")
        params = []
        if card_id is not None:
            sql += " AND l.card_id=?"
            params.append(int(card_id))
        sql += " ORDER BY s.year DESC, s.month DESC, l.line_date, l.id LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()
    finally:
        if own:
            conn.close()


def get_cc_lines_ai_coded(card_id=None, statement_id=None, confidence=None,
                          needs_review_only=False, limit=200, conn=None):
    """Spend lines the AI HAS put a code on — the review list, not the to-do list.

    The important distinction from ``get_cc_lines_missing_coding``: that one lists
    charges with NO code, so a charge the AI coded *confidently and wrongly* does not
    appear in it at all. That was a real blind spot — Greenfields, Diner Co, Pizza Yard,
    Quickmart and Larder & Co were all coded to a single travel account, and a
    pharmacy came back "unknown merchant -> the fallback expense account". None of
    those were visible to any existing query, because from the app's point of view the
    coding was done.

    So this returns lines with an `ai_account_code`, whether or not a human has since
    confirmed one, with `confirmed` marking those where `xero_account_code` is already
    set (changing one of those is a correction, not a first coding). Judging whether a
    code is *right* needs the merchant name and a chart of accounts, so it is the
    caller's job — this only makes the candidates visible.

    Filters: `confidence` ('low'|'medium'|'high') and `needs_review_only` (the AI's own
    ai_needs_review flag). Lowest confidence first, so the least trustworthy come first.
    """
    own = conn is None
    if own:
        conn = get_db()
    try:
        sql = ("SELECT l.id, l.card_id, l.statement_id, l.line_date, l.reference, "
               "       l.amount_cents, l.reason, l.personal, "
               "       l.ai_account_code, l.ai_account_name, l.ai_confidence, "
               "       l.ai_rationale, l.ai_needs_review, l.ai_source, l.ai_coded_at, "
               "       l.xero_account_code, l.xero_account_name, l.xero_reconciled, "
               "       c.display_name AS card_display_name, c.card_name, "
               "       s.year, s.month "
               "  FROM cc_lines l "
               "  JOIN cc_cards c ON c.id = l.card_id "
               "  LEFT JOIN cc_statements s ON s.id = l.statement_id "
               " WHERE l.category='spend' AND l.personal=0 "
               "   AND l.ai_account_code IS NOT NULL")
        params = []
        if card_id is not None:
            sql += " AND l.card_id=?"
            params.append(int(card_id))
        if statement_id is not None:
            sql += " AND l.statement_id=?"
            params.append(int(statement_id))
        if confidence:
            sql += " AND LOWER(COALESCE(l.ai_confidence,''))=?"
            params.append(str(confidence).lower())
        if needs_review_only:
            sql += " AND COALESCE(l.ai_needs_review,0)=1"
        # Least trustworthy first: explicit review flag, then low/medium confidence.
        sql += (" ORDER BY COALESCE(l.ai_needs_review,0) DESC, "
                "          CASE LOWER(COALESCE(l.ai_confidence,'')) "
                "            WHEN 'low' THEN 0 WHEN 'medium' THEN 1 "
                "            WHEN 'high' THEN 2 ELSE 3 END, "
                "          s.year DESC, s.month DESC, l.line_date, l.id LIMIT ?")
        params.append(limit)
        return conn.execute(sql, params).fetchall()
    finally:
        if own:
            conn.close()


def get_cc_receipt_link_provenance(statement_id, conn=None):
    """[(receipt_id, line_id, actor, linked_at)] for a statement's links.

    Exists so "which links did the agent make?" is answerable — see migration 0043.
    A NULL actor means the link predates that migration.
    """
    own = conn is None
    if own:
        conn = get_db()
    try:
        return conn.execute(
            "SELECT rl.receipt_id, rl.line_id, rl.actor, rl.linked_at "
            "  FROM cc_receipt_lines rl "
            "  JOIN cc_receipts r ON r.id = rl.receipt_id "
            " WHERE r.statement_id=? ORDER BY rl.line_id, rl.receipt_id",
            (statement_id,)).fetchall()
    finally:
        if own:
            conn.close()


def claim_cc_lines_needing_coding(limit=200, statement_id=None, stale_minutes=30):
    """Atomically lease coding rows to one worker, recovering stale claims."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # xero_reconciled lines are excluded: they are closed in Xero, so re-coding one
        # spends a coding pass to change a suggestion on a row nobody is looking at any
        # more. _mark_cc_line_coding_dirty already skips them, but set_cc_line_reason
        # and set_cc_line_personal also raise the dirty flag without checking, so the
        # guard belongs here too — this is the single door every AI coding pass goes
        # through.
        sql = ("SELECT id, card_id, reference, amount_cents, reason, line_date "
               "FROM cc_lines WHERE category='spend' AND coding_dirty=1 "
               "AND COALESCE(personal,0)=0 AND COALESCE(xero_reconciled,0)=0 "
               "AND (coding_status IS NULL "
               "OR coding_status!='processing' OR coding_claimed_at IS NULL "
               "OR coding_claimed_at<=datetime('now', ?))")
        params = [f'-{max(1, int(stale_minutes))} minutes']
        if statement_id is not None:
            sql += " AND statement_id=?"
            params.append(statement_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(max(0, int(limit)))
        rows = conn.execute(sql, params).fetchall()
        if rows:
            marks = ','.join('?' for _ in rows)
            conn.execute(
                f"UPDATE cc_lines SET coding_status='processing', "
                f"coding_claimed_at=datetime('now') WHERE id IN ({marks})",
                [r['id'] for r in rows])
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_cc_line_coding_claim(line_id):
    conn = get_db()
    try:
        conn.execute("UPDATE cc_lines SET coding_status=NULL, coding_claimed_at=NULL WHERE id=?",
                     (line_id,))
        conn.commit()
    finally:
        conn.close()


def set_cc_line_ai_coding(line_id, account_code, account_name, confidence,
                          needs_review, rationale, source, conn=None):
    """Store the AI/memory account suggestion on a line and clear its dirty flag.
    Pass `conn` to share one connection (caller commits)."""
    own = conn is None
    if own:
        conn = get_db()
    try:
        conn.execute(
            "UPDATE cc_lines SET ai_account_code=?, ai_account_name=?, ai_confidence=?, "
            "ai_needs_review=?, ai_rationale=?, ai_source=?, "
            "ai_coded_at=datetime('now'), coding_dirty=0, coding_status='done', "
            "coding_claimed_at=NULL WHERE id=?",
            (account_code, account_name, confidence, 1 if needs_review else 0,
             scrub.scrub_pii(rationale), source, line_id))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def set_cc_line_xero_account(line_id, account_code, account_name):
    """The admin's confirmed / overridden Xero account for a line."""
    conn = get_db()
    try:
        conn.execute("UPDATE cc_lines SET xero_account_code=?, xero_account_name=? WHERE id=?",
                     (account_code or None, account_name or None, line_id))
        conn.commit()
    finally:
        conn.close()


def set_cc_line_xero_reconciled(line_id, reconciled, actor=None, override=False):
    """Admin manual 'reconciled in Xero' tick — the line's "done" marker.

    A ticked line drops out of the admin working view AND out of the
    cardholder's checklist. Stamps who/when so an audit can see it, and records
    ``override`` when it was ticked while the transaction was still short of the
    finance-ready rule (no receipt, reason, location or submission). Unticking
    clears all three stamps.
    """
    on = 1 if reconciled else 0
    conn = get_db()
    try:
        conn.execute(
            "UPDATE cc_lines SET xero_reconciled=?, "
            "xero_reconciled_at=CASE WHEN ? THEN datetime('now') ELSE NULL END, "
            "xero_reconciled_by=CASE WHEN ? THEN ? ELSE NULL END, "
            "xero_reconciled_override=CASE WHEN ? AND ? THEN 1 ELSE 0 END "
            "WHERE id=?",
            (on, on, on, (actor or None), on, 1 if override else 0, line_id))
        conn.commit()
    finally:
        conn.close()


# The finance-ready tail shared by the statement-scoped reconcile UPDATEs below.
# Same rule as _CC_LINE_READY_SQL (plus the submitted marker), written without a
# table alias because an UPDATE has none.
_CC_BULK_READY_TAIL = (
    "AND TRIM(COALESCE(reason,''))<>'' "
    "AND TRIM(COALESCE(location,''))<>'' "
    "AND COALESCE(vat_invoice_required,0)=0 "
    "AND submitted_at IS NOT NULL "
    "AND (personal=1 OR EXISTS "
    "     (SELECT 1 FROM cc_receipt_lines rl WHERE rl.line_id=cc_lines.id)) "
    "AND NOT EXISTS ("
    "    SELECT 1 FROM cc_line_receipt_suggestions sg "
    "    WHERE sg.line_id=cc_lines.id AND sg.status='suggested' "
    "      AND NOT EXISTS ("
    "          SELECT 1 FROM cc_receipt_lines linked "
    "          WHERE linked.line_id=sg.line_id "
    "            AND linked.receipt_id=sg.receipt_id))"
)

# Scope every posted id to this card + statement + an open spend line. Shared by
# the strict and the override sweep so neither can reach another card's row.
_CC_BULK_SCOPE = ("AND card_id=? AND statement_id=? AND category='spend' "
                  "AND status='outstanding' AND xero_reconciled=0 ")


def bulk_reconcile_cc_lines(card_id, statement_id, line_ids, actor=None):
    """Mark selected, finance-ready lines reconciled, fully scoped server-side."""
    ids = sorted({int(i) for i in line_ids if i})
    if not ids:
        return 0
    marks = ','.join('?' for _ in ids)
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE cc_lines SET xero_reconciled=1, "
            "xero_reconciled_at=datetime('now'), xero_reconciled_by=?, "
            f"xero_reconciled_override=0 WHERE id IN ({marks}) "
            + _CC_BULK_SCOPE + _CC_BULK_READY_TAIL,
            [(actor or None), *ids, card_id, statement_id])
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def force_reconcile_cc_lines(card_id, statement_id, line_ids, actor=None):
    """Admin override: mark every selected open transaction done, ready or not.

    Finance-ready rows are stamped exactly as the strict sweep would; the rest
    are stamped ``xero_reconciled_override=1`` so it stays visible — and
    auditable — that they were closed off while evidence was still missing. The
    card/statement/category scoping is unchanged, so a posted id still cannot
    reach another card's transaction. Returns ``(done, forced)``.
    """
    ids = sorted({int(i) for i in line_ids if i})
    if not ids:
        return 0, 0
    marks = ','.join('?' for _ in ids)
    scope = [*ids, card_id, statement_id]
    conn = get_db()
    try:
        with conn:
            ready = conn.execute(
                "UPDATE cc_lines SET xero_reconciled=1, "
                "xero_reconciled_at=datetime('now'), xero_reconciled_by=?, "
                f"xero_reconciled_override=0 WHERE id IN ({marks}) "
                + _CC_BULK_SCOPE + _CC_BULK_READY_TAIL,
                [(actor or None), *scope]).rowcount
            # Whatever is left in scope was not ready — the xero_reconciled=0
            # test in _CC_BULK_SCOPE keeps this pass off the rows just done.
            forced = conn.execute(
                "UPDATE cc_lines SET xero_reconciled=1, "
                "xero_reconciled_at=datetime('now'), xero_reconciled_by=?, "
                f"xero_reconciled_override=1 WHERE id IN ({marks}) "
                + _CC_BULK_SCOPE,
                [(actor or None), *scope]).rowcount
        return ready + forced, forced
    finally:
        conn.close()


def bulk_accept_cc_ai_accounts(card_id, statement_id, line_ids):
    """Accept selected high-confidence AI/memory codes after explicit review."""
    ids = sorted({int(i) for i in line_ids if i})
    if not ids:
        return 0
    marks = ','.join('?' for _ in ids)
    conn = get_db()
    try:
        cur = conn.execute(
            f"UPDATE cc_lines SET xero_account_code=ai_account_code, "
            f"xero_account_name=ai_account_name WHERE id IN ({marks}) "
            "AND card_id=? AND statement_id=? AND category='spend' "
            "AND status='outstanding' AND xero_reconciled=0 "
            "AND xero_account_code IS NULL AND ai_account_code IS NOT NULL "
            "AND ai_confidence='high' AND COALESCE(ai_needs_review,0)=0",
            [*ids, card_id, statement_id])
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def apply_cc_account_to_statement_merchant(statement_id, merchant_key, account_code,
                                           account_name, normalize):
    """Set the Xero account on every OUTSTANDING spend line in `statement_id`
    whose merchant normalises to `merchant_key`. Returns the count updated.

    `normalize` is the merchant-key function (cc_ai.normalize_merchant), passed
    in so this data-layer helper stays free of the cc_ai import. Used by the
    admin "apply to all <merchant> this month" action so recurring spend codes
    in one click."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, reference FROM cc_lines WHERE statement_id=? "
            "AND category='spend' AND status='outstanding' AND xero_reconciled=0",
            (statement_id,)).fetchall()
        ids = [r['id'] for r in rows if normalize(r['reference']) == merchant_key]
        for lid in ids:
            conn.execute(
                "UPDATE cc_lines SET xero_account_code=?, xero_account_name=? WHERE id=?",
                (account_code or None, account_name or None, lid))
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def get_cc_merchant_map(merchant_key, conn=None):
    """Remembered account for a normalised merchant, or None."""
    own = conn is None
    if own:
        conn = get_db()
    try:
        return conn.execute(
            "SELECT account_code, account_name FROM cc_merchant_map WHERE merchant_key=?",
            (merchant_key,)).fetchone()
    finally:
        if own:
            conn.close()


def get_cc_merchant_map_family(brand, conn=None):
    """Every remembered row whose key starts with ``brand`` — the same merchant's
    other branches ('FUELSTOP MAIN ROAD', 'FUELSTOP CONVENIENCE', ...).

    Lets a never-seen branch inherit a decision already made for the brand. The
    caller decides whether the rows agree well enough to use (see
    ``cc_ai.resolve_remembered_account``); this is just the read.
    """
    if not brand:
        return []
    own = conn is None
    if own:
        conn = get_db()
    try:
        return conn.execute(
            "SELECT merchant_key, account_code, account_name, hits "
            "FROM cc_merchant_map WHERE merchant_key=? OR merchant_key LIKE ? "
            "ORDER BY hits DESC",
            (brand, brand.replace('%', '') + ' %')).fetchall()
    finally:
        if own:
            conn.close()


def get_cc_receipt_details_for_lines(line_ids, conn=None):
    """``{line_id: receipt-detail text}`` for lines that have a receipt linked.

    Reads the extraction output already stored on ``cc_receipts`` (its ``ai_raw_json``
    holds the line items and summary since the extraction schema was widened) and
    hands the coding model a short description of WHAT WAS BOUGHT. Without this the
    coding prompt only ever saw 'merchant + amount', which is why many lines
    were landing on fallback accounts.

    Links live in ``cc_receipt_lines``, not ``cc_lines.receipt_id``, so join through
    the junction table. Lines with no receipt are simply absent from the result.
    """
    ids = [int(i) for i in (line_ids or [])]
    if not ids:
        return {}
    own = conn is None
    if own:
        conn = get_db()
    try:
        out = {}
        # Chunked to stay under SQLite's variable limit on a big statement.
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ','.join('?' for _ in chunk)
            rows = conn.execute(
                f"SELECT rl.line_id AS line_id, r.ai_vendor AS ai_vendor, "
                f"r.ai_raw_json AS ai_raw_json FROM cc_receipt_lines rl "
                f"JOIN cc_receipts r ON r.id = rl.receipt_id "
                f"WHERE rl.line_id IN ({marks}) AND r.ai_status='processed' "
                f"ORDER BY rl.receipt_id", chunk).fetchall()
            for r in rows:
                text = _receipt_detail_text(r['ai_raw_json'], r['ai_vendor'])
                if not text:
                    continue
                # A line can have several receipts attached; keep them all, since
                # each one is evidence about the same charge.
                prev = out.get(r['line_id'])
                out[r['line_id']] = f"{prev} · {text}" if prev else text
        return out
    finally:
        if own:
            conn.close()


def _receipt_detail_text(raw_json, vendor):
    """Turn a stored extraction blob into a short 'what was bought' line.

    Tolerant by design: ``ai_raw_json`` rows predate the widened schema and PII
    scrubbing may have rewritten parts of the blob, so anything unparsable or
    missing yields '' rather than raising.
    """
    import json as _json
    try:
        d = _json.loads(raw_json or '') or {}
    except Exception:
        return ''
    if not isinstance(d, dict):
        return ''
    bits = []
    summary = (d.get('summary') or '').strip()
    if summary:
        bits.append(summary)
    items = d.get('line_items')
    if isinstance(items, list):
        names = [str(i).strip() for i in items if str(i or '').strip()][:20]
        if names:
            bits.append('Items: ' + '; '.join(names))
    if not bits and vendor:
        # Pre-enrichment receipts have no items; the extracted vendor is still
        # better evidence than the bank's mangled reference string.
        bits.append('Receipt vendor: {}'.format(vendor))
    if d.get('is_tax_invoice') is False:
        bits.append('(not a valid tax invoice — no VAT number printed)')
    return ' · '.join(bits)


def upsert_cc_merchant_map(merchant_key, account_code, account_name):
    """Remember (or update) the account for a merchant — global across all cards."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO cc_merchant_map (merchant_key, account_code, account_name) "
            "VALUES (?,?,?) ON CONFLICT(merchant_key) DO UPDATE SET "
            "account_code=excluded.account_code, account_name=excluded.account_name, "
            "hits=hits+1, updated_at=datetime('now')",
            (merchant_key, account_code, account_name))
        conn.commit()
    finally:
        conn.close()


def set_cc_statement_submitted(statement_id, by):
    conn = get_db()
    try:
        conn.execute("UPDATE cc_statements SET submitted_at=datetime('now'), submitted_by=? "
                     "WHERE id=?", (by, statement_id))
        conn.commit()
    finally:
        conn.close()


def reopen_cc_statement(statement_id):
    conn = get_db()
    try:
        conn.execute("UPDATE cc_statements SET submitted_at=NULL, submitted_by=NULL "
                     "WHERE id=?", (statement_id,))
        conn.commit()
    finally:
        conn.close()


# ── Re-exported repositories (gradual database split) ────────────────────────
# These live in northwind/data/repositories/* but are re-exported here so the
# `import database as db; db.function_name()` facade is unchanged.
from northwind.data.repositories.regional import (  # noqa: E402,F401
    get_rm_user, list_rm_users, upsert_rm_user, set_rm_active, delete_rm_user,
    get_rm_stores, get_store_rm, assign_store_rm, set_rm_card, rm_capabilities,
)
