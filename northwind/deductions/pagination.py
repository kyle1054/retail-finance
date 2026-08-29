"""Row windowing for the deduction list pages (employees, uniforms, lay-bys,
undercharges).

Those lists used to render EVERY matching row. That is fine on the dev database
and quietly fatal in production: the page is ``Cache-Control: no-store``, so a
store on a shop-floor tablet re-downloads and re-lays-out the whole table on
every navigation, and the table only ever grows. This caps what reaches the
browser while leaving an explicit "show all" escape hatch for the rare
whole-list scan.

The window is applied AFTER the route has finished its own aggregation, and the
routes pass their money totals in separately. Slicing the list the template sums
would have made every stat card and TOTAL row report the current page instead of
the filtered set — an understated payroll figure is a far worse bug than a heavy
page.
"""
import math

from flask import request, url_for

DEFAULT_PER_PAGE = 50
# A hand-typed ?per_page= is still bounded: the point of the window is that no
# single request can be asked to build an unbounded DOM.
MAX_PER_PAGE = 500
SHOW_ALL = 'all'


def _int_arg(args, name):
    try:
        return int(args.get(name, ''))
    except (TypeError, ValueError):
        return None


def _url(endpoint, args, **overrides):
    """Rebuild the current URL with only the paging keys changed.

    Every other query parameter (store/status/type/month/year/search) is carried
    through verbatim, so paging never silently widens the filter the user set.

    REPEATED parameters are preserved as a list, not flattened. args.to_dict()
    keeps only the FIRST value of a repeated key, so a page built from a
    multi-select filter — /cards/review carries one card_id per selected card —
    would have quietly dropped every card but the first the moment someone paged.
    The user would see a narrower list and no indication why. The four deduction
    lists have no repeated parameter today; this is here so that reusing the
    macro on a page that does cannot introduce that bug.
    """
    params = {key: values[0] if len(values) == 1 else list(values)
              for key, values in args.lists()}
    for key, value in overrides.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    return url_for(endpoint, **params)


def _page_links(page, pages, endpoint, args):
    """First / last / current±1, with gaps marked — not one link per page.

    A 40-page list would otherwise put 40 more anchors on the very page we are
    trying to make lighter.
    """
    wanted = {1, pages, page - 1, page, page + 1}
    numbers = sorted(n for n in wanted if 1 <= n <= pages)
    links = []
    previous = 0
    for number in numbers:
        if previous and number > previous + 1:
            links.append({'gap': True, 'number': None, 'url': None,
                          'current': False})
        links.append({
            'gap': False,
            'number': number,
            'url': _url(endpoint, args, page=number),
            'current': number == page,
        })
        previous = number
    return links


def paginate(rows, noun='rows', per_page=DEFAULT_PER_PAGE, endpoint=None,
             args=None):
    """Window ``rows`` for display and describe the window for the pager macro.

    Returns ``(page_rows, pager)``. ``pager`` is plain data so the Jinja macro
    stays logic-free.
    """
    args = request.args if args is None else args
    endpoint = endpoint or request.endpoint
    total = len(rows)

    requested = (args.get('per_page') or '').strip().lower()
    show_all = requested == SHOW_ALL
    if not show_all and requested:
        override = _int_arg(args, 'per_page')
        if override and override > 0:
            per_page = min(override, MAX_PER_PAGE)

    if show_all:
        pages, page, window = 1, 1, list(rows)
    else:
        pages = max(1, int(math.ceil(total / float(per_page))))
        page = _int_arg(args, 'page') or 1
        page = min(max(page, 1), pages)
        start = (page - 1) * per_page
        window = list(rows[start:start + per_page])

    first_index = 0 if not total else (1 if show_all else (page - 1) * per_page + 1)
    pager = {
        'noun': noun,
        'total': total,
        'per_page': per_page,
        'page': page,
        'pages': pages,
        'shown': len(window),
        'first_index': first_index,
        'last_index': first_index + len(window) - 1 if window else 0,
        'show_all': show_all,
        # Only worth any pixels once the list actually exceeds one window, or
        # once the reader has opted into the unbounded view and needs a way back.
        'visible': show_all or total > per_page,
        'prev_url': _url(endpoint, args, page=page - 1) if page > 1 else None,
        'next_url': _url(endpoint, args, page=page + 1) if page < pages else None,
        'all_url': _url(endpoint, args, page=None, per_page=SHOW_ALL),
        'paged_url': _url(endpoint, args, page=None, per_page=None),
        'links': [] if show_all else _page_links(page, pages, endpoint, args),
    }
    return window, pager
