(function () {
  'use strict';

  var bulkForm = document.getElementById('ccBulkForm');
  if (!bulkForm) return;

  var selectAll = document.querySelector('[data-cc-bulk-all]');
  var countLabel = document.querySelector('[data-cc-bulk-count]');
  var actions = Array.prototype.slice.call(
    bulkForm.querySelectorAll('[data-cc-bulk-action]')
  );

  function boxes() {
    return Array.prototype.slice.call(
      document.querySelectorAll('input[name="line_id"][form="ccBulkForm"]')
    ).filter(function (box) {
      var row = box.closest('tr');
      return !row || row.style.display !== 'none';
    });
  }

  function updateSelection() {
    var visible = boxes();
    var checked = visible.filter(function (box) { return box.checked; });
    if (countLabel) {
      countLabel.textContent = checked.length
        ? checked.length + ' selected'
        : 'Select transactions for batch actions';
    }
    bulkForm.classList.toggle('is-active', checked.length > 0);
    actions.forEach(function (button) {
      button.disabled = checked.length === 0;
    });
    visible.forEach(function (box) {
      var row = box.closest('.cc-txn-row');
      if (row) row.classList.toggle('is-selected', box.checked);
    });
    if (selectAll) {
      selectAll.checked = visible.length > 0 && checked.length === visible.length;
      selectAll.indeterminate = checked.length > 0 && checked.length < visible.length;
    }
  }

  // The whole widget lives here, including the check-all mutation. It used to
  // sit in an inline block in cc_card.html, which only worked because that
  // block was parsed before this deferred file registered its own listener —
  // reorder the two and the count silently lagged an interaction behind.
  document.addEventListener('change', function (event) {
    if (event.target.matches('[data-cc-bulk-all]')) {
      var on = event.target.checked;
      boxes().forEach(function (box) { box.checked = on; });
      updateSelection();
    } else if (event.target.matches('input[name="line_id"][form="ccBulkForm"]')) {
      updateSelection();
    }
  });
  document.addEventListener('cc:rows-changed', updateSelection);

  // ── "Mark done" (reconciled in Xero) ──────────────────────────────────────
  // Marking a transaction done also removes it from the cardholder's checklist,
  // so the confirm names how many of the selected rows still have gaps and only
  // then sends the override the server requires for them. Cancelling submits
  // nothing; with JS off the post stays strict and skips incomplete rows.
  var overrideFlag = bulkForm.querySelector('[data-cc-bulk-override]');

  bulkForm.addEventListener('submit', function (event) {
    var submitter = event.submitter ||
      document.activeElement;                      // Safari < 15 has no submitter
    if (!submitter || submitter.value !== 'reconcile') return;
    var checked = boxes().filter(function (box) { return box.checked; });
    var gaps = checked.filter(function (box) {
      var row = box.closest('.cc-txn-row');
      return row && !row.classList.contains('is-ready') &&
        !row.classList.contains('is-reconciled');
    });
    var question = gaps.length
      ? gaps.length + ' of the ' + checked.length + ' selected transaction' +
        (checked.length === 1 ? '' : 's') + ' still ' +
        (gaps.length === 1 ? 'has' : 'have') + ' a missing receipt, reason, ' +
        'location or submission.\n\nMark all ' + checked.length +
        ' done anyway? They disappear from your view and from the cardholder\'s.'
      : 'Mark the ' + checked.length + ' selected transaction' +
        (checked.length === 1 ? '' : 's') + ' done (reconciled in Xero)? ' +
        'They disappear from your view and from the cardholder\'s.';
    if (!confirm(question)) {
      event.preventDefault();
      return;
    }
    if (overrideFlag) overrideFlag.value = gaps.length ? '1' : '';
  });

  updateSelection();
})();

(function () {
  'use strict';

  var box = document.getElementById('ccPrev');
  if (!box) return;

  var panel = box.querySelector('.cc-prev-panel');
  var body = document.getElementById('ccPrevBody');
  var nameEl = document.getElementById('ccPrevName');
  var extEl = document.getElementById('ccPrevExt');
  var lastFocus = null;

  function closePreview() {
    box.hidden = true;
    body.innerHTML = '';
    document.body.classList.remove('cc-preview-open');
    if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
  }

  function openPreview(url, name, kind) {
    nameEl.textContent = name || 'Receipt';
    extEl.href = url;
    extEl.setAttribute(
      'aria-label',
      'Open ' + (name || 'receipt') + ' in a new tab'
    );
    body.innerHTML = '';

    var content = document.createElement(kind === 'image' ? 'img' : 'iframe');
    content.src = url;
    if (kind === 'image') content.alt = name || 'Receipt';
    else content.title = name || 'Receipt PDF';
    body.appendChild(content);

    lastFocus = document.activeElement;
    box.hidden = false;
    document.body.classList.add('cc-preview-open');
    panel.focus();
  }

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('.cc-rcpt-preview');
    if (trigger) {
      var kind = trigger.getAttribute('data-preview-kind');
      if (kind === 'image' || kind === 'pdf') {
        event.preventDefault();
        openPreview(
          trigger.getAttribute('href'),
          trigger.getAttribute('data-name'),
          kind
        );
      }
      return;
    }
    if (event.target.closest('[data-ccprev-close]')) closePreview();
  });

  document.addEventListener('keydown', function (event) {
    if (box.hidden) return;
    if (event.key === 'Escape') {
      closePreview();
      return;
    }
    if (event.key !== 'Tab') return;

    var focusable = panel.querySelectorAll(
      'a[href],button:not([disabled]),[tabindex]:not([tabindex="-1"])'
    );
    if (!focusable.length) {
      event.preventDefault();
      panel.focus();
      return;
    }
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    if (event.shiftKey &&
        (document.activeElement === first || document.activeElement === panel)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
})();
