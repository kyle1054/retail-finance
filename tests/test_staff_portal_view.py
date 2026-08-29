"""What a shop-floor staff member sees on their own deductions page.

The portal is the only part of the app an employee ever reads, so the figures on
it are the ones that get argued about at payroll. These cover the money-facing
logic: what is coming off next, when the plan clears, and instalments that fell
due and were never collected.
"""
import datetime as dt

from northwind.data import database as db
from northwind.deductions import routes_portal


def _summary(emp_id):
    return routes_portal._employee_summary(emp_id)


def _an_employee_with(predicate):
    """First active employee whose summary satisfies `predicate`, else None."""
    conn = db.get_db()
    try:
        ids = [r['id'] for r in conn.execute(
            "SELECT id FROM employees WHERE status='active' ORDER BY id")]
    finally:
        conn.close()
    for emp_id in ids:
        summary = _summary(emp_id)
        if summary and predicate(summary):
            return summary
    return None


def test_next_due_is_the_first_month_still_to_be_collected(db_copy):
    summary = _an_employee_with(lambda s: s['next_due'])
    assert summary, 'no employee has an upcoming deduction in the fixture DB'
    now = dt.datetime.now()

    schedule = db.get_employee_schedule(summary['emp']['id'])
    upcoming = [m for m in schedule
                if (m['total'] - m['total_paid']) > 0.005
                and (m['year'], m['month']) >= (now.year, now.month)]
    first = upcoming[0]

    assert summary['next_due']['due'] == round(first['total'] - first['total_paid'], 2)
    assert str(first['year']) in summary['next_due']['label']


def test_an_uncollected_instalment_is_flagged_not_shown_as_upcoming(db_copy):
    """Money the ledger still wants but payroll never took. Showing it as
    "coming up" would promise a smaller deduction than the balance implies."""
    summary = _an_employee_with(lambda s: s['overdue'])
    assert summary, 'no employee has an overdue instalment in the fixture DB'
    now = (dt.datetime.now().year, dt.datetime.now().month)

    for month in summary['overdue']:
        assert month['due'] > 0.005
    # Nothing can be both overdue and upcoming.
    labels = {m['label'] for m in summary['overdue']}
    assert summary['next_due'] is None or summary['next_due']['label'] not in labels


def test_clear_after_is_the_last_month_with_money_owing(db_copy):
    summary = _an_employee_with(lambda s: s['clear_after'])
    assert summary
    schedule = [m for m in db.get_employee_schedule(summary['emp']['id'])
                if (m['total'] - m['total_paid']) > 0.005]
    last = schedule[-1]
    assert str(last['year']) in summary['clear_after']['label']


def test_someone_who_owes_nothing_gets_no_next_payment_promised(db_copy):
    summary = _an_employee_with(
        lambda s: s['outstanding']['total'] <= 0.005 and not s['active'])
    if summary is None:
        return  # no such employee in this DB; nothing to assert
    assert summary['next_due'] is None
    assert summary['clear_after'] is None
    assert summary['overdue'] == []


def test_the_page_shows_the_headline_figures(staff_client):
    client, emp = staff_client
    page = client.get('/portal/store/%s' % emp['id']).get_data(as_text=True)
    assert 'Total still owing' in page
    summary = _summary(emp['id'])
    if summary['next_due']:
        assert 'Coming off your next pay' in page
    if summary['overdue']:
        assert 'has not come off your pay yet' in page
    # Zero categories are hidden so the one real number is easy to find.
    if summary['outstanding']['layby'] <= 0.005:
        assert '>Lay-by<' not in page


def test_thousands_are_spaced_so_four_figure_balances_read_cleanly(staff_client):
    """R1250.00 is a wall of digits on a till tablet; R1 250.00 is not."""
    client, _ = staff_client
    conn = db.get_db()
    try:
        big = conn.execute(
            "SELECT employee_id FROM layby_deductions "
            "WHERE status='active' AND balance_remaining >= 1000 LIMIT 1").fetchone()
    finally:
        conn.close()
    if not big:
        return
    page = client.get('/portal/store/%s' % big['employee_id']).get_data(as_text=True)
    # Whatever the exact figure, no four-digit amount may render unspaced.
    import re
    assert not re.search(r'R\d{4,}\.\d\d', page), 'a four-figure amount lost its space'
