/* Shared interaction layer for the NORTHWIND operations workspace.
 * Progressive enhancement only: every action still works without JavaScript. */
(function () {
  'use strict';

  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Give every wide data region a keyboard-accessible scroll surface and a
  // subtle edge cue once it has moved horizontally.
  document.querySelectorAll('.table-responsive').forEach((wrap) => {
    if (!wrap.hasAttribute('tabindex')) wrap.tabIndex = 0;
    if (!wrap.hasAttribute('role')) wrap.setAttribute('role', 'region');
    if (!wrap.hasAttribute('aria-label')) wrap.setAttribute('aria-label', 'Scrollable data table');
    const update = () => wrap.classList.toggle('is-scrolled', wrap.scrollLeft > 4);
    wrap.addEventListener('scroll', update, { passive: true });
    update();
  });

  // Existing sortable headings also work without a mouse.
  document.querySelectorAll('th.sortable, th[data-sort]').forEach((heading) => {
    heading.tabIndex = 0;
    heading.setAttribute('role', 'button');
    heading.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      heading.click();
    });
  });

  // Immediate pressed/loading feedback prevents accidental double submits on
  // uploads, exports and financial mutations. Forms intercepted by another
  // handler remain untouched because defaultPrevented is checked after dispatch.
  //
  // One delegated listener, not one per form: the Xero review queue had 114
  // forms, so this loop alone attached 114 listeners at load. Delegation also
  // covers forms that arrive later (the review drawer is fetched on demand),
  // which the querySelectorAll pass at load could never see.
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!form || form.tagName !== 'FORM') return;
    window.setTimeout(() => {
      if (event.defaultPrevented || form.dataset.noLoading === 'true') return;
      const submitter = event.submitter || form.querySelector('[type="submit"]');
      if (!submitter || submitter.disabled) return;
      submitter.dataset.originalHtml = submitter.innerHTML;
      submitter.classList.add('is-loading');
      submitter.setAttribute('aria-busy', 'true');
      submitter.disabled = true;
      submitter.innerHTML = '<span class="ui-spinner" aria-hidden="true"></span><span>Working…</span>';
    }, 0);
  });

  // Selected rows stay visually anchored while the user works through a bulk
  // payroll action. The existing page logic remains the source of truth.
  document.addEventListener('change', (event) => {
    if (event.target.matches('[data-submit-on-change]') && event.target.form) {
      event.target.form.submit();
      return;
    }
    if (!event.target.matches('.mtick, [data-row-select]')) return;
    const row = event.target.closest('tr');
    if (row) row.classList.toggle('row-selected', event.target.checked);
    const selectedForm = document.getElementById('tickSelectedForm');
    if (selectedForm) {
      const count = document.querySelectorAll('.mtick:checked').length;
      selectedForm.classList.toggle('has-selection', count > 0);
    }
  });

  document.addEventListener('click', (event) => {
    const copyButton = event.target.closest('[data-copy-text]');
    if (!copyButton) return;
    window.copyToClipboard(copyButton.dataset.copyText || '', copyButton);
  });

  document.addEventListener('submit', (event) => {
    const form = event.target.closest('form[data-confirm]');
    if (!form || window.confirm(form.dataset.confirm || 'Continue?')) return;
    event.preventDefault();
  });

  // Error pages retain a safe dashboard fallback when JavaScript is unavailable.
  document.addEventListener('click', (event) => {
    const backLink = event.target.closest('[data-action="history-back"]');
    if (!backLink) return;
    if (window.history.length <= 1) return;
    event.preventDefault();
    window.history.back();
  });

  // Keyboard navigation: a discoverable G chord complements the existing ⌘K
  // search and never fires while a person is typing in a field.
  let chord = '';
  let chordTimer = null;
  document.addEventListener('keydown', (event) => {
    const target = event.target;
    const typing = target && (target.matches('input, textarea, select') || target.isContentEditable);
    if (typing || event.metaKey || event.ctrlKey || event.altKey) return;

    const key = event.key.toLowerCase();
    if (key === 'g') {
      chord = 'g';
      window.clearTimeout(chordTimer);
      chordTimer = window.setTimeout(() => { chord = ''; }, 1200);
      return;
    }
    if (chord !== 'g') return;
    chord = '';
    window.clearTimeout(chordTimer);
    const urls = {
      d: document.body.dataset.homeUrl,
      e: document.body.dataset.employeesUrl,
      p: document.body.dataset.payrollUrl
    };
    if (urls[key]) {
      event.preventDefault();
      window.location.assign(urls[key]);
    }
  });

  // Add a short, meaningful page entrance only when motion is welcome.
  if (!reducedMotion) {
    const main = document.getElementById('mainContent');
    if (main) requestAnimationFrame(() => main.classList.add('is-ready'));
  }
})();
