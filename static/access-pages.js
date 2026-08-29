/* CSP-safe interactions for login and People & Access pages. */
(function () {
  'use strict';

  window.addEventListener('error', (event) => {
    const logo = event.target;
    if (!(logo instanceof HTMLImageElement) || !logo.matches('[data-logo-fallback]')) return;
    logo.hidden = true;
    const fallback = logo.nextElementSibling;
    if (fallback) fallback.classList.add('is-visible');
  }, true);

  function activateTab(tabButton) {
    const tab = tabButton.dataset.accessTab;
    document.querySelectorAll('.tab-panel').forEach((panel) => {
      panel.classList.remove('active');
      panel.hidden = true;
    });
    document.querySelectorAll('[role="tab"][data-access-tab]').forEach((button) => {
      button.classList.remove('active');
      button.setAttribute('aria-selected', 'false');
      button.tabIndex = -1;
    });
    const target = document.getElementById(`tab-${tab}`);
    if (target) {
      target.classList.add('active');
      target.hidden = false;
    }
    tabButton.classList.add('active');
    tabButton.setAttribute('aria-selected', 'true');
    tabButton.tabIndex = 0;
  }

  document.addEventListener('click', (event) => {
    const copyButton = event.target.closest('[data-copy-target], [data-copy-values]');
    if (copyButton) {
      const values = copyButton.dataset.copyValues
        ? copyButton.dataset.copyValues.split(',').map((id) => {
          const element = document.getElementById(id.trim());
          return element ? element.textContent.trim() : '';
        }).filter(Boolean)
        : [document.getElementById(copyButton.dataset.copyTarget)?.textContent.trim() || ''];
      window.copyToClipboard(values.join('  /  '), copyButton);
      return;
    }

    const tabButton = event.target.closest('[data-access-tab]');
    if (!tabButton) return;
    activateTab(tabButton);
  });

  document.addEventListener('keydown', (event) => {
    const current = event.target.closest('[role="tab"][data-access-tab]');
    if (!current) return;
    const tabs = Array.from(current.closest('[role="tablist"]').querySelectorAll('[role="tab"]'));
    const index = tabs.indexOf(current);
    let next = null;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = tabs[(index + 1) % tabs.length];
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = tabs[(index - 1 + tabs.length) % tabs.length];
    if (event.key === 'Home') next = tabs[0];
    if (event.key === 'End') next = tabs[tabs.length - 1];
    if (!next) return;
    event.preventDefault();
    activateTab(next);
    next.focus();
  });
})();
