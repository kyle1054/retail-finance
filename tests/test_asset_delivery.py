"""How static assets and page HTML reach the browser.

These lock in the Tier-1 delivery work. Each one guards a change whose failure
mode is quiet rather than loud:

* a fingerprint that stops changing pins a stale asset in every browser for a
  YEAR, and no deploy can reach in and fix it;
* an icon class with no CSS rule renders as nothing at all — an invisible
  button, not an error;
* a font URL whose hash drifts silently loses its long cache;
* trim_blocks can join two inline elements together, which is a visual bug that
  no test would otherwise notice.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, 'static')
FINGERPRINT = re.compile(r'^[0-9a-f]{10}$')


# ── content-addressed URLs ───────────────────────────────────────────────────

def test_static_urls_carry_a_content_fingerprint(client):
    from flask import url_for
    import app as a
    with a.app.test_request_context('/'):
        url = url_for('static', filename='style.css')
    assert '?v=' in url, 'static URLs must be versioned or they cannot be cached'
    assert FINGERPRINT.match(url.split('?v=')[1])


def test_fingerprinted_static_is_immutable_for_a_year(client):
    from flask import url_for
    import app as a
    with a.app.test_request_context('/'):
        url = url_for('static', filename='style.css')
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.headers['Cache-Control'] == 'public, max-age=31536000, immutable'


def test_unversioned_or_stale_static_is_not_cached_forever(client):
    """The one way this feature can hurt is serving a stale asset for a year.

    So the immutable header is granted only to a URL carrying the file's CURRENT
    hash. A bare URL, or one holding a previous deploy's hash, falls back to the
    old revalidate-every-time behaviour.
    """
    for url in ('/static/style.css', '/static/style.css?v=0123456789'):
        resp = client.get(url)
        assert resp.status_code == 200
        assert resp.headers['Cache-Control'] == 'no-cache', url


def test_changed_file_gets_a_new_url(client, tmp_path):
    """A deploy that changes a file MUST change its URL."""
    from northwind.core import static_fingerprint
    path = os.path.join(STATIC, 'style.css')
    original = open(path, 'rb').read()
    first = static_fingerprint('style.css')
    try:
        with open(path, 'wb') as fh:
            fh.write(original + b'\n/* touched by a test */\n')
        assert static_fingerprint('style.css') != first, \
            'edited file kept its old URL — browsers would never see the change'
    finally:
        with open(path, 'wb') as fh:
            fh.write(original)
    assert static_fingerprint('style.css') == first, 'restoring the bytes must restore the URL'


def test_fingerprint_is_content_based_not_timestamp_based(client):
    """Identical bytes must produce identical URLs, or every deploy would evict
    every asset from every cache for nothing."""
    from northwind.core import static_fingerprint
    path = os.path.join(STATIC, 'style.css')
    data = open(path, 'rb').read()
    first = static_fingerprint('style.css')
    os.utime(path, None)                      # touch: new mtime, same content
    assert static_fingerprint('style.css') == first
    assert open(path, 'rb').read() == data

def test_missing_static_file_still_renders(client):
    """An unknown filename must not raise during rendering."""
    from northwind.core import static_fingerprint
    assert static_fingerprint('does-not-exist.css') is None
    assert static_fingerprint('../deductions.db') is None   # no path escape either


def test_pages_are_still_no_store(client):
    """The long static cache must not have leaked onto authenticated HTML."""
    resp = client.get('/admin')
    assert resp.headers['Cache-Control'] == 'no-store'


# ── fonts ───────────────────────────────────────────────────────────────────

def test_inter_is_declared_once(client):
    """Six @font-face rules pointed at six byte-identical copies of the same
    variable font, so a page using four weights downloaded it four times."""
    css = open(os.path.join(STATIC, 'vendor', 'inter.css'), encoding='utf-8').read()
    assert len(re.findall(r'^@font-face', css, re.M)) == 1
    assert len(set(re.findall(r"url\('([^']+)'", css))) == 1
    assert 'font-weight: 100 900' in css, 'the file is a variable font; declare the range'


def test_font_urls_inside_stylesheets_carry_the_right_hash(client):
    """Font URLs live in plain CSS text, so url_for() can't fingerprint them —
    they are stamped by hand (inter.css) or by tools/subset_icons.py. If one
    drifts from the file it points at, that font silently loses its year cache."""
    from northwind.core import static_fingerprint
    for sheet in ('inter.css', 'bootstrap-icons.css'):
        css = open(os.path.join(STATIC, 'vendor', sheet), encoding='utf-8').read()
        refs = re.findall(r'url\([\'"]\./fonts/([^\'"?]+)\?v=([0-9a-f]+)[\'"]', css)
        assert refs, 'no fingerprinted font URL found in ' + sheet
        for filename, stamped in refs:
            actual = static_fingerprint('vendor/fonts/' + filename)
            assert actual == stamped, (
                '%s points at %s?v=%s but the file hashes to %s — re-run '
                'tools/subset_icons.py (or update inter.css)'
                % (sheet, filename, stamped, actual))


# ── icon subset ──────────────────────────────────────────────────────────────

def test_every_icon_the_app_uses_is_still_defined():
    """The subset must cover every `bi-` class in the app.

    A missing rule is invisible: the icon just doesn't paint. Re-run
    tools/subset_icons.py after adding one.
    """
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, 'tools', 'subset_icons.py'), '--check'],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_icon_css_is_actually_a_subset():
    css = open(os.path.join(STATIC, 'vendor', 'bootstrap-icons.css'), encoding='utf-8').read()
    rules = re.findall(r'^\.(bi-[a-z0-9-]+)::before', css, re.M)
    assert 100 < len(rules) < 700, 'expected a few hundred icons, got %d' % len(rules)
    assert '.bi::before' in css, 'the base rule must survive the trim'
    assert '@font-face' in css


# ── Jinja whitespace control ─────────────────────────────────────────────────

def test_jinja_trims_block_whitespace(client):
    import app as a
    assert a.app.jinja_env.trim_blocks
    assert a.app.jinja_env.lstrip_blocks
    rendered = a.app.jinja_env.from_string(
        '<ul>\n    {% for i in [1, 2] %}\n    <li>{{ i }}</li>\n    {% endfor %}\n</ul>'
    ).render()
    assert rendered == '<ul>\n    <li>1</li>\n    <li>2</li>\n</ul>'


def _whitespace_losses(source):
    """Every run of whitespace trim_blocks/lstrip_blocks removes from a template.

    Uses Jinja's own lexer, so it is exact and needs no render context — which
    means it covers every template, not just the ones a test client can reach.
    """
    from jinja2 import Environment

    def data_stream(trim):
        env = Environment(trim_blocks=trim, lstrip_blocks=trim)
        out = []
        for _, kind, value in env.lex(source):
            if kind == 'data':
                out.append(value)
            elif kind == 'variable_end':
                out.append('\x01')      # a variable prints *something*
        return ''.join(out)

    plain, trimmed = data_stream(False), data_stream(True)
    losses, i, j = [], 0, 0
    while i < len(plain) or j < len(trimmed):
        wb = i < len(plain) and plain[i].isspace()
        wa = j < len(trimmed) and trimmed[j].isspace()
        if wb and wa:
            while i < len(plain) and plain[i].isspace():
                i += 1
            while j < len(trimmed) and trimmed[j].isspace():
                j += 1
        elif wb:
            start = i
            while i < len(plain) and plain[i].isspace():
                i += 1
            losses.append((plain, start))
        elif wa:
            while j < len(trimmed) and trimmed[j].isspace():
                j += 1
        else:
            i += 1
            j += 1
    return losses


@pytest.mark.parametrize('name', sorted(
    f for f in os.listdir(os.path.join(ROOT, 'templates')) if f.endswith('.html')))
def test_no_template_loses_whitespace_where_it_is_significant(name):
    """trim_blocks is only safe because none of these sites matter.

    Whitespace between block elements, or inside a flex/grid container, is
    invisible. Whitespace inside <pre>/<textarea> or between the values of an
    attribute is NOT — `class="a b"` becoming `class="ab"` silently drops a
    class. This fails if a template ever introduces one of those.
    """
    source = open(os.path.join(ROOT, 'templates', name), encoding='utf-8').read()
    for text, at in _whitespace_losses(source):
        before = text[:at]
        open_tag, close_tag = before.rfind('<'), before.rfind('>')
        if open_tag > close_tag:
            segment = before[open_tag:]
            inside_value = segment.count('"') % 2 == 1 or segment.count("'") % 2 == 1
            # CSS declarations are ';'-separated, so losing the space between
            # two of them inside a style="" is harmless.
            assert not inside_value or before.rstrip()[-1:] == ';', (
                '%s: whitespace removed inside an attribute value near %r'
                % (name, before[-90:]))
        for tag in ('pre', 'textarea'):
            assert before.rfind('<' + tag) <= before.rfind('</' + tag), (
                '%s: whitespace removed inside <%s> near %r' % (name, tag, before[-90:]))
