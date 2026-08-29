"""Guard: no Python object may leak its repr into a rendered page.

The bug this exists for: a route passed `totals = {'items': len(items), ...}` and
the template said `{{ totals.items }}`. Jinja resolves an ATTRIBUTE before a key,
and `dict.items` is a real method — so the page shipped

    <built-in method items of dict object at 0x10794cf80> items

to the live undercharges screen instead of the count. Nothing failed: the route
returned 200, the template compiled, and every existing test passed, because no
test looked at that span. Only rendering the page and reading it catches this.

It generalises past that one key. Any object handed to a template that is never
called or subscripted renders its repr, and Jinja escapes the angle brackets so
it survives as visible text rather than breaking the markup. The dict/Row method
names that can shadow a same-named key are enumerated in
`test_no_template_shadows_a_container_method` below.
"""
import re

import pytest

from northwind.data import database as db

# Rendered pages that must be clean. Kept broad on purpose: the failure mode is
# per-template, so coverage here is what makes the guard worth having.
ADMIN_URLS = [
    '/admin', '/employees', '/undercharges', '/laybys', '/uniforms',
    '/monthly', '/stores', '/activity', '/cash',
    '/cards', '/cards/review', '/cards/upload', '/import-center',
    '/hq/employees', '/hq/laybys', '/hq/monthly', '/hq/allowances',
    '/admin/admins', '/admin/staff-logins', '/admin/regional-managers',
    '/cash/xero-setup', '/invoice-search',
]

# Jinja escapes '<' to '&lt;', so a leaked repr shows up escaped in the body.
# Match both spellings — a future |safe would produce the raw form.
LEAK_PATTERNS = [
    (r'(?:&lt;|<)built-in method ', 'built-in method repr'),
    (r'(?:&lt;|<)bound method ', 'bound method repr'),
    (r'(?:&lt;|<)function ', 'function repr'),
    (r'(?:&lt;|<)generator object ', 'generator repr'),
    (r'(?:&lt;|<)(?:filter|map|zip) object ', 'lazy iterator repr'),
    (r'(?:&lt;|<)class ', 'class repr'),
    (r' object at 0x[0-9a-fA-F]+', 'instance repr'),
]


def _assert_clean(url, body):
    for pattern, label in LEAK_PATTERNS:
        match = re.search(pattern, body)
        if match:
            start = max(0, match.start() - 90)
            pytest.fail(
                f'{url} leaked a {label} into the page:\n'
                f'  …{body[start:match.end() + 90]}…\n'
                f'Usually a dict key shadowed by a method: use '
                f"obj['key'] instead of obj.key.")


@pytest.mark.parametrize('url', ADMIN_URLS)
def test_admin_pages_render_no_python_reprs(client, url):
    r = client.get(url, follow_redirects=True)
    assert r.status_code == 200, f'{url} returned {r.status_code}'
    _assert_clean(url, r.get_data(as_text=True))


def test_detail_pages_render_no_python_reprs(client, conn):
    """Row-detail pages, which carry the richest template context."""
    emp = conn.execute('SELECT id FROM employees ORDER BY id LIMIT 1').fetchone()
    store = conn.execute('SELECT name FROM stores LIMIT 1').fetchone()
    card = conn.execute('SELECT id FROM cc_cards LIMIT 1').fetchone()

    urls = []
    if emp:
        urls.append(f'/employees/{emp["id"]}')
    if store:
        urls.append(f'/cash/{store["name"]}/2026/7')
    if card:
        urls.append(f'/cards/{card["id"]}')

    assert urls, 'dev DB has no rows to build a detail URL from'
    for url in urls:
        r = client.get(url, follow_redirects=True)
        assert r.status_code == 200, f'{url} returned {r.status_code}'
        _assert_clean(url, r.get_data(as_text=True))


def test_store_pages_render_no_python_reprs(staff_client):
    """The store portal renders under a different base template."""
    c, emp = staff_client
    for url in ('/portal/store', f'/cash/{emp["current_store"]}/2026/7'):
        r = c.get(url, follow_redirects=True)
        if r.status_code != 200:
            continue
        _assert_clean(url, r.get_data(as_text=True))


# Names on dict / sqlite3.Row that win over a same-named key in `{{ obj.name }}`.
# `count` and `index` are deliberately ABSENT: dict has neither, so `totals.count`
# safely resolves to the key. They ARE methods on list/str, hence the note below.
SHADOWING_NAMES = sorted(
    set(m for m in dir(dict) if not m.startswith('_'))
    | set(m for m in dir({}.keys()) if not m.startswith('_'))
)


def test_shadowing_name_list_is_accurate():
    """Documents why `.count`/`.index` are safe on a dict but not on a list."""
    assert 'items' in SHADOWING_NAMES and 'get' in SHADOWING_NAMES
    assert 'count' not in SHADOWING_NAMES, 'dict grew a .count — widen the guard'
    assert hasattr([], 'count'), 'lists DO have .count; never use it as a key'


def test_no_template_shadows_a_container_method():
    """Static sweep, so a new `{{ totals.items }}` fails before it reaches a page.

    Only flags bare attribute access — `x.get(...)` and `x.items()` with a call
    are intentional method calls and are left alone.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / 'templates'
    # {{ … }} or {% … %}, then obj.name NOT followed by '(' — i.e. not a call.
    expr = re.compile(r'\{\{(.*?)\}\}|\{%(.*?)%\}', re.S)
    access = re.compile(
        r'\b(\w+)\.(' + '|'.join(SHADOWING_NAMES) + r')\b\s*(?!\()')

    offenders = []
    for path in sorted(root.rglob('*.html')):
        text = path.read_text(encoding='utf-8')
        for match in expr.finditer(text):
            code = match.group(1) or match.group(2) or ''
            for obj, attr in access.findall(code):
                line = text[:match.start()].count('\n') + 1
                offenders.append(
                    f'{path.relative_to(root.parent)}:{line}  {obj}.{attr}  '
                    f"→ use {obj}['{attr}']")

    assert not offenders, (
        'Template attribute access shadowed by a container method — this renders '
        'the method\'s repr, not the value:\n  ' + '\n  '.join(offenders))
