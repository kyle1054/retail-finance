"""Static guarantees for employee-page emails copied into Outlook."""
from pathlib import Path


SOURCE = (Path(__file__).resolve().parents[1] / 'templates' / 'employee.html').read_text()


def test_email_shell_is_fluid_light_and_outlook_safe():
    assert '<html lang="en">' in SOURCE
    assert 'name="color-scheme" content="light only"' in SOURCE
    assert '<!--[if mso]>' in SOURCE
    assert 'max-width:${o.width || 600}px' in SOURCE
    assert 'bgcolor="${E_SURFACE}"' in SOURCE
    assert 'mso-line-height-rule:exactly' in SOURCE
    assert 'background:${E_INK};padding:20px 24px' not in SOURCE


def test_individual_plan_email_leads_with_balance_and_reduces_detail_rows():
    assert "let summaryLabel = 'Still owing'" in SOURCE
    assert "let summaryValue = R(remaining)" in SOURCE
    assert "summaryLabel = 'Plan complete'" in SOURCE
    assert "'Monthly Deduction', 'Amount Paid', 'Balance Remaining'" in SOURCE
    assert 'const detailRows = rows.filter' in SOURCE
    assert '>Plan details</h2>' in SOURCE


def test_layby_saving_is_a_positive_callout_not_two_accounting_rows():
    assert 'Your <strong style="color:${E_INK};">${p.disc || 40}% staff discount' in SOURCE
    assert 'The retail total was ${R(p.basket)}; your plan total is ${R(total)}.' in SOURCE


def test_dynamic_plan_values_remain_html_escaped():
    assert '${escHtml(summaryLabel)}' in SOURCE
    assert '${escHtml(summaryValue)}' in SOURCE
    assert '${escHtml(r.label)}' in SOURCE
    assert '${escHtml(r.val)}' in SOURCE
