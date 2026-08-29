"""Regression guardrails for the staged CSP ``'unsafe-inline'`` removal."""

from pathlib import Path

from tools.csp_inventory import (
    CATEGORIES,
    scan_file,
    scan_generated_markup,
    scan_templates,
    totals,
)


# This is the verified pre-migration ceiling. During each migration batch the
# corresponding number is reduced; the final step changes all four values to zero.
BASELINE_MAXIMUMS = {
    "event_handlers": 53,
    "executable_inline_scripts": 8,
    "javascript_urls": 0,
    "style_blocks": 5,
    "style_attributes": 1888,
}

# Per-template ceilings prevent inline debt from moving between legacy files while
# the aggregate total stays flat. Tuple order follows tools.csp_inventory.CATEGORIES.
BASELINE_BY_TEMPLATE = {
    "activity.html": (3, 1, 0, 0, 27),
    "admin_change_password.html": (0, 0, 0, 0, 0),
    "admin_login.html": (0, 0, 0, 0, 0),
    "admin_manage_admins.html": (0, 0, 0, 0, 3),
    "admin_regional_managers.html": (0, 0, 0, 0, 10),
    "admin_staff_logins.html": (0, 0, 0, 0, 20),
    "allowances.html": (0, 0, 0, 0, 20),
    "base.html": (0, 0, 0, 0, 9),
    "cash_base.html": (0, 0, 0, 0, 2),
    "cash_day.html": (0, 0, 0, 0, 37),
    "cash_ledger.html": (0, 0, 0, 0, 70),
    "cash_mj.html": (0, 0, 0, 0, 42),
    "cash_overview.html": (0, 0, 0, 0, 65),
    "cash_sales.html": (0, 0, 0, 0, 41),
    "cash_store_days.html": (0, 0, 0, 0, 42),
    "cash_store_picker.html": (0, 0, 0, 0, 7),
    "cash_summary.html": (0, 0, 0, 0, 52),
    "cash_xero_setup.html": (0, 0, 0, 0, 41),
    "cc_card.html": (11, 1, 0, 1, 12),
    "cc_cards.html": (0, 0, 0, 1, 2),
    "cc_upload.html": (0, 1, 0, 1, 0),
    "employee.html": (26, 1, 0, 0, 504),
    "employees.html": (0, 0, 0, 0, 34),
    "error.html": (0, 0, 0, 0, 3),
    "import_center.html": (0, 1, 0, 0, 59),
    "index.html": (4, 1, 0, 0, 173),
    "invoice_search.html": (0, 0, 0, 0, 8),
    "landing.html": (0, 0, 0, 0, 0),
    "laybys.html": (0, 0, 0, 0, 77),
    "monthly.html": (0, 0, 0, 0, 92),
    "payroll_period.html": (0, 0, 0, 0, 10),
    "payroll_reconcile.html": (0, 0, 0, 0, 10),
    "payroll_sheet.html": (0, 0, 0, 0, 20),
    "payroll_sync.html": (0, 0, 0, 0, 109),
    "portal_base.html": (0, 0, 0, 0, 1),
    "portal_cc_card.html": (4, 0, 0, 0, 3),
    "portal_cc_pick.html": (0, 0, 0, 0, 2),
    "portal_employee.html": (2, 0, 0, 0, 8),
    "portal_hub.html": (0, 0, 0, 1, 15),
    "portal_store.html": (2, 0, 0, 0, 9),
    "rm_dashboard.html": (0, 1, 0, 1, 65),
    "rm_store_days.html": (0, 1, 0, 0, 0),
    "staff_login.html": (0, 0, 0, 0, 0),
    "stores.html": (1, 1, 0, 0, 35),
    "undercharges.html": (0, 0, 0, 0, 101),
    "uniforms.html": (0, 0, 0, 0, 54),
}


def test_inline_csp_inventory_does_not_increase():
    current = totals(scan_templates())
    over = {
        category: (current[category], maximum)
        for category, maximum in BASELINE_MAXIMUMS.items()
        if current[category] > maximum
    }
    assert not over, f"inline CSP debt increased: {over}"


def test_new_templates_start_csp_clean():
    """A new template may not add debt while legacy pages are being migrated."""
    unexpected = sorted(set(scan_templates()) - set(BASELINE_BY_TEMPLATE))
    assert not unexpected, f"new templates contain inline CSP debt: {unexpected}"


def test_email_bodies_are_excluded_but_pages_are_not():
    """templates/email/ holds mail bodies, never pages: no CSP applies to them
    and inline styles are mandatory (Gmail and Outlook strip <style> blocks).
    The exclusion must stay narrow — a real page must never slip through it."""
    from tools.csp_inventory import EXCLUDED_DIRS, _template_paths

    assert EXCLUDED_DIRS == ("email",), 'widening this hides real pages from the gate'
    scanned = {path.name for path in _template_paths()}
    assert 'cc_receipt_reminder.html' not in scanned
    assert {'employee.html', 'cc_card.html', 'base.html'} <= scanned


def test_inline_debt_cannot_move_between_legacy_templates():
    over = {}
    for template, counts in scan_templates().items():
        maximums = BASELINE_BY_TEMPLATE.get(template)
        if maximums is None:
            continue
        for index, category in enumerate(CATEGORIES):
            if counts[category] > maximums[index]:
                over[f"{template}:{category}"] = (counts[category], maximums[index])
    assert not over, f"per-template inline CSP debt increased: {over}"


def test_scanner_recognises_executable_inline_constructs(tmp_path):
    template = tmp_path / "fixture.html"
    template.write_text(
        """
        <button ONPOINTERDOWN = "save()" style=color:red>Save</button>
        <form onreset=resetForm()></form>
        <script>run()</script>
        <style>.example { color: red; }</style>
        <a href = " JavaScript:alert(1)">unsafe</a>
        """,
        encoding="utf-8",
    )
    assert scan_file(template) == {
        "event_handlers": 2,
        "executable_inline_scripts": 1,
        "javascript_urls": 1,
        "style_blocks": 1,
        "style_attributes": 1,
    }


def test_scanner_ignores_safe_external_inert_and_commented_constructs(tmp_path):
    template = tmp_path / "fixture.html"
    template.write_text(
        """
        <script src=/static/app.js></script>
        <script TYPE=application/json>{"safe": true}</script>
        {# <button onclick="jinjaIgnored()"></button>
           <script>jinjaIgnored()</script> #}
        <!-- <button onclick="ignored()"></button>
             <script>ignored()</script>
             <style>.ignored { color: red; }</style> -->
        """,
        encoding="utf-8",
    )
    assert scan_file(template) == {
        "event_handlers": 0,
        "executable_inline_scripts": 0,
        "javascript_urls": 0,
        "style_blocks": 0,
        "style_attributes": 0,
    }


def test_error_history_action_keeps_safe_no_history_fallback():
    source = (Path(__file__).resolve().parents[1] / "static" / "app-ui.js").read_text(
        encoding="utf-8"
    )
    history_guard = source.index("if (window.history.length <= 1) return;")
    prevent_default = source.index("event.preventDefault();", history_guard)
    history_back = source.index("window.history.back();", prevent_default)
    assert history_guard < prevent_default < history_back


def test_shared_shells_have_no_executable_inline_javascript():
    inventory = scan_templates()
    for template in ("base.html", "portal_base.html", "cash_base.html"):
        counts = inventory.get(template, {})
        assert counts.get("event_handlers", 0) == 0
        assert counts.get("executable_inline_scripts", 0) == 0
        assert counts.get("javascript_urls", 0) == 0
        assert counts.get("style_blocks", 0) == 0


def test_shared_shell_accessibility_and_clipboard_fallback_contracts():
    root = Path(__file__).resolve().parents[1]
    toast_source = (root / "static" / "shell-common.js").read_text(encoding="utf-8")
    clipboard_source = (root / "static" / "base-shell.js").read_text(encoding="utf-8")
    assert "close.setAttribute('aria-label', 'Close notification')" in toast_source
    assert "close.addEventListener('click'" in toast_source
    assert "if (document.execCommand('copy'))" in clipboard_source


def test_access_pages_have_no_inline_code_or_style_blocks():
    inventory = scan_templates()
    pages = (
        "admin_change_password.html",
        "admin_login.html",
        "admin_manage_admins.html",
        "admin_regional_managers.html",
        "admin_staff_logins.html",
        "landing.html",
        "staff_login.html",
    )
    for template in pages:
        counts = inventory.get(template, {})
        assert counts.get("event_handlers", 0) == 0
        assert counts.get("executable_inline_scripts", 0) == 0
        assert counts.get("javascript_urls", 0) == 0
        assert counts.get("style_blocks", 0) == 0


def test_staff_access_tabs_expose_accessible_state_contract():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "admin_staff_logins.html").read_text(encoding="utf-8")
    script = (root / "static" / "access-pages.js").read_text(encoding="utf-8")
    assert 'role="tablist"' in template
    assert template.count('role="tab"') == 2
    assert template.count('role="tabpanel"') == 2
    assert "tabButton.setAttribute('aria-selected', 'true')" in script
    assert "event.key === 'ArrowRight'" in script


def test_deductions_list_pages_have_no_inline_executable_code():
    inventory = scan_templates()
    for template in (
        "allowances.html",
        "employees.html",
        "laybys.html",
        "undercharges.html",
        "uniforms.html",
    ):
        counts = inventory.get(template, {})
        assert counts.get("event_handlers", 0) == 0
        assert counts.get("executable_inline_scripts", 0) == 0
        assert counts.get("javascript_urls", 0) == 0


def test_monthly_and_payroll_pages_have_no_inline_executable_code():
    inventory = scan_templates()
    for template in (
        "monthly.html",
        "payroll_reconcile.html",
        "payroll_sheet.html",
        "payroll_sync.html",
    ):
        counts = inventory.get(template, {})
        assert counts.get("event_handlers", 0) == 0
        assert counts.get("executable_inline_scripts", 0) == 0
        assert counts.get("javascript_urls", 0) == 0


def test_cash_pages_have_no_inline_executable_code():
    inventory = scan_templates()
    for template in (
        "cash_ledger.html",
        "cash_mj.html",
        "cash_overview.html",
        "cash_sales.html",
        "cash_store_days.html",
        "cash_summary.html",
    ):
        counts = inventory.get(template, {})
        assert counts.get("event_handlers", 0) == 0
        assert counts.get("executable_inline_scripts", 0) == 0
        assert counts.get("javascript_urls", 0) == 0


def test_cash_extracted_interaction_contracts():
    root = Path(__file__).resolve().parents[1]
    overview = (root / "templates" / "cash_overview.html").read_text(encoding="utf-8")
    days = (root / "templates" / "cash_store_days.html").read_text(encoding="utf-8")
    sales = (root / "templates" / "cash_sales.html").read_text(encoding="utf-8")
    ledger = (root / "templates" / "cash_ledger.html").read_text(encoding="utf-8")
    overview_js = (root / "static" / "cash-overview.js").read_text(encoding="utf-8")
    ledger_js = (root / "static" / "cash-ledger.js").read_text(encoding="utf-8")
    mj_js = (root / "static" / "cash-mj.js").read_text(encoding="utf-8")

    for source in (overview, days, sales):
        assert 'aria-expanded="false"' in source
        assert "aria-controls=" in source
    assert 'row.dataset.loaded = "0"' in overview_js
    assert 'storeButton = row.querySelector("[data-cash-store-toggle]")' in overview_js
    assert '[data-edit-entry]' in ledger_js
    assert 'data-confirm="Remove this entry?"' in ledger
    assert 'Math.round((parseFloat(row.querySelector(".mj-gross").value) || 0) * 100)' in mj_js
    assert 'roundingCell.classList.add("rounding-debit")' in mj_js


def test_monthly_disclosure_and_confirmation_interaction_contracts():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "monthly.html").read_text(encoding="utf-8")
    script = (root / "static" / "monthly.js").read_text(encoding="utf-8")
    assert 'data-invoice-toggle aria-expanded="false"' in template
    assert 'aria-controls="detail-{{ loop.index }}"' in template
    assert 'button.setAttribute("aria-expanded", String(!isOpen))' in script
    assert 'document.querySelectorAll("form")' not in script


def test_external_javascript_does_not_generate_inline_csp_markup():
    assert scan_generated_markup() == {}


def test_generated_markup_scanner_recognises_inline_constructs(tmp_path):
    (tmp_path / "unsafe.js").write_text(
        """const html = '<button onclick="go()" style="color:red">x</button>';"""
        """const url = 'javascript:alert(1)';""",
        encoding="utf-8",
    )
    assert scan_generated_markup(tmp_path) == {
        "unsafe.js": {
            "event_handlers": 1,
            "style_attributes": 1,
            "javascript_urls": 1,
        }
    }
