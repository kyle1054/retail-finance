"""The purged Bootstrap stylesheet must still cover every class we render.

`tools/purge_bootstrap_css.py` drops roughly two thirds of vendored Bootstrap.
It is the only change in this performance work that can alter what a person
SEES, and its failure mode is silent: a dropped rule doesn't raise, the modal
just opens invisible. So the contract is checked from the other end — render the
real pages, pull every class out of the HTML that actually came back, and assert
that every Bootstrap selector which *could match that markup* survived.

"Could match" matters. `.icon-link>.bi` mentions `bi`, which every page renders,
but nothing here ever carries `icon-link`, so dropping that rule changes nothing.
The check therefore requires a selector only when ALL of its classes are on the
page (or safelisted) — the same rule the purge itself applies.

If this fails after a template change, re-run `python3 tools/purge_bootstrap_css.py`.
"""
import os
import re
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import purge_bootstrap_css as purge   # noqa: E402


def _selector_pairs(nodes, chain=()):
    """(at-rule context, one selector) for every selector in the sheet.

    The context is carried so a rule kept inside `@media (min-width:768px)` is
    not mistaken for the same selector at top level.
    """
    for node in nodes:
        if node[0] == 'rule':
            for sel in purge.split_selectors(node[1]):
                yield (chain, sel)
        elif node[0] == 'nested':
            for pair in _selector_pairs(node[2], chain + (node[1],)):
                yield pair


@pytest.fixture(scope='module')
def sheets():
    assert os.path.exists(purge.OUTPUT), 'run python3 tools/purge_bootstrap_css.py'
    original = purge.parse(open(purge.SOURCE, encoding='utf-8').read())
    trimmed = purge.parse(open(purge.OUTPUT, encoding='utf-8').read())
    return list(_selector_pairs(original)), set(_selector_pairs(trimmed))


_CLASS_ATTR = re.compile(r'''class=["']([^"']*)["']''')


def _rendered_classes(client, urls):
    seen = set()
    for url in urls:
        resp = client.get(url)
        if resp.status_code >= 400:
            continue
        for attr in _CLASS_ATTR.findall(resp.get_data(as_text=True)):
            seen.update(attr.split())
    return seen


def _uncovered(sheets, rendered):
    """Selectors that could style the rendered markup but are no longer there."""
    original, trimmed = sheets
    lost = []
    for pair in original:
        needed = purge.required_classes(pair[1])
        if not needed:
            continue           # element/attribute rules are always kept
        if all(n in rendered or n in purge.SAFELIST
               or n.startswith(purge.SAFELIST_PREFIXES) for n in needed):
            if pair not in trimmed:
                lost.append(pair[1])
    return sorted(set(lost))


def _admin_urls():
    now = datetime.now()
    return [
        '/', '/admin', '/admin/login', '/employees', '/uniforms', '/laybys',
        '/undercharges', '/invoice-search', '/monthly',
        f'/monthly/{now.year}/{now.month}', '/stores', '/payroll/sync',
        '/payroll/sheet', '/payroll/reconcile', '/import-center', '/activity',
        '/cash', '/cards', '/cards/review', '/cards/upload',
        '/hq/employees', '/hq/laybys', '/hq/monthly',
        '/admin/admins', '/admin/staff-logins', '/admin/change-password',
    ]


def test_purged_sheet_covers_every_class_the_admin_pages_render(client, sheets):
    rendered = _rendered_classes(client, _admin_urls())
    assert len(rendered) > 200, 'pages did not render — coverage check is vacuous'
    lost = _uncovered(sheets, rendered)
    assert not lost, f'purged Bootstrap no longer styles rendered markup: {lost}'


def test_purged_sheet_covers_every_class_the_staff_portal_renders(staff_client, sheets):
    c, emp = staff_client
    rendered = _rendered_classes(c, ['/portal/store', f"/portal/store/{emp['id']}"])
    assert rendered, 'portal did not render'
    assert not _uncovered(sheets, rendered)


def test_the_login_shells_are_covered(client, sheets):
    """The unauthenticated shells load Bootstrap too, and nobody clicks through
    them while testing a feature."""
    rendered = _rendered_classes(client, ['/', '/admin/login'])
    assert not _uncovered(sheets, rendered)


@pytest.mark.parametrize('selector', [
    # Modal — bootstrap.Modal is called from 15 places in our JS.
    '.modal.show .modal-dialog', '.modal-backdrop', '.modal-backdrop.show',
    '.fade', '.fade:not(.show)',
    # Collapse — 16 data-bs-toggle="collapse" sites (sidebar groups, filters).
    '.collapsing', '.collapse:not(.show)',
    # Dropdown — 4 sites (account menu, export menus).
    '.dropdown-menu.show',
    # Offcanvas — 1 site.
    '.offcanvas.showing', '.offcanvas.hiding',
    # Tab — 2 sites.
    '.nav-tabs .nav-link.active', '.tab-content>.active',
    # Toast — base.html renders flashes through Bootstrap's Toast.
    '.toast.showing',
])
def test_javascript_runtime_selectors_survive(sheets, selector):
    """Bootstrap's own JS adds these classes; they appear in NO template of ours,
    so the content scanner cannot see them and only the safelist keeps them.
    Missing one means a modal that opens invisible or a dropdown that never
    shows — with no error anywhere."""
    _, trimmed = sheets
    assert any(sel == selector for _, sel in trimmed), \
        f'runtime selector {selector!r} was purged'


def test_css_variables_and_keyframes_are_kept():
    """style.css overrides Bootstrap and reads its custom properties, and the
    spinner/progress animations are keyframe-driven."""
    css = open(purge.OUTPUT, encoding='utf-8').read()
    original = open(purge.SOURCE, encoding='utf-8').read()
    assert ':root' in css
    for var in ('--bs-primary', '--bs-body-bg', '--bs-border-color',
                '--bs-body-font-family'):
        assert var in css, f'{var} is read by style.css'
    assert css.count('@keyframes') == original.count('@keyframes')
    # A redistribution build step must not strip the MIT license notice.
    assert 'Licensed under MIT' in css


def test_purge_output_is_not_stale():
    """The committed artifact must match what the tool produces from the current
    templates — otherwise a template gained a class that has no rule."""
    _, _, output, _, _ = purge.build()
    assert open(purge.OUTPUT, encoding='utf-8').read() == output, (
        'static/vendor/bootstrap.purged.css is stale — re-run '
        'python3 tools/purge_bootstrap_css.py')


def test_purged_css_still_parses_to_balanced_output():
    css = open(purge.OUTPUT, encoding='utf-8').read()
    assert css.count('{') == css.count('}')
    assert purge.render(purge.parse(css)) == css, 'output is not round-trip stable'


def test_purge_is_a_real_saving():
    original = os.path.getsize(purge.SOURCE)
    trimmed = os.path.getsize(purge.OUTPUT)
    assert trimmed < original * 0.75, 'purge saved almost nothing — check the scanner'
    assert trimmed > 20 * 1024, 'purge looks catastrophically over-aggressive'
