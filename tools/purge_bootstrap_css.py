"""Emit a trimmed copy of vendored Bootstrap holding only the classes we use.

    python3 tools/purge_bootstrap_css.py            # write static/vendor/bootstrap.purged.css
    python3 tools/purge_bootstrap_css.py --report    # what got dropped, and why
    python3 tools/purge_bootstrap_css.py --check     # exit 1 if the output is stale

`bootstrap.min.css` is 227 KB and every page loads it, including the six login /
shell templates. Bootstrap classes really are used broadly here (card 299×,
btn 285×, modal 173×), so this is not a "delete the framework" exercise — it
drops the components nobody references (accordion, carousel, breadcrumb,
pagination, popover, tooltip) and the contextual variants of components we only
use in one or two colours.

RE-RUN THIS after any template or front-end JS change. The purged file is a
build artifact; `bootstrap.min.css` stays in the tree untouched, so a bad purge
is a one-line revert of the <link> in the six base templates.

How a rule survives
-------------------
Every class name Bootstrap defines is looked up as a whole token in the scanned
content (templates + our own JS/CSS). A selector is kept when every class it
*requires* is used; classes inside :not()/:is()/:where() are not required,
because `.btn:not(.disabled)` must survive even if nothing ever carries
`.disabled`. Selectors with no class at all (Reboot's element rules, :root,
[data-bs-theme], ::selection) are always kept, as are @keyframes and the
license banner.

The dangerous gap is Bootstrap's OWN JavaScript, which adds classes that appear
in no template of ours — miss `show` and every modal opens invisible. Those live
in SAFELIST below with a note on which component needs them.
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, 'static', 'vendor', 'bootstrap.min.css')
OUTPUT = os.path.join(ROOT, 'static', 'vendor', 'bootstrap.purged.css')

# Files that can put a class on an element. Vendored assets are excluded: they
# are the framework itself, so scanning them would keep everything.
CONTENT_DIRS = [
    (os.path.join(ROOT, 'templates'), ('.html',)),
    (os.path.join(ROOT, 'static'), ('.js', '.css')),
]
SKIP_DIRS = {'vendor', 'fonts'}


# ── Safelist ────────────────────────────────────────────────────────────────
# Classes that are never written in a template, so the scanner cannot see them.
# Each entry says which component would break without it. Dropping one of these
# does not raise an error anywhere — the modal just silently never appears.
SAFELIST = {
    # bootstrap.bundle.min.js toggles these directly (Modal, Dropdown, Collapse,
    # Tab, Offcanvas, Toast are all in use here — see data-bs-toggle counts).
    'show',                  # every component's visible state
    'showing', 'hiding',     # Offcanvas + Toast transition states
    'hide',                  # Toast's pre-5.3 hidden state (5.3 defines no rule)
    'fade',                  # Modal/Tab/Toast transition wrapper
    'collapsing',            # Collapse mid-animation height transition
    'collapsed',             # the *toggler*, not the panel (chevron rotation)
    'active',                # Tab panes, nav links, dropdown items, buttons
    'disabled',              # JS-disabled nav links / dropdown items
    'modal-open',            # Modal put this on <body> in v4; v5.3 defines no
                             # rule for it, so this entry is future-proofing only
    'modal-backdrop',        # Modal builds this element from scratch
    'modal-static',          # the shake when a static backdrop is clicked
    'offcanvas-backdrop',    # Offcanvas builds this element from scratch
    'dropdown-menu',         # Dropdown reads/repositions it; also template-used
    'dropdown-menu-end',     # flipped alignment applied by Popper
    'dropup', 'dropend', 'dropstart',   # Dropdown's auto-flip direction classes

    # Built by string concatenation in our own JS, so the scanner only ever sees
    # the prefix (e.g. `btn-outline-${outline}` in payroll-reconcile.js).
    'btn-outline-warning', 'btn-outline-danger',   # payroll-reconcile.js:51
    'text-warning', 'text-danger',                 # payroll-reconcile.js:49,51
    'bg-primary', 'bg-info',                       # `bg-{{ r.badge }}` in search results

    # Bootstrap's own validation hook: added to a <form> by JS, never authored.
    'was-validated',
}

# Whole families kept by prefix because a colour/breakpoint variant can be
# assembled at runtime. Cheap in bytes, and the failure mode of getting one
# wrong is an unstyled badge or button in a page nobody re-tested.
SAFELIST_PREFIXES = (
    'bg-',            # bg-{{ r.badge }}, and status badges across the lists
    'text-bg-',       # Bootstrap 5.3's badge/toast colour pairing
    'btn-',           # btn-outline-${outline}
    'alert-',         # flash categories come from Python, not the template
)


def _iter_content_files():
    for base, exts in CONTENT_DIRS:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in sorted(filenames):
                if name.endswith(exts):
                    yield os.path.join(dirpath, name)


_TOKEN = re.compile(r'[A-Za-z0-9_-]+')


def used_tokens():
    """Every identifier-shaped token in our own markup and scripts.

    Deliberately not limited to class="…" attributes: classes are also added by
    classList.add('…'), built into innerHTML strings, and spliced in by Jinja
    conditionals. Tokenising the whole file catches all three. It over-keeps
    (a Python-ish word that happens to equal a class name), which is the right
    direction to be wrong in.
    """
    tokens = set()
    for path in _iter_content_files():
        with open(path, encoding='utf-8', errors='replace') as fh:
            tokens.update(_TOKEN.findall(fh.read()))
    return tokens


# ── A very small CSS reader ─────────────────────────────────────────────────
# Enough for a minified stylesheet: strings, comments, url(), balanced braces.
def _scan_to(css, i, stop):
    """Advance past strings/comments/url() until one of `stop` at depth 0."""
    depth = 0
    while i < len(css):
        c = css[i]
        if c in '"\'':
            i += 1
            while i < len(css) and css[i] != c:
                i += 2 if css[i] == '\\' else 1
            i += 1
            continue
        if css.startswith('/*', i):
            end = css.find('*/', i + 2)
            i = len(css) if end == -1 else end + 2
            continue
        if c in '([':
            depth += 1
        elif c in ')]':
            depth -= 1
        elif depth == 0 and c in stop:
            return i
        i += 1
    return i


def parse(css):
    """Parse into a list of nodes.

    ('banner', text)      a /*! … */ license comment (kept verbatim)
    ('statement', text)   an at-rule with no block, e.g. @charset
    ('verbatim', text)    an at-rule whose body we never touch (@keyframes)
    ('nested', prelude, [nodes])   @media / @supports — recursed into
    ('rule', prelude, body)        a normal selector rule
    """
    nodes, i = [], 0
    while i < len(css):
        if css[i].isspace():
            i += 1
            continue
        if css.startswith('/*', i):
            end = css.find('*/', i + 2)
            end = len(css) if end == -1 else end + 2
            chunk = css[i:end]
            if chunk.startswith('/*!'):
                nodes.append(('banner', chunk))
            i = end
            continue
        stop = _scan_to(css, i, '{;')
        if stop >= len(css):
            break
        prelude = css[i:stop].strip()
        if css[stop] == ';':
            if prelude:
                nodes.append(('statement', prelude + ';'))
            i = stop + 1
            continue
        # Balanced block body.
        depth, j = 1, stop + 1
        while j < len(css) and depth:
            k = _scan_to(css, j, '{}')
            if k >= len(css):
                break
            depth += 1 if css[k] == '{' else -1
            j = k + 1
        body = css[stop + 1:j - 1]
        if prelude.startswith('@'):
            name = prelude.split(None, 1)[0].lower()
            if name in ('@media', '@supports', '@layer', '@container'):
                nodes.append(('nested', prelude, parse(body)))
            else:
                nodes.append(('verbatim', f'{prelude}{{{body}}}'))
        else:
            nodes.append(('rule', prelude, body))
        i = j
    return nodes


def split_selectors(prelude):
    """Split a selector list on top-level commas."""
    out, start, i = [], 0, 0
    while i <= len(prelude):
        i = _scan_to(prelude, i, ',')
        out.append(prelude[start:i].strip())
        start = i = i + 1
    return [s for s in out if s]


_PSEUDO_ARGS = re.compile(r':{1,2}[a-zA-Z-]+\(')
_CLASS = re.compile(r'\.((?:[A-Za-z0-9_-]|\\.)+)')


def _strip_pseudo_args(selector):
    """Remove the argument of any functional pseudo-class.

    `.btn:not(.disabled)` must survive whether or not anything carries
    `.disabled` — a :not() argument makes the rule match MORE elements, so its
    classes are not a requirement. Same for :is()/:where()/:has()/:nth-child().
    """
    while True:
        m = _PSEUDO_ARGS.search(selector)
        if not m:
            return selector
        depth, j = 1, m.end()
        while j < len(selector) and depth:
            if selector[j] == '(':
                depth += 1
            elif selector[j] == ')':
                depth -= 1
            j += 1
        selector = selector[:m.start()] + selector[j:]


def required_classes(selector):
    return {m.replace('\\', '')
            for m in _CLASS.findall(_strip_pseudo_args(selector))}


def all_classes(css_nodes):
    """Every class name Bootstrap defines (for the --report and the tests)."""
    found = set()
    for node in css_nodes:
        if node[0] == 'rule':
            for sel in split_selectors(node[1]):
                found |= required_classes(sel)
        elif node[0] == 'nested':
            found |= all_classes(node[2])
    return found


def _is_kept(name, used):
    return (name in used
            or name in SAFELIST
            or name.startswith(SAFELIST_PREFIXES))


def purge(nodes, used, dropped):
    """Return a new node list with unreachable selectors removed."""
    out = []
    for node in nodes:
        kind = node[0]
        if kind in ('banner', 'statement', 'verbatim'):
            out.append(node)
        elif kind == 'nested':
            inner = purge(node[2], used, dropped)
            if inner:
                out.append(('nested', node[1], inner))
        else:
            keep = []
            for sel in split_selectors(node[1]):
                needed = required_classes(sel)
                if all(_is_kept(n, used) for n in needed):
                    keep.append(sel)
                else:
                    dropped.update(n for n in needed if not _is_kept(n, used))
            if keep:
                out.append(('rule', ','.join(keep), node[2]))
    return out


def render(nodes):
    parts = []
    for node in nodes:
        if node[0] in ('banner', 'statement', 'verbatim'):
            parts.append(node[1])
        elif node[0] == 'nested':
            parts.append(f'{node[1]}{{{render(node[2])}}}')
        else:
            parts.append(f'{node[1]}{{{node[2]}}}')
    return ''.join(parts)


def build():
    with open(SOURCE, encoding='utf-8') as fh:
        css = fh.read()
    nodes = parse(css)
    used = used_tokens()
    dropped = set()
    kept = purge(nodes, used, dropped)
    return css, nodes, render(kept), used, dropped


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--report', action='store_true',
                    help='list the class families that were dropped')
    ap.add_argument('--check', action='store_true',
                    help="don't write; exit 1 if the committed output is stale")
    args = ap.parse_args()

    original, nodes, output, used, dropped = build()
    defined = all_classes(nodes)

    if args.check:
        current = open(OUTPUT, encoding='utf-8').read() if os.path.exists(OUTPUT) else ''
        if current != output:
            print(f'{os.path.relpath(OUTPUT, ROOT)} is stale — re-run '
                  f'tools/purge_bootstrap_css.py')
            return 1
        print(f'{os.path.relpath(OUTPUT, ROOT)} is up to date.')
        return 0

    with open(OUTPUT, 'w', encoding='utf-8') as fh:
        fh.write(output)

    saved = len(original) - len(output)
    print(f'{os.path.relpath(SOURCE, ROOT)}  {len(original) / 1024:.1f} KB')
    print(f'{os.path.relpath(OUTPUT, ROOT)}  {len(output) / 1024:.1f} KB'
          f'   ({saved / 1024:.1f} KB, {saved * 100.0 / len(original):.0f}% smaller)')
    print(f'{len(defined)} Bootstrap classes defined, '
          f'{len(defined & used)} referenced in our content, '
          f'{len(SAFELIST)} safelisted by name, '
          f'{len(SAFELIST_PREFIXES)} safelisted families')

    if args.report:
        print('\nDropped classes by family:')
        families = {}
        for name in sorted(dropped):
            families.setdefault(name.split('-')[0], []).append(name)
        for head, names in sorted(families.items(), key=lambda kv: -len(kv[1])):
            print(f'  {head:<22} {len(names):>4}  {", ".join(names[:6])}'
                  f'{" …" if len(names) > 6 else ""}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
