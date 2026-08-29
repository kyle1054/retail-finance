"""Trim Bootstrap Icons down to the icons this app actually uses.

    python3 tools/subset_icons.py            # regenerate the served files
    python3 tools/subset_icons.py --check    # verify they're current (used by a test)
    python3 tools/subset_icons.py --list     # just print what's used

Bootstrap Icons ships 2,050 icons: a 96 KB stylesheet and a 127 KB font. This app
uses fewer than 200 of them. Every visitor downloads the other 1,860-odd for
nothing.

RE-RUN THIS AFTER ADDING AN ICON. A `bi-` class with no rule renders as *nothing*
— an invisible button, not an error — so the failure is silent. tests/
test_asset_delivery.py runs --check to catch it.

Sources of truth
----------------
The PRISTINE upstream files live in tools/icon-subset/ (bootstrap-icons.full.css
and .full.woff2) and the trimmed copies are generated into static/vendor/. The
originals must be kept: regenerating from an already-trimmed file could only ever
shrink the set, so a newly-added icon would be dropped forever. Replacing them is
how you upgrade Bootstrap Icons.

The `.woff` fallback in the @font-face is deliberately left pointing at the FULL,
untouched font. No browser new enough to run this app will ever fetch it (woff2
is universal since 2016), so it costs nothing on the wire — it is just insurance
that removes the "ancient browser silently loses every icon" failure mode.

How usage is detected
---------------------
Every `bi-...` token in templates/, the app's own static/ CSS+JS, and northwind/. Two
cases need care:

* Names built at render time — `bi-arrow-{{ 'up' if … else 'down' }}` in
  rm_dashboard.html. A token that is immediately followed by an interpolation or
  a Jinja tag is treated as a PREFIX and every icon starting with it is kept.
  That over-keeps a little; the alternative silently loses arrows.
* Names that don't exist upstream (typos, or icons from a newer Bootstrap Icons).
  Those are reported — they are already invisible in the app today, and
  subsetting neither causes nor fixes that.
"""
import argparse
import hashlib
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_CSS = os.path.join(ROOT, 'tools', 'icon-subset', 'bootstrap-icons.full.css')
SRC_FONT = os.path.join(ROOT, 'tools', 'icon-subset', 'bootstrap-icons.full.woff2')
OUT_CSS = os.path.join(ROOT, 'static', 'vendor', 'bootstrap-icons.css')
OUT_FONT = os.path.join(ROOT, 'static', 'vendor', 'fonts', 'bootstrap-icons.woff2')

# Where icon classes can appear. static/vendor/ is excluded on purpose: it holds
# the icon stylesheet itself, whose 2,050 rules are definitions, not usage.
SCAN_DIRS = [('templates', ('.html',)), ('northwind', ('.py',)), ('static', ('.js', '.css'))]
SCAN_SKIP = os.path.join('static', 'vendor')

ICON_RULE = re.compile(r'^\.(bi-[a-z0-9-]+)::before\s*\{\s*content:\s*"\\([0-9a-fA-F]+)";\s*\}\s*$')
# A token, plus whatever immediately follows it, so an interpolated tail is
# visible. The optional trailing '-' is what catches `bi-arrow-{{ 'up' … }}`.
TOKEN = re.compile(r'\bbi-[a-z0-9]+(?:-[a-z0-9]+)*-?(\{\{|\{%|\$\{)?')


def upstream_icons():
    """{icon name: codepoint} from the pristine upstream stylesheet."""
    icons = {}
    with open(SRC_CSS, encoding='utf-8') as fh:
        for line in fh:
            m = ICON_RULE.match(line)
            if m:
                icons[m.group(1)] = int(m.group(2), 16)
    return icons


def _scan_files():
    for folder, suffixes in SCAN_DIRS:
        base = os.path.join(ROOT, folder)
        for dirpath, dirnames, filenames in os.walk(base):
            if SCAN_SKIP in dirpath:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d != '__pycache__']
            for name in sorted(filenames):
                if name.endswith(suffixes):
                    yield os.path.join(dirpath, name)


def used_icons(available):
    """(kept icon names, names referenced that upstream doesn't define)."""
    kept, unknown = set(), {}
    for path in _scan_files():
        with open(path, encoding='utf-8', errors='replace') as fh:
            text = fh.read()
        for m in TOKEN.finditer(text):
            name = m.group(0)[:len(m.group(0)) - len(m.group(1) or '')]
            if m.group(1) is None:
                name = name.rstrip('-')
            if m.group(1):
                # Built at render time: keep the whole family under this prefix.
                matches = {i for i in available if i.startswith(name)}
                if matches:
                    kept |= matches
                    continue
            if name in available:
                kept.add(name)
            elif name != 'bi':
                unknown.setdefault(name, os.path.relpath(path, ROOT))
    return kept, unknown


def fingerprint(path):
    """Content hash of a file, in the SAME form northwind.core.static_fingerprint uses.

    The font URLs inside this stylesheet are plain CSS text, so the app's
    url_for() fingerprinting can't reach them — they'd be stuck revalidating on
    every page load while every other asset is cached for a year. Stamping the
    hash here gets them the same treatment. Both halves must agree: core.py only
    serves the immutable header when the URL's ?v= matches what it computes, so a
    mismatch costs a round trip (safe) rather than serving a stale font.
    tests/test_asset_delivery.py asserts the two stay in step.
    """
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b''):
            digest.update(chunk)
    return digest.hexdigest()[:10]


def build_css(kept, icons):
    """The upstream header + @font-face + base rules, then only the kept icons."""
    with open(SRC_CSS, encoding='utf-8') as fh:
        source = fh.read()
    head = source.split('\n.bi-', 1)[0]
    # Upstream ships its own fixed cache-buster query; swap it for this app's
    # content hash of the file we actually serve.
    for url, path in (('./fonts/bootstrap-icons.woff2', OUT_FONT),
                      ('./fonts/bootstrap-icons.woff', OUT_FONT.replace('.woff2', '.woff'))):
        head = re.sub(re.escape(url) + r'\?[0-9a-f]+',
                      url + '?v=' + fingerprint(path), head)
    lines = [head.rstrip('\n'), '', '/* Subset: %d of %d upstream icons — the ones this app uses.' % (len(kept), len(icons)),
             '   GENERATED by tools/subset_icons.py from tools/icon-subset/bootstrap-icons.full.css.',
             '   Do not hand-edit: adding an icon class to a template means re-running that',
             '   script, or the icon renders as nothing at all. */']
    lines += ['.%s::before { content: "\\%x"; }' % (name, icons[name]) for name in sorted(kept)]
    return '\n'.join(lines) + '\n'


def build_font(kept, icons):
    """Subset woff2, or None if fonttools/brotli aren't installed."""
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
    except ImportError:
        return None
    try:
        # recalcTimestamp=False keeps the output byte-identical across runs.
        # Otherwise every re-run stamps a new 'head' modified date, which changes
        # the file's content hash and needlessly evicts it from every browser
        # cache that already has it.
        font = TTFont(SRC_FONT, recalcTimestamp=False)
    except ImportError:          # fontTools raises this when brotli is missing
        return None
    options = subset.Options()
    options.layout_features = ['*']
    options.notdef_outline = True
    options.recalc_bounds = True
    options.drop_tables = []
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(unicodes=sorted(icons[n] for n in kept))
    subsetter.subset(font)
    font.flavor = 'woff2'
    buf = io.BytesIO()
    font.save(buf)
    font.close()
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true',
                    help='exit 1 if the served files are stale; write nothing')
    ap.add_argument('--list', action='store_true', help='print the icons in use and stop')
    args = ap.parse_args()

    icons = upstream_icons()
    kept, unknown = used_icons(icons)

    if args.list:
        for name in sorted(kept):
            print(name)
        return 0

    if args.check:
        # build_css hashes the font as it is ON DISK, so --check never depends on
        # fontTools producing byte-identical output twice.
        expected = build_css(kept, icons)
        current = ''
        if os.path.exists(OUT_CSS):
            with open(OUT_CSS, encoding='utf-8') as fh:
                current = fh.read()
        for name in sorted(unknown):
            print('warning: %s is used in %s but no such upstream icon exists — it '
                  'renders as nothing today, subset or not' % (name, unknown[name]))
        if current != expected:
            print('STALE: static/vendor/bootstrap-icons.css\n'
                  'Run: python3 tools/subset_icons.py')
            return 1
        print('up to date: %d icons' % len(kept))
        return 0

    # Font first — the stylesheet embeds its content hash.
    font = build_font(kept, icons)
    if font is None:
        print('SKIPPED the font subset: fontTools + brotli are not installed.\n'
              '  pip install fonttools brotli   then re-run.\n'
              '  %s is unchanged (still the full 2,050-glyph font). The CSS trim\n'
              '  below stands on its own; never hand-edit the binary.'
              % os.path.relpath(OUT_FONT, ROOT))
    else:
        before = os.path.getsize(OUT_FONT) if os.path.exists(OUT_FONT) else 0
        with open(OUT_FONT, 'wb') as fh:
            fh.write(font)
        print('wrote %s  (%.1f KB, was %.1f KB)'
              % (os.path.relpath(OUT_FONT, ROOT), len(font) / 1024.0, before / 1024.0))

    css = build_css(kept, icons)
    with open(OUT_CSS, 'w', encoding='utf-8') as fh:
        fh.write(css)
    print('wrote %s  (%d icons, %.1f KB)' % (
        os.path.relpath(OUT_CSS, ROOT), len(kept), len(css.encode()) / 1024.0))

    for name in sorted(unknown):
        print('warning: %s is used in %s but no such upstream icon exists — it '
              'renders as nothing today, subset or not' % (name, unknown[name]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
