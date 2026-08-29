"""Payroll roster sync — the shared service layer.

Both front doors use this: the web route (``routes_payroll.payroll_sync_preview`` /
``_apply``) and the MCP connector (``northwind_mcp.tools_payroll``). That is not a nicety, it
is the rule this codebase already had to learn the hard way — the connector previously
carried its own copy of the plan-adjust maths and had silently drifted from the route it
mirrored, using a different NULL-total fallback. The fix was one service layer, and
payroll sync is a much bigger surface to let drift: name matching, termination guards,
store history.

Three parts, all reusable:

* ``parse_text`` / ``parse_xlsx`` — a roster into ``{name_lower: {...}}``. Pure.
* ``resolve`` — roster + DB → the decision buckets (store moves, leavers, joiners,
  fuzzy name matches, ambiguous names, duplicate employees, new stores). Read-only;
  it decides nothing on its own and writes nothing.
* ``apply_decisions`` — the writes, given explicit selections. Takes a connection so the
  caller controls the transaction, which is what lets the MCP dry-run it and roll back.

**Retail only.** A payroll roster is a Retail operation, and scoping it strictly to
``sector='retail'`` is a safety property, not a filter: an uploaded retail roster must
never be able to match, move or terminate an HQ employee — an HQ person absent from a
retail roster would otherwise look like a leaver.
"""
import difflib
import re
from collections import defaultdict
from datetime import datetime

from northwind.data import database as db

# Fuzzy name matching. Compared on a sorted-token, punctuation-free canonical key
# (db.canonical_name) so a missing comma or a swapped first/last name lines up exactly
# (ratio 1.0) while a genuine typo still needs to clear the cutoff.
FUZZY_CUTOFF = 0.72


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def valid_payroll_name(name):
    """A roster name must be 'Surname, Firstname' — exactly one comma with a
    non-empty name on each side. Used to skip blank / header / malformed rows
    identically on the file and paste paths."""
    if not name or ',' not in name:
        return False
    parts = name.split(',')
    return len(parts) == 2 and parts[0].strip() != '' and parts[1].strip() != ''


def parse_text(pasted):
    """Parse pasted roster rows (as copied straight out of Excel) into
    {name_lower: {full_name, store, job_title}}. Columns are tab-separated —
    'Surname, Firstname' <tab> Store <tab> Job title, one per line — with a
    fallback to runs of 2+ spaces when a line has no tab. Blank, header, and
    comma-less name rows are skipped. Returns (payroll_employees, rows_read,
    skipped)."""
    payroll_employees = {}
    rows_read = 0
    skipped = 0
    for line in (pasted or '').splitlines():
        if not line.strip():
            continue  # blank line between blocks — not counted as skipped
        parts = line.split('\t') if '\t' in line else re.split(r' {2,}', line.strip())
        name = parts[0].strip() if parts else ''
        if not valid_payroll_name(name):
            skipped += 1
            continue
        store = parts[1].strip() if len(parts) > 1 else ''
        title = parts[2].strip() if len(parts) > 2 else ''
        payroll_employees[name.lower()] = {
            'full_name': name, 'store': store, 'job_title': title
        }
        rows_read += 1
    return payroll_employees, rows_read, skipped


def parse_xlsx(raw):
    """Parse a payroll Excel export into {name_lower: {full_name, store,
    job_title}}. Returns (payroll_employees, detected_sheet). Raises ValueError
    with a user-facing message on a structural problem."""
    import pandas as pd
    import io as _io

    try:
        xl = pd.ExcelFile(_io.BytesIO(raw))
    except Exception as e:
        raise ValueError(f'Could not open file: {e}')

    payroll_employees = {}
    detected_sheet = None
    try:
        for sheet_name in xl.sheet_names:
            try:
                df = pd.read_excel(_io.BytesIO(raw), sheet_name=sheet_name, header=None)
            except Exception:
                continue
            if df.empty or df.shape[1] < 1:
                continue
            start_row = None
            for i in range(min(15, len(df))):
                val = df.iloc[i, 0] if df.shape[1] > 0 else None
                if pd.notna(val) and isinstance(val, str) and ',' in val and len(val.split(',')) == 2:
                    start_row = i
                    break
            if start_row is None:
                continue
            detected_sheet = sheet_name
            for i in range(start_row, len(df)):
                name = df.iloc[i, 0] if df.shape[1] > 0 else None
                store = df.iloc[i, 1] if df.shape[1] > 1 else None
                title = df.iloc[i, 2] if df.shape[1] > 2 else None
                if pd.notna(name) and isinstance(name, str) and ',' in name:
                    full_name = name.strip()
                    store_str = str(store).strip() if pd.notna(store) else ''
                    title_str = str(title).strip() if pd.notna(title) else ''
                    payroll_employees[full_name.lower()] = {
                        'full_name': full_name, 'store': store_str, 'job_title': title_str
                    }
            break
    except Exception as e:
        raise ValueError(f'An error occurred while parsing the Excel file structure: {e}. '
                         'Please check the sheet layout.')

    return payroll_employees, detected_sheet


# --------------------------------------------------------------------------- #
# Resolution (read-only)
# --------------------------------------------------------------------------- #
def resolve(payroll_employees, conn=None):
    """Compare a parsed roster against the active retail employees.

    Returns the decision buckets. Nothing is written and nothing is decided — a caller
    picks which of these to act on, which is exactly what the web form and the MCP
    ``decisions`` argument each do.

    Only *uniquely* named employees take part in auto-matching. A duplicated name is
    surfaced in ``ambiguous`` and is never auto-moved or auto-terminated, because
    choosing between two real people on a name alone is not a decision a machine should
    make.
    """
    own = conn is None
    if own:
        conn = db.get_db()
    try:
        db_emps = conn.execute(
            "SELECT id, full_name, current_store, job_title FROM employees "
            "WHERE status='active' AND sector='retail'"
        ).fetchall()
    finally:
        if own:
            conn.close()

    by_name = defaultdict(list)
    for e in db_emps:
        by_name[e['full_name'].lower().strip()].append(dict(e))

    db_lookup = {name: emps[0] for name, emps in by_name.items() if len(emps) == 1}
    ambiguous = [
        {'full_name': emps[0]['full_name'], 'count': len(emps),
         'stores': ', '.join(sorted({x['current_store'] or '—' for x in emps}))}
        for name, emps in by_name.items() if len(emps) > 1
    ]

    # Possible duplicate employees in the roster itself — names that collapse to
    # the same canonical key (exact, swapped first/last, or a missing comma),
    # which a plain name match would miss. Same store ⇒ near-certain mistaken
    # twin; different stores ⇒ flag, but it could be two real same-named people.
    canon_groups = defaultdict(list)
    for e in db_emps:
        canon_groups[db.canonical_name(e['full_name'])].append(dict(e))
    duplicate_employees = [
        {'members': members,
         'same_store': len({(m['current_store'] or '').strip().lower() for m in members}) == 1}
        for key, members in canon_groups.items() if key and len(members) > 1
    ]
    duplicate_employees.sort(
        key=lambda g: (not g['same_store'], g['members'][0]['full_name'].lower()))
    payroll_keys = {key.lower().strip(): p for key, p in payroll_employees.items()}

    exact_db_keys = set(db_lookup.keys()) & set(payroll_keys.keys())
    unmatched_db_keys = set(db_lookup.keys()) - set(payroll_keys.keys())
    unmatched_payroll_keys = set(payroll_keys.keys()) - set(db_lookup.keys())

    fuzzy_matches = []
    matched_db_keys = set()
    matched_payroll_keys = set()

    db_canon = {}
    for raw_key in unmatched_db_keys:
        db_canon.setdefault(db.canonical_name(raw_key), raw_key)

    # Sorted for determinism: set iteration order varies between processes, and an
    # unstable fuzzy pairing would make a preview token disagree with its own apply.
    for p_key in sorted(unmatched_payroll_keys):
        p = payroll_keys[p_key]
        available = {c: k for c, k in db_canon.items() if k not in matched_db_keys}
        if not available:
            break
        p_canon = db.canonical_name(p_key)
        close_matches = difflib.get_close_matches(
            p_canon, sorted(available.keys()), n=1, cutoff=FUZZY_CUTOFF)
        if close_matches:
            db_key = available[close_matches[0]]
            candidate = db_lookup[db_key]
            score = round(difflib.SequenceMatcher(
                None, p_canon, close_matches[0]).ratio() * 100)

            fuzzy_matches.append({
                'db_id': candidate['id'],
                'db_name': candidate['full_name'],
                'db_old_store': candidate['current_store'],
                'payroll_name': p['full_name'],
                'payroll_new_store': p['store'],
                'payroll_title': p['job_title'],
                'score': score
            })

            matched_db_keys.add(db_key)
            matched_payroll_keys.add(p_key)

    store_changes = []
    for key in sorted(exact_db_keys):
        emp = db_lookup[key]
        p = payroll_keys[key]
        if p['store'] and emp['current_store'] != p['store']:
            store_changes.append({
                'id': emp['id'],
                'full_name': emp['full_name'],
                'old_store': emp['current_store'],
                'new_store': p['store'],
                'job_title': p['job_title'] or emp['job_title']
            })

    not_in_payroll = []
    outstanding_totals = db.get_outstanding_totals()
    for key in sorted(unmatched_db_keys - matched_db_keys):
        emp = db_lookup[key]
        not_in_payroll.append({
            'id': emp['id'],
            'full_name': emp['full_name'],
            'current_store': emp['current_store'],
            'outstanding': outstanding_totals.get(emp['id'], 0.0)
        })

    new_in_payroll = []
    for key in sorted(unmatched_payroll_keys - matched_payroll_keys):
        p = payroll_keys[key]
        new_in_payroll.append({
            'full_name': p['full_name'],
            'store': p['store'],
            'job_title': p['job_title']
        })

    existing_stores = set(db.get_stores())
    new_stores = sorted({
        p['new_store'] for p in store_changes
        if p['new_store'] not in existing_stores
    } | {
        p['store'] for p in new_in_payroll
        if p['store'] not in existing_stores
    } | {
        p['payroll_new_store'] for p in fuzzy_matches
        if p['payroll_new_store'] not in existing_stores
    })

    return {
        'payroll_count': len(payroll_employees),
        'store_changes': store_changes,
        'not_in_payroll': not_in_payroll,
        'new_in_payroll': new_in_payroll,
        'fuzzy_matches': fuzzy_matches,
        'ambiguous': ambiguous,
        'duplicate_employees': duplicate_employees,
        'new_stores': new_stores,
    }


# --------------------------------------------------------------------------- #
# Application (writes)
# --------------------------------------------------------------------------- #
def effective_date(period_year=None, period_month=None):
    """When a move/termination takes effect: the payroll month being processed if
    given, else now. Keeps store history correctly dated when a backdated or future
    roster is applied."""
    if period_year and period_month and 1 <= period_month <= 12:
        return datetime(period_year, period_month, 1).isoformat()
    return datetime.now().isoformat()


def apply_decisions(conn, *, moves=(), terminations=(), fuzzy_links=(), additions=(),
                    new_stores=(), force_terminate=(), effective=None,
                    outstanding=None):
    """Apply the selected roster decisions on `conn`. Does NOT commit — the caller owns
    the transaction, which is what lets a preview run this and roll back.

    Shapes (each an explicit selection, never inferred):
      moves         [{id, new_store}]
      terminations  [employee_id, ...]
      fuzzy_links   [{id, full_name?, store?, job_title?}]
      additions     [{full_name, store, job_title}]
      new_stores    ['Store name', ...]
      force_terminate  ids allowed to terminate despite owing money

    Returns a counts dict including ``kept_owing``: employees NOT terminated because
    they still owe money. That guard is deliberate — a leaver with an outstanding
    balance whose record is closed silently abandons the debt, so it takes an explicit
    override, and the count is reported rather than swallowed.
    """
    now_str = effective or effective_date()
    if outstanding is None:
        outstanding = db.get_outstanding_totals()
    force = set(force_terminate or ())

    counts = {'moved': 0, 'terminated': 0, 'added': 0, 'linked': 0,
              'kept_owing': 0, 'stores_added': 0, 'kept_owing_ids': []}

    for name in new_stores or ():
        cleaned = (name or '').strip()
        if not cleaned:
            continue
        conn.execute("INSERT OR IGNORE INTO stores (name) VALUES (?)", (cleaned,))
        counts['stores_added'] += 1

    for move in moves or ():
        emp_id = move.get('id')
        new_store = (move.get('new_store') or '').strip()
        if not emp_id or not new_store:
            continue
        _move_store(conn, emp_id, new_store, now_str)
        counts['moved'] += 1

    for emp_id in terminations or ():
        if outstanding.get(emp_id, 0) > 0 and emp_id not in force:
            counts['kept_owing'] += 1
            counts['kept_owing_ids'].append(emp_id)
            continue
        conn.execute(
            "UPDATE employees SET status='terminated', terminated_at=? WHERE id=?",
            (now_str, emp_id))
        conn.execute(
            "UPDATE store_history SET to_date=? WHERE employee_id=? AND to_date IS NULL",
            (now_str, emp_id))
        counts['terminated'] += 1

    for link in fuzzy_links or ():
        emp_id = link.get('id')
        if not emp_id:
            continue
        new_name = (link.get('full_name') or '').strip()
        new_store = (link.get('store') or '').strip()
        new_title = (link.get('job_title') or '').strip()
        if new_name:
            conn.execute("UPDATE employees SET full_name=?, job_title=? WHERE id=?",
                         (new_name, new_title, emp_id))
        emp = conn.execute("SELECT current_store FROM employees WHERE id=?",
                           (emp_id,)).fetchone()
        if emp and new_store and emp['current_store'] != new_store:
            _move_store(conn, emp_id, new_store, now_str)
        counts['linked'] += 1

    for add in additions or ():
        new_name = (add.get('full_name') or '').strip()
        if not new_name:
            continue
        new_store = (add.get('store') or '').strip()
        new_title = (add.get('job_title') or '').strip()
        emp_id = db.next_employee_id(conn)
        # Always retail — payroll sync only ever operates on retail staff.
        conn.execute(
            "INSERT INTO employees (id, full_name, current_store, job_title, sector) "
            "VALUES (?, ?, ?, ?, 'retail')",
            (emp_id, new_name, new_store, new_title))
        conn.execute(
            "INSERT INTO store_history (employee_id, store, from_date) VALUES (?, ?, ?)",
            (emp_id, new_store, now_str))
        counts['added'] += 1

    return counts


def _move_store(conn, emp_id, new_store, now_str):
    """Close the open store_history row and open a new one — the move is a history
    event, not just a column update, so a later report can say where someone was."""
    conn.execute(
        "UPDATE store_history SET to_date=? WHERE employee_id=? AND to_date IS NULL",
        (now_str, emp_id))
    conn.execute(
        "INSERT INTO store_history (employee_id, store, from_date) VALUES (?,?,?)",
        (emp_id, new_store, now_str))
    conn.execute("UPDATE employees SET current_store=? WHERE id=?",
                 (new_store, emp_id))
