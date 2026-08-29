"""Repeatable performance baseline for the NORTHWIND web app.

    python3 tools/perf/benchmark.py                 # full report
    python3 tools/perf/benchmark.py --json out.json # machine-readable, for diffing
    python3 tools/perf/benchmark.py --compare a.json b.json

Measures what actually reaches a person's browser, because that — not server
time — is where this app's cost sits:

  • HTML bytes per page, and what those bytes would be gzipped (authenticated
    pages are Cache-Control: no-store, so the FULL body is re-sent on every
    navigation — this number is paid per page view, not once)
  • DOM element / form / input counts (what the browser must build, style and
    lay out; the thing that makes a tablet lag)
  • SQL statements and connections opened per request (N+1 growth)
  • server render time
  • the static asset payload each page pulls in, raw vs gzipped

Runs against a COPY of deductions.db, so it never touches real data.
"""
import argparse
import collections
import gzip
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

# Pages worth watching: the heaviest of each shape (long list, wide sheet,
# per-row forms, dashboard aggregate).
PAGES = [
    ('/admin', 'admin dashboard'),
    ('/employees', 'employee list'),
    ('/undercharges', 'undercharges'),
    ('/laybys', 'laybys'),
    ('/uniforms', 'uniforms'),
    ('/stores', 'stores'),
    ('/cash', 'cash overview'),
    ('/cards', 'cc cards'),
    ('/cards/review', 'cc review'),
    ('/activity', 'activity log'),
    ('/hq/employees', 'hq employees'),
]

_SQL_COUNTS = collections.Counter()
_CONNS = [0]
_TRACKING = [False]


def _install_tracing():
    """Count every statement + REAL connection opened inside a traced request.

    Connections are counted here at `sqlite3.connect`, not at `database.get_db`.
    Those used to be the same number; since a request shares one connection,
    get_db() is called many times per page but opens a file once. Counting the
    calls reported 8 connections for /admin where the truth is 1 — a tool that
    overstates the thing it exists to measure is worse than no tool.
    """
    real_connect = sqlite3.connect

    class TracedConn(sqlite3.Connection):
        def execute(self, sql, *a, **kw):
            if _TRACKING[0]:
                _SQL_COUNTS[' '.join(sql.split())[:120]] += 1
            return super().execute(sql, *a, **kw)

        def executemany(self, sql, *a, **kw):
            if _TRACKING[0]:
                _SQL_COUNTS[' '.join(sql.split())[:120]] += 1
            return super().executemany(sql, *a, **kw)

    def connect(*a, **kw):
        if _TRACKING[0]:
            _CONNS[0] += 1
        kw['factory'] = TracedConn
        return real_connect(*a, **kw)

    sqlite3.connect = connect


def _boot():
    """A Flask test client authenticated as a super admin, on a throwaway DB copy."""
    from northwind.data import database as db
    fd, path = tempfile.mkstemp(suffix='.db', prefix='nw_bench_')
    os.close(fd)
    shutil.copy(os.path.join(ROOT, 'db', 'deductions.db'), path)
    db.DB_PATH = path
    db.invalidate_stores_cache()
    import migrations
    migrations.run_migrations()

    _install_tracing()

    import app as a
    a.app.config['TESTING'] = True
    a.app.config['WTF_CSRF_ENABLED'] = False

    from werkzeug.security import generate_password_hash
    ident = db.get_admin_user('perfbench')
    if ident is None:
        db.create_admin_user('perfbench', 'Perf Bench',
                             generate_password_hash('x', method='pbkdf2:sha256'),
                             role='super')
        ident = db.get_admin_user('perfbench')

    client = a.app.test_client()
    with client.session_transaction() as s:
        s.update(admin=True, admin_role='super', admin_username='perfbench',
                 admin_display_name='Perf Bench', uid=ident['id'],
                 auth_version=ident['auth_version'], admin_last_active=time.time())
    return client, path


def _measure(client, url):
    """One page: bytes, DOM shape, queries, time, and the assets it references."""
    client.get(url)                                    # warm caches/templates
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        client.get(url)
        times.append((time.perf_counter() - t0) * 1000)

    _SQL_COUNTS.clear()
    _CONNS[0] = 0
    _TRACKING[0] = True
    resp = client.get(url)
    _TRACKING[0] = False

    body = resp.get_data(as_text=True)
    raw = len(body.encode())
    gz = len(gzip.compress(body.encode(), 6))
    tags = re.findall(r'<([a-zA-Z][a-zA-Z0-9]*)', body)

    assets, asset_raw, asset_gz = [], 0, 0
    for ref in dict.fromkeys(re.findall(r'(?:href|src)="(/static/[^"?]+)', body)):
        p = os.path.join(ROOT, ref.lstrip('/'))
        if os.path.exists(p):
            data = open(p, 'rb').read()
            asset_raw += len(data)
            asset_gz += len(gzip.compress(data, 6))
            assets.append(ref)

    return {
        'url': url,
        'status': resp.status_code,
        'html_bytes': raw,
        'html_gzip_bytes': gz,
        'compressed': bool(resp.headers.get('Content-Encoding')),
        'cache_control': resp.headers.get('Cache-Control', ''),
        'elements': len(tags),
        'forms': body.count('<form'),
        'inputs': body.count('<input'),
        'rows': body.count('<tr'),
        'queries': sum(_SQL_COUNTS.values()),
        'connections': _CONNS[0],
        'ms_median': sorted(times)[len(times) // 2],
        'assets': len(assets),
        'asset_bytes': asset_raw,
        'asset_gzip_bytes': asset_gz,
        'top_queries': _SQL_COUNTS.most_common(5),
    }


def _static_headers(client):
    """Are static assets long-cacheable yet, and are they compressed?"""
    out = []
    for f in ('/static/style.css', '/static/vendor/bootstrap.min.css',
              '/static/vendor/fonts/inter-400.woff2'):
        r = client.get(f)
        out.append({'path': f, 'status': r.status_code,
                    'cache_control': r.headers.get('Cache-Control', '(none)'),
                    'encoding': r.headers.get('Content-Encoding', '(none)'),
                    'bytes': len(r.get_data())})
    return out


def run():
    client, path = _boot()
    try:
        pages = []
        for url, label in PAGES:
            m = _measure(client, url)
            m['label'] = label
            pages.append(m)
        return {'pages': pages, 'static': _static_headers(client)}
    finally:
        for p in (path, path + '-wal', path + '-shm'):
            if os.path.exists(p):
                os.remove(p)


def _kb(n):
    return n / 1024.0


def report(data):
    print('HTML per navigation (authed pages are no-store — this is paid every view)')
    print(f'{"page":<20}{"code":>5}{"KB":>8}{"gzip":>7}{"x":>5}'
          f'{"elems":>8}{"forms":>7}{"inputs":>8}{"SQL":>6}{"conn":>6}{"ms":>6}')
    print('-' * 92)
    for p in data['pages']:
        print(f'{p["label"]:<20}{p["status"]:>5}{_kb(p["html_bytes"]):>8.1f}'
              f'{_kb(p["html_gzip_bytes"]):>7.1f}'
              f'{p["html_bytes"] / max(p["html_gzip_bytes"], 1):>5.0f}'
              f'{p["elements"]:>8}{p["forms"]:>7}{p["inputs"]:>8}'
              f'{p["queries"]:>6}{p["connections"]:>6}{p["ms_median"]:>6.0f}')

    ok = [p for p in data['pages'] if p['status'] == 200]
    if ok:
        print(f'\nworst page: {max(ok, key=lambda p: p["html_bytes"])["label"]}  '
              f'({_kb(max(p["html_bytes"] for p in ok)):.0f} KB)')
        print(f'total HTML across {len(ok)} pages: {_kb(sum(p["html_bytes"] for p in ok)):.0f} KB'
              f'  →  {_kb(sum(p["html_gzip_bytes"] for p in ok)):.0f} KB gzipped')
        print(f'any response compressed on the wire: '
              f'{"YES" if any(p["compressed"] for p in ok) else "NO"}')

    print('\nStatic asset payload referenced per page')
    print(f'{"page":<20}{"assets":>8}{"raw KB":>10}{"gzip KB":>10}')
    print('-' * 48)
    for p in data['pages']:
        if p['status'] == 200:
            print(f'{p["label"]:<20}{p["assets"]:>8}{_kb(p["asset_bytes"]):>10.1f}'
                  f'{_kb(p["asset_gzip_bytes"]):>10.1f}')

    print('\nStatic caching / compression headers')
    for s in data['static']:
        print(f'  {s["path"]:<42} {s["cache_control"]:<28} enc={s["encoding"]}')

    print('\nN+1 suspects (most-repeated statement per page, >3x)')
    for p in data['pages']:
        hot = [(q, n) for q, n in p['top_queries'] if n > 3]
        if hot:
            print(f'\n  {p["label"]}  ({p["queries"]} queries / {p["connections"]} connections)')
            for q, n in hot:
                print(f'    {n:>5}x  {q[:100]}')


def compare(before, after):
    bi = {p['url']: p for p in before['pages']}
    print(f'{"page":<20}{"HTML KB":>18}{"elements":>18}{"SQL":>16}{"ms":>14}')
    print('-' * 86)

    def delta(b, a, unit=''):
        if b == 0:
            return f'{a}{unit}'
        pct = (a - b) * 100.0 / b
        return f'{b:.0f}→{a:.0f}{unit} ({pct:+.0f}%)'

    for p in after['pages']:
        b = bi.get(p['url'])
        if not b or p['status'] != 200:
            continue
        print(f'{p["label"]:<20}'
              f'{delta(_kb(b["html_bytes"]), _kb(p["html_bytes"])):>18}'
              f'{delta(b["elements"], p["elements"]):>18}'
              f'{delta(b["queries"], p["queries"]):>16}'
              f'{delta(b["ms_median"], p["ms_median"]):>14}')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--json', metavar='PATH', help='write results as JSON')
    ap.add_argument('--compare', nargs=2, metavar=('BEFORE', 'AFTER'))
    args = ap.parse_args()

    if args.compare:
        with open(args.compare[0]) as f1, open(args.compare[1]) as f2:
            compare(json.load(f1), json.load(f2))
        return

    data = run()
    report(data)
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(data, f, indent=2)
        print(f'\nwrote {args.json}')


if __name__ == '__main__':
    main()
