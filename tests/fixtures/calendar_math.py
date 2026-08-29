"""Month and day arithmetic for the seed, anchored once per process.

Everything the seed writes is positioned relative to ANCHOR — the first of the
current month — rather than to a hardcoded date, because several tests compare
the data against ``datetime.now()`` ("what comes off my next pay", "which
instalment is overdue"). A frozen calendar would make those tests pass today and
fail next month.

ANCHOR is read ONCE, at import, and every offset is a whole number of months or
days from it, so a run that straddles midnight or a month end still sees one
consistent calendar. The offsets used by the generators are also deliberately
coarse (a plan is 8 months into a 20-month term, not 1 month into a 2-month
term), so no assertion sits on a boundary that a single day could cross.
"""
import calendar
import datetime as dt

ANCHOR = dt.date.today().replace(day=1)


def shift(offset):
    """(year, month) `offset` whole months from the anchor month."""
    index = ANCHOR.year * 12 + (ANCHOR.month - 1) + int(offset)
    return index // 12, index % 12 + 1


def month_index(year, month):
    return int(year) * 12 + int(month) - 1


def offset_of(year, month):
    """Inverse of `shift`: how many months `year`/`month` is from the anchor."""
    return month_index(year, month) - month_index(ANCHOR.year, ANCHOR.month)


def iso_day(day_offset):
    """An ISO date `day_offset` days from the anchor (negative = past)."""
    return (ANCHOR + dt.timedelta(days=int(day_offset))).isoformat()


def iso_in_month(offset, day):
    """An ISO date inside the month `offset` months from the anchor.

    `day` is clamped to the length of that month, so callers can ask for the
    28th of February without knowing which February they will get.
    """
    year, month = shift(offset)
    day = min(int(day), calendar.monthrange(year, month)[1])
    return '%04d-%02d-%02d' % (year, month, day)


def month_end(offset):
    year, month = shift(offset)
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def month_start(offset):
    year, month = shift(offset)
    return dt.date(year, month, 1)
