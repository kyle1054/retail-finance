(function () {
 document.querySelectorAll('[data-submit-on-change]').forEach(function (control) {
   control.addEventListener('change', function () {
     if (control.form) control.form.submit();
   });
 });
})();

(function () {
 try {
  var todo = document.getElementById('ccTodo');
  if (!todo) return;
  var done = document.getElementById('ccDone'),
      doneHead = document.getElementById('ccDoneHead'),
      doneCount = document.getElementById('ccDoneCount'),
      todoCount = document.getElementById('ccTodoCount'),
      todoEmpty = document.getElementById('ccTodoEmpty'),
      from = document.getElementById('fFrom'), to = document.getElementById('fTo'),
      search = document.getElementById('fSearch'), currentFilter = 'all',
      focusMode = todo.getAttribute('data-focus-mode') === 'y';

  function resolved(card) {
    return card.getAttribute('data-receipt') === 'y'
      && card.getAttribute('data-reason') === 'y'
      && card.getAttribute('data-location') === 'y'
      && card.getAttribute('data-ai') !== 'pending'
      && card.getAttribute('data-vat') !== 'requested';
  }
  // Mirrors the server-rendered chip in portal_cc_card.html exactly. A collapsed
  // row shows nothing else, so this line has to name every outstanding item
  // rather than only the first one.
  function chipParts(card) {
    var personal = card.getAttribute('data-personal') === 'y',
        rc = card.getAttribute('data-receipt') === 'y',
        rs = card.getAttribute('data-reason') === 'y',
        loc = card.getAttribute('data-location') === 'y',
        ai = card.getAttribute('data-ai') !== 'pending',
        vatClear = card.getAttribute('data-vat') !== 'requested';
    if (!vatClear) return ['is-vat', 'bi-file-earmark-text', 'VAT tax invoice requested'];
    var missing = [];
    if (!personal && !rc) missing.push('receipt');
    if (!rs) missing.push('reason');
    if (!loc) missing.push('location');
    if (!ai) missing.push('AI match');
    if (!missing.length) {
      return personal ? ['is-personal', 'bi-person-check', 'Personal · ready']
                      : ['is-done', 'bi-check-lg', 'Complete'];
    }
    return ['is-todo', personal ? 'bi-list-check' : 'bi-exclamation-circle',
            'Needs ' + missing.join(' · ')];
  }
  function paintChip(card) {
    var chip = card.querySelector('.cc-status-chip');
    if (!chip) return;
    var p = chipParts(card);
    chip.className = 'cc-status-chip ' + p[0];
    chip.innerHTML = '<i class="bi ' + p[1] + '"></i> ' + p[2];
  }
  function syncActions(card, keepPosition) {
    var ready = resolved(card);
    // During an autosave, keep data-ready unchanged so the CSS cannot hide the
    // row from #ccTodo before the user chooses to reload or navigate. The chip
    // and actions may update, but the row stays physically in place.
    if (!keepPosition) card.setAttribute('data-ready', ready ? 'y' : 'n');
    var wrap = card.querySelector('.cc-sel-wrap'), box = card.querySelector('.cc-sel');
    if (wrap) wrap.hidden = !ready;
    if (box) { box.disabled = !ready; if (!ready) box.checked = false; }
    var submit = card.querySelector('.cc-ready-submit');
    if (submit) submit.hidden = !ready;
  }
  // Single owner of the collapsed state so the chevron's aria-expanded can never
  // drift from what is actually on screen.
  function setCollapsed(card, collapsed) {
    card.classList.toggle('is-collapsed', collapsed);
    var toggle = card.querySelector('.cc-txn-toggle');
    if (toggle) toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  }
  function place(card) {
    if (focusMode) {
      card.classList.remove('cc-collapsible');
      setCollapsed(card, false);
      paintChip(card);
      syncActions(card);
      return;
    }
    // Keep the card open (and restore focus) if the user is mid-edit inside it —
    // otherwise a live-complete would hide the field they're still using.
    var focused = card.contains(document.activeElement) ? document.activeElement : null;
    card.classList.add('cc-collapsible');
    setCollapsed(card, !focused);
    (resolved(card) ? done : todo).appendChild(card);
    if (focused) focused.focus();
    paintChip(card);
    syncActions(card);
  }
  function refresh() {
    var t = todo.querySelectorAll('.cc-txn').length,
        d = done.querySelectorAll('.cc-txn').length;
    if (todoCount) todoCount.textContent = t === 1 ? '1 item' : t + ' items';
    if (todoEmpty) todoEmpty.hidden = t !== 0;
    if (doneHead) doneHead.hidden = d === 0;
    if (doneCount) doneCount.textContent = d + ' done';
    if (d === 0) {
      done.hidden = true;
      if (doneHead) { doneHead.classList.remove('open'); doneHead.setAttribute('aria-expanded', 'false'); }
    }
  }

  // Initial distribution: resolved cards drop into the Completed section.
  Array.prototype.slice.call(todo.querySelectorAll('.cc-txn')).forEach(function (card) {
    if (resolved(card)) place(card); else { paintChip(card); syncActions(card); }
  });
  refresh();

  function toggleDone() {
    if (!done.querySelectorAll('.cc-txn').length) return;
    done.hidden = !done.hidden;
    doneHead.classList.toggle('open', !done.hidden);
    doneHead.setAttribute('aria-expanded', done.hidden ? 'false' : 'true');
    syncExpandAll();   // opening Completed changes what "all" covers
  }
  if (doneHead) {
    doneHead.addEventListener('click', toggleDone);
    doneHead.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleDone(); }
    });
  }

  // Expand/collapse every row the filters are currently showing. Acts on what
  // is visible, not the whole month, so "expand all" after filtering to "needs
  // receipt" opens exactly those rows.
  var expandAll = document.getElementById('ccExpandAll');
  function visibleRows() {
    var out = [];
    [todo, done].forEach(function (c) {
      // The Completed section is a collapsed container most of the time. Its
      // rows carry no inline display style, so without this check they counted
      // as "visible": the button label could read "Expand all" because of rows
      // nobody can see, and clicking it opened them behind a closed section.
      if (c.hidden) return;
      Array.prototype.forEach.call(c.querySelectorAll('.cc-txn'), function (r) {
        if (r.style.display !== 'none' && r.classList.contains('cc-collapsible')) out.push(r);
      });
    });
    return out;
  }
  function syncExpandAll() {
    if (!expandAll) return;
    var rows = visibleRows();
    expandAll.hidden = rows.length < 2;
    if (expandAll.hidden) return;
    var anyCollapsed = rows.some(function (r) { return r.classList.contains('is-collapsed'); });
    expandAll.setAttribute('aria-expanded', anyCollapsed ? 'false' : 'true');
    expandAll.querySelector('span').textContent = anyCollapsed ? 'Expand all' : 'Collapse all';
    expandAll.querySelector('i').className =
      'bi ' + (anyCollapsed ? 'bi-arrows-expand' : 'bi-arrows-collapse');
  }
  if (expandAll) expandAll.addEventListener('click', function () {
    var rows = visibleRows();
    // Anything still shut means the button reads "Expand all" — so open them.
    var collapse = !rows.some(function (r) { return r.classList.contains('is-collapsed'); });
    rows.forEach(function (r) { setCollapsed(r, collapse); });
    syncExpandAll();
  });

  function onClick(e) {
    // The chevron is a real button, so it has to be handled before the generic
    // "ignore clicks on controls" guard further down.
    var toggle = e.target.closest('.cc-txn-toggle');
    if (toggle) {
      var toggleCard = toggle.closest('.cc-txn');
      if (toggleCard) setCollapsed(toggleCard, !toggleCard.classList.contains('is-collapsed'));
      syncExpandAll();
      return;
    }
    // "Link" reveals the existing-file picker. "Add file" only falls back to
    // this panel when the one-tap picker below is not driving it (no dialog
    // support — see the cc-attach-js flag).
    var panelBtn = e.target.closest('.cc-rcpt-link, .cc-rcpt-add');
    if (panelBtn) {
      if (panelBtn.classList.contains('cc-rcpt-add')
          && document.documentElement.classList.contains('cc-attach-js')) return;
      var panel = document.getElementById(panelBtn.getAttribute('data-target'));
      if (panel) panel.hidden = !panel.hidden;
      if (panel && !panel.hidden) {
        var focusable = panel.querySelector('.cc-filepick-trigger')
          || panel.querySelector('input[type="file"]');
        if (focusable) focusable.focus();
      }
      return;
    }
    var top = e.target.closest('.cc-txn-top');
    if (top && !e.target.closest('a, button, input, label')) {
      var card = top.closest('.cc-txn');
      if (card && card.classList.contains('cc-collapsible')) {
        setCollapsed(card, !card.classList.contains('is-collapsed'));
        syncExpandAll();
      }
    }
  }
  todo.addEventListener('click', onClick);
  done.addEventListener('click', onClick);

  function apply() {
    var f = from ? from.value : '', t = to ? to.value : '', q = search ? search.value.trim().toLowerCase() : '';
    [todo, done].forEach(function (c) {
      c.querySelectorAll('.cc-txn').forEach(function (row) {
        var d = row.getAttribute('data-date') || '', m = row.getAttribute('data-merchant') || '';
        var receipt = row.getAttribute('data-receipt') === 'y',
            reason = row.getAttribute('data-reason') === 'y',
            location = row.getAttribute('data-location') === 'y',
            aiClear = row.getAttribute('data-ai') !== 'pending',
            vatRequested = row.getAttribute('data-vat') === 'requested',
            personal = row.getAttribute('data-personal') === 'y',
            submitted = row.getAttribute('data-submitted') === 'y',
            ready = row.getAttribute('data-ready') === 'y';
        var statusOk = currentFilter === 'all'
          || (currentFilter === 'receipt' && !personal && !receipt)
          || (currentFilter === 'reason' && !reason)
          || (currentFilter === 'location' && !location)
          || (currentFilter === 'vat' && vatRequested)
          || (currentFilter === 'ai' && !aiClear)
          || (currentFilter === 'ready' && ready && !submitted)
          || (currentFilter === 'personal' && personal)
          || (currentFilter === 'submitted' && submitted);
        var ok = statusOk && (!f || d >= f) && (!t || d <= t) && (!q || m.indexOf(q) !== -1);
        row.style.display = ok ? '' : 'none';
      });
    });
    if (currentFilter !== 'all' && done.querySelector('.cc-txn:not([style*="display: none"])')) {
      done.hidden = false;
      if (doneHead) { doneHead.classList.add('open'); doneHead.setAttribute('aria-expanded', 'true'); }
    }
    // The filter controls live behind a button now, so the button itself has to
    // say when something is filtering the list — otherwise a stray chip looks
    // like missing transactions.
    var badge = document.getElementById('ccFilterActive');
    if (badge) {
      var active = (currentFilter !== 'all' ? 1 : 0) + (q ? 1 : 0) + ((f || t) ? 1 : 0);
      badge.hidden = !active;
      badge.textContent = active;
    }
    // Filtering changes which rows "all" refers to.
    syncExpandAll();
  }
  [from, to, search].forEach(function (el) {
    if (!el) return;
    el.addEventListener('input', apply); el.addEventListener('change', apply);
  });
  document.querySelectorAll('.cc-filter-chip').forEach(function (chip) {
    chip.addEventListener('click', function () {
      currentFilter = chip.getAttribute('data-filter') || 'all';
      document.querySelectorAll('.cc-filter-chip').forEach(function (c) {
        c.classList.toggle('active', c === chip);
      });
      apply();
    });
  });
  var more = document.getElementById('ccMoreFilters'),
      panel = document.getElementById('ccFilterPanel');
  if (more && panel) more.addEventListener('click', function () {
    panel.hidden = !panel.hidden;
    more.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true');
    more.classList.toggle('active', !panel.hidden);
  });
  var clearDates = document.getElementById('ccClearDates');
  if (clearDates) clearDates.addEventListener('click', function () {
    if (from) from.value = ''; if (to) to.value = ''; apply();
  });
  apply();

  // Autosaves update the current card in place. Never re-file it into the
  // Completed section, re-run filters, or alter section counts mid-edit: each
  // of those can remove content above the viewport and jump the user down the
  // page. Normal navigation/reload performs the full sectioning from the saved
  // server state.
  window.__ccReplace = function (card) {
    paintChip(card);
    syncActions(card, true);
  };
 } catch (e) {
  // Fail safe: if init throws, reveal every card so nothing is ever hidden.
  document.documentElement.classList.add('cc-jsfail');
  if (window.console && console.error) console.error('cc portal init failed', e);
 }
})();

// Drag-and-drop dropzone: click/tap to browse, drag to highlight, show count.
(function () {
  var form = document.getElementById('ccDrop');
  if (!form) return;
  var input = document.getElementById('ccFile'),
      inner = document.getElementById('ccDzInner'),
      toggle = document.getElementById('ccUploadToggle'),
      panel = document.getElementById('ccUploadPanel'),
      tray = document.getElementById('ccDzTray'),
      count = document.getElementById('ccDzCount'),
      clear = document.getElementById('ccDzClear');
  function setOpen(open) {
    if (!panel || !toggle) return;
    panel.hidden = !open;
    toggle.classList.toggle('open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  function refresh() {
    var n = input.files ? input.files.length : 0;
    if (n) {
      count.textContent = n + ' file' + (n === 1 ? '' : 's') + ' ready';
      tray.hidden = false; setOpen(true);
    } else { tray.hidden = true; }
  }
  if (toggle) toggle.addEventListener('click', function () {
    setOpen(panel ? panel.hidden : true);
    if (panel && !panel.hidden) inner.focus();
  });
  inner.addEventListener('click', function () { input.click(); });
  inner.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
  });
  input.addEventListener('change', refresh);
  clear.addEventListener('click', function () { input.value = ''; refresh(); });
  ['dragenter', 'dragover'].forEach(function (ev) {
    form.addEventListener(ev, function (e) { e.preventDefault(); setOpen(true); form.classList.add('is-drag'); });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    form.addEventListener(ev, function (e) {
      e.preventDefault();
      if (ev === 'dragleave' && form.contains(e.relatedTarget)) return;
      form.classList.remove('is-drag');
    });
  });
  form.addEventListener('drop', function (e) {
    if (e.dataTransfer && e.dataTransfer.files.length) { input.files = e.dataTransfer.files; refresh(); }
  });

  // XHR upload with a real transfer-progress bar (fetch can't report upload
  // progress). On success we reload so the new receipts appear and the server's
  // flashes render as toasts; on failure we toast and let the user retry.
  var bar = document.getElementById('ccUploadBar'),
      fill = document.getElementById('ccUploadFill'),
      pct = document.getElementById('ccUploadPct'),
      token = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

  function setBusy(on) {
    bar.hidden = !on;
    if (tray) tray.style.display = on ? 'none' : '';
    form.querySelectorAll('button, .cc-file-input').forEach(function (el) { el.disabled = on; });
    form.classList.toggle('is-uploading', on);
  }

  function upload() {
    var fd = new FormData(form);  // statement_id + whichever file input has files
    var xhr = new XMLHttpRequest();
    xhr.open('POST', form.action);
    if (token) xhr.setRequestHeader('X-CSRFToken', token);
    xhr.setRequestHeader('X-Requested-With', 'fetch');
    xhr.upload.addEventListener('progress', function (e) {
      if (!e.lengthComputable) return;
      var p = Math.round(e.loaded / e.total * 100);
      fill.style.width = p + '%';
      pct.textContent = p < 100 ? 'Uploading… ' + p + '%' : 'Processing…';
    });
    xhr.addEventListener('load', function () {
      if (xhr.status >= 200 && xhr.status < 300) {
        pct.textContent = 'Done';
        window.location.reload();
      } else {
        var msg = 'Upload failed — please try again.';
        try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
        showToast(msg, 'danger');
        setBusy(false);
      }
    });
    xhr.addEventListener('error', function () {
      showToast('Upload failed — check your connection and try again.', 'danger');
      setBusy(false);
    });
    // Backstop so a silently-stalled connection can't leave the UI disabled
    // forever. Generous (10 min) so a legit big upload on slow mobile isn't cut off.
    xhr.timeout = 600000;
    xhr.addEventListener('timeout', function () {
      showToast('Upload timed out — please try again.', 'danger');
      setBusy(false);
    });
    xhr.addEventListener('abort', function () { setBusy(false); });
    setBusy(true);
    fill.style.width = '0%';
    pct.textContent = 'Uploading… 0%';
    xhr.send(fd);
  }

  form.addEventListener('submit', function (e) { e.preventDefault(); upload(); });
  window.__ccUpload = upload;  // the camera path (snap-and-upload) reuses this
})();

// Phone-only "Take photo": show the camera buttons only on touch devices (a
// desktop has no camera to capture to), then snap-and-upload in one tap.
(function () {
  var coarse = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
  var touch = 'ontouchstart' in window || (navigator.maxTouchPoints || 0) > 0;
  if (coarse || touch) document.documentElement.classList.add('cc-touch');

  // Anything carrying data-cam opens its paired camera input. Keyed on the
  // attribute rather than a class so a control can be styled however it needs
  // to be (the receipt gallery's camera tile looks nothing like a button).
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-cam]');
    if (!btn) return;
    e.preventDefault();
    var input = document.getElementById(btn.getAttribute('data-cam'));
    if (input) input.click();
  });
  // Same contract for the gallery's "Add file" tile: one tap opens the OS file
  // picker. Only bound once the per-transaction uploader has confirmed it can
  // run (cc-attach-js), so a browser without <dialog> keeps the visible input
  // and Upload button instead of a picker whose submit path is unavailable.
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-pick]');
    if (!btn || !document.documentElement.classList.contains('cc-attach-js')) return;
    e.preventDefault();
    var input = document.getElementById(btn.getAttribute('data-pick'));
    if (input) input.click();
  });
  // A captured photo uploads immediately via the XHR uploader (progress bar +
  // toasts). Falls back to a plain submit if that IIFE didn't initialise.
  document.querySelectorAll('.cc-cam-input, .cc-pick-input').forEach(function (input) {
    input.addEventListener('change', function () {
      if (!(input.files && input.files.length && input.form)) return;
      // A browsed file only self-submits when the confirmation flow is live;
      // otherwise the cardholder still presses the form's own Upload button.
      if (input.classList.contains('cc-pick-input')
          && !document.documentElement.classList.contains('cc-attach-js')) return;
      if (input.form.id === 'ccDrop' && typeof window.__ccUpload === 'function') {
        window.__ccUpload();
      } else if (input.form.requestSubmit) {
        input.form.requestSubmit();
      } else {
        input.form.submit();
      }
    });
  });
})();

// Per-transaction upload. Dragging highlights the exact card under the pointer;
// dropping only STAGES the files. Browse and camera use the same immutable
// confirmation before the signed, transaction-scoped request begins. The plain
// form POST remains a complete no-JS fallback.
(function () {
  var forms = Array.prototype.slice.call(document.querySelectorAll('form.cc-attach-form'));
  if (!forms.length) return;
  var dialog = document.getElementById('ccDropConfirm'),
      targetBox = document.getElementById('ccDropConfirmTarget'),
      filesBox = document.getElementById('ccDropConfirmFiles'),
      cancelButton = document.getElementById('ccDropCancel'),
      attachButton = document.getElementById('ccDropAttach'),
      pending = null;

  // Everything the gallery's one-tap "Add file" route depends on is present:
  // this uploader, and a <dialog> to confirm the destination in. Only then may
  // the CSS hide the panel's own file input and Upload button — stage() refuses
  // to run without showModal, and hiding them first would strand the cardholder
  // with no way to attach anything at all.
  if (dialog && typeof dialog.showModal === 'function') {
    document.documentElement.classList.add('cc-attach-js');
  }

  function fileDrag(event) {
    var types = event.dataTransfer && event.dataTransfer.types;
    return !!(types && Array.prototype.indexOf.call(types, 'Files') !== -1);
  }
  function clearHover(except) {
    document.querySelectorAll('.cc-txn.is-drop-target').forEach(function (card) {
      if (card !== except) card.classList.remove('is-drop-target');
    });
  }
  function selectedFiles(form) {
    var files = [];
    form.querySelectorAll('input[type="file"]').forEach(function (input) {
      if (input.files) files = files.concat(Array.prototype.slice.call(input.files));
    });
    return files;
  }
  function clearFileInputs(form) {
    form.querySelectorAll('input[type="file"]').forEach(function (input) { input.value = ''; });
  }
  function cardText(card, selector) {
    var node = card.querySelector(selector);
    return node ? node.textContent.trim() : '';
  }
  function paintTarget(card) {
    targetBox.textContent = '';
    var merchant = document.createElement('strong');
    merchant.textContent = cardText(card, '.cc-txn-merchant');
    var date = document.createElement('small');
    date.textContent = cardText(card, '.cc-txn-date');
    var amount = document.createElement('span');
    amount.className = 'cc-confirm-amount';
    amount.textContent = cardText(card, '.cc-txn-amt');
    targetBox.appendChild(merchant);
    targetBox.appendChild(date);
    targetBox.appendChild(amount);
  }
  function stage(form, files) {
    var card = form.closest('.cc-txn');
    if (!card || !files.length) return;
    if (!dialog || typeof dialog.showModal !== 'function') {
      showToast('Your browser cannot confirm this receipt target. Use the Upload here button after refreshing.', 'danger');
      return;
    }
    if (pending && pending.card) pending.card.classList.remove('is-drop-confirming');
    clearHover();
    pending = { form: form, files: files.slice(), card: card };
    card.classList.add('is-drop-confirming');
    paintTarget(card);
    filesBox.textContent = files.length === 1
      ? 'File: ' + files[0].name
      : files.length + ' files: ' + files.map(function (file) { return file.name; }).join(', ');
    dialog.showModal();
    window.setTimeout(function () { attachButton.focus(); }, 0);
  }
  function finishPending(clearFiles) {
    if (!pending) return;
    pending.card.classList.remove('is-drop-confirming');
    if (clearFiles) clearFileInputs(pending.form);
    pending = null;
  }

  function upload(form, files, card) {
    var status = form.querySelector('.cc-attach-status'),
        expectedLine = Number(card.getAttribute('data-line-id')),
        xhr = new XMLHttpRequest(),
        payload = new FormData(form);
    payload.delete('receipts');
    files.forEach(function (file) { payload.append('receipts', file, file.name); });
    xhr.open('POST', form.action);
    xhr.setRequestHeader('X-Requested-With', 'fetch');
    var token = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    if (token) xhr.setRequestHeader('X-CSRFToken', token);
    form.querySelectorAll('button, input').forEach(function (control) { control.disabled = true; });
    card.classList.add('is-drop-uploading');
    if (status) status.textContent = 'Uploading to this transaction… 0%';
    xhr.upload.addEventListener('progress', function (progress) {
      if (!progress.lengthComputable || !status) return;
      var pct = Math.round(progress.loaded / progress.total * 100);
      status.textContent = pct < 100 ? 'Uploading to this transaction… ' + pct + '%' : 'Verifying attachment…';
    });
    function recover(message) {
      form.querySelectorAll('button, input').forEach(function (control) { control.disabled = false; });
      card.classList.remove('is-drop-uploading');
      if (status) status.textContent = 'Not attached — retry.';
      showToast(message, 'danger');
    }
    xhr.addEventListener('load', function () {
      var data = {};
      try { data = JSON.parse(xhr.responseText); } catch (ignore) {}
      var returnedLine = data.target && Number(data.target.line_id);
      if (xhr.status >= 200 && xhr.status < 300 && data.ok && returnedLine === expectedLine) {
        if (status) status.textContent = 'Attached to this transaction';
        window.location.reload();
      } else if (xhr.status >= 200 && xhr.status < 300 && data.ok) {
        recover('The server did not confirm the transaction target. Nothing is being shown as attached; refresh before retrying.');
      } else {
        recover(data.error || 'Could not attach that receipt — please try again.');
      }
    });
    xhr.addEventListener('error', function () {
      recover('Upload failed — check your connection and try again.');
    });
    xhr.timeout = 600000;
    xhr.addEventListener('timeout', function () {
      recover('Upload timed out — please try again.');
    });
    xhr.send(payload);
  }

  forms.forEach(function (form) {
    var card = form.closest('.cc-txn');
    if (!card) return;
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var files = selectedFiles(form), status = form.querySelector('.cc-attach-status');
      if (!files.length) {
        if (status) status.textContent = 'Choose a file or take a photo first.';
        return;
      }
      stage(form, files);
    });
    ['dragenter', 'dragover'].forEach(function (name) {
      card.addEventListener(name, function (event) {
        if (!fileDrag(event)) return;
        event.preventDefault();
        event.stopPropagation();
        event.dataTransfer.dropEffect = 'copy';
        clearHover(card);
        card.classList.add('is-drop-target');
      });
    });
    card.addEventListener('dragleave', function (event) {
      if (event.relatedTarget && card.contains(event.relatedTarget)) return;
      card.classList.remove('is-drop-target');
    });
    card.addEventListener('drop', function (event) {
      if (!fileDrag(event)) return;
      event.preventDefault();
      event.stopPropagation();
      card.classList.remove('is-drop-target');
      var files = event.dataTransfer && event.dataTransfer.files
        ? Array.prototype.slice.call(event.dataTransfer.files) : [];
      if (files.length) stage(form, files);
    });
  });

  // Prevent the browser from navigating to a dropped file when it lands in the
  // gap between cards. Only a card's own handler is allowed to stage a target.
  document.addEventListener('dragover', function (event) {
    if (fileDrag(event)) event.preventDefault();
  });
  document.addEventListener('drop', function (event) {
    if (!fileDrag(event)) return;
    event.preventDefault();
    clearHover();
  });
  document.addEventListener('dragleave', function (event) {
    if (!event.relatedTarget) clearHover();
  });

  dialog.addEventListener('close', function () { finishPending(true); });
  cancelButton.addEventListener('click', function (event) {
    // Chromium can complete a method="dialog" form submission without running
    // the dialog's close-event cleanup synchronously. Clear the highlighted
    // transaction explicitly so Cancel can never leave a stale target behind.
    event.preventDefault();
    finishPending(true);
    dialog.close('cancel');
  });
  attachButton.addEventListener('click', function () {
    if (!pending) return;
    var chosen = pending;
    pending = null;
    chosen.card.classList.remove('is-drop-confirming');
    dialog.close();
    upload(chosen.form, chosen.files, chosen.card);
  });
})();

// Save the cardholder's own reason while they type and again on blur.  A
// keepalive request is important here: clicking Upload, Personal, Next, or a
// month link immediately after typing must not cancel the save during page
// navigation. Saves are serialised so an older response can never become the
// browser's final state after a newer edit.
(function () {
  document.querySelectorAll('form[data-autosave]').forEach(function (form) {
    var input = form.querySelector('.cc-reason-input');
    if (!input || input.readOnly) return;
    var lastSaved = input.value.trim();
    var timer = null;
    var inFlight = false;
    var queued = false;
    var status = form.querySelector('.cc-save-status');
    var need = form.querySelector('.cc-need');
    var card = form.closest('.cc-txn');

    function showSaved(val) {
      status.textContent = 'saved'; status.className = 'cc-save-status ok';
      form.classList.toggle('is-missing', !val);
      if (need) need.hidden = !!val;
      if (card) card.setAttribute('data-reason', val ? 'y' : 'n');
      if (card && window.__ccReplace) window.__ccReplace(card);
      window.setTimeout(function () {
        if (status.textContent === 'saved' && input.value.trim() === lastSaved) {
          status.textContent = '';
        }
      }, 1600);
    }

    function saveLatest() {
      window.clearTimeout(timer);
      timer = null;
      var val = input.value.trim();
      if (val === lastSaved) return;
      if (inFlight) { queued = true; return; }

      var payload = new FormData(form);
      payload.set('reason', val);
      inFlight = true;
      queued = false;
      status.textContent = 'saving…'; status.className = 'cc-save-status saving';
      fetch(form.action, {
        method: 'POST',
        body: payload,
        headers: { 'X-Requested-With': 'fetch' },
        keepalive: true
      }).then(function (r) {
        if (!r.ok) throw r;
        lastSaved = val;
        if (input.value.trim() === val) showSaved(val);
        else queued = true;
      }).catch(function (err) {
        status.textContent = (err && err.status === 423 ? 'locked' : 'not saved');
        status.className = 'cc-save-status err';
        showToast(err && err.status === 423
          ? 'This month is locked — ask an admin to reopen it.'
          : 'Could not save your reason — it is still here. Please try again.', 'danger');
      }).finally(function () {
        inFlight = false;
        if (queued) { queued = false; saveLatest(); }
      });
    }

    function scheduleSave() {
      window.clearTimeout(timer);
      if (input.value.trim() === lastSaved) return;
      status.textContent = 'saving…'; status.className = 'cc-save-status saving';
      timer = window.setTimeout(saveLatest, 450);
    }

    input.addEventListener('input', scheduleSave);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); saveLatest(); input.blur(); }
    });
    input.addEventListener('blur', saveLatest);
  });
})();

// Location: searchable, multi-select chip picker. Selected locations are stored
// as a comma-joined string in the hidden `location` field (auto-saved). Type to
// filter the managed list; press Enter or click a result to select it.
(function () {
  function splitVals(s) {
    return (s || '').split(',').map(function (v) { return v.trim(); }).filter(Boolean);
  }
  function esc(s) { return s.replace(/[&<>"]/g, function (c) {
    return { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c]; }); }

  document.querySelectorAll('form.cc-locpick[data-autosave-loc]').forEach(function (form) {
    var locked  = form.querySelector('.cc-loc-control').classList.contains('is-locked');
    var hidden  = form.querySelector('.cc-loc-value');
    var box     = form.querySelector('.cc-loc-box');
    var search  = form.querySelector('.cc-loc-search');
    var menu    = form.querySelector('.cc-loc-menu');
    var status  = form.querySelector('.cc-save-status');
    var options = [];
    try { options = JSON.parse(form.getAttribute('data-options') || '[]'); } catch (e) {}
    var selected = splitVals(hidden.value);
    var last = selected.join(', ');
    var activeIndex = -1;

    function setExpanded(open) {
      menu.hidden = !open;
      search.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (!open) { activeIndex = -1; search.setAttribute('aria-activedescendant', ''); }
    }

    function setActive(index) {
      var opts = menu.querySelectorAll('.cc-loc-opt');
      if (!opts.length) { activeIndex = -1; search.setAttribute('aria-activedescendant', ''); return; }
      activeIndex = (index + opts.length) % opts.length;
      opts.forEach(function (opt, i) {
        var on = i === activeIndex;
        opt.classList.toggle('is-active', on);
        opt.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      search.setAttribute('aria-activedescendant', opts[activeIndex].id);
      opts[activeIndex].scrollIntoView({block:'nearest'});
    }

    function save() {
      var val = selected.join(', ');
      hidden.value = val;
      if (val === last) return;
      var previous = last;
      last = val;
      status.textContent = 'saving…'; status.className = 'cc-save-status saving';
      fetch(form.action, { method: 'POST', body: new FormData(form),
                           headers: { 'X-Requested-With': 'fetch' } })
        .then(function (r) {
          if (r.ok) {
            status.textContent = 'saved'; status.className = 'cc-save-status ok';
            form.classList.toggle('is-missing', !val);
            var need = form.querySelector('.cc-need');
            if (need) need.hidden = !!val;
            var card = form.closest('.cc-txn');
            if (card) card.setAttribute('data-location', val ? 'y' : 'n');
            if (card && window.__ccReplace) window.__ccReplace(card);
            setTimeout(function () { if (status.textContent === 'saved') status.textContent = ''; }, 1600);
          } else {
            last = previous;
            status.textContent = (r.status === 423 ? 'locked' : 'not saved');
            status.className = 'cc-save-status err';
            showToast(r.status === 423 ? 'This month is locked — ask an admin to reopen it.'
                                       : 'Could not save — please try again.', 'danger');
          }
        })
        .catch(function () { last = previous;
                             status.textContent = 'offline'; status.className = 'cc-save-status err';
                             showToast('You appear to be offline — change not saved.', 'danger'); });
    }

    function renderChips() {
      box.querySelectorAll('.cc-loc-chip').forEach(function (c) { c.remove(); });
      selected.forEach(function (name) {
        var chip = document.createElement('span');
        chip.className = 'cc-loc-chip';
        chip.innerHTML = '<span>' + esc(name) + '</span>';
        if (!locked) {
          var x = document.createElement('button');
          x.type = 'button'; x.className = 'cc-loc-chip-x'; x.title = 'Remove';
          x.setAttribute('aria-label', 'Remove ' + name); x.textContent = '×';
          x.addEventListener('click', function () { remove(name); });
          chip.appendChild(x);
        }
        box.insertBefore(chip, search);
      });
    }
    function add(name) {
      name = (name || '').trim();
      if (!name) return;
      var exists = selected.some(function (v) { return v.toLowerCase() === name.toLowerCase(); });
      if (!exists) { selected.push(name); renderChips(); save(); }
      search.value = ''; filter();
    }
    function remove(name) {
      selected = selected.filter(function (v) { return v !== name; });
      renderChips(); save(); filter();
    }

    function filter() {
      var q = search.value.trim().toLowerCase();
      var picked = selected.map(function (v) { return v.toLowerCase(); });
      var matches = options.filter(function (o) {
        return picked.indexOf(o.toLowerCase()) === -1 &&
               (!q || o.toLowerCase().indexOf(q) !== -1);
      });
      var html = '';
      matches.forEach(function (o, i) {
        html += '<div class="cc-loc-opt" id="' + menu.id + '-opt-' + i + '" role="option" aria-selected="false" data-val="' + esc(o) + '">' + esc(o) + '</div>';
      });
      // Selection is restricted to the managed list — typing only searches it.
      if (!html) {
        html = '<div class="cc-loc-empty">' + (q ? 'No matching location' : 'No locations') + '</div>';
      }
      menu.innerHTML = html;
      setExpanded(true);
      activeIndex = -1;
      status.textContent = matches.length + ' location' + (matches.length === 1 ? '' : 's') + ' available';
    }

    if (locked) { renderChips(); return; }

    renderChips();
    search.addEventListener('focus', filter);
    search.addEventListener('input', filter);
    search.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (menu.hidden) filter();
        setActive(activeIndex + (e.key === 'ArrowDown' ? 1 : -1));
      } else if (e.key === 'Enter') {
        // Enter picks the keyboard-highlighted match, or the first result.
        e.preventDefault();
        var opts = menu.querySelectorAll('.cc-loc-opt');
        var pick = opts[activeIndex >= 0 ? activeIndex : 0];
        if (pick && !menu.hidden) add(pick.getAttribute('data-val'));
      } else if (e.key === 'Backspace' && !search.value && selected.length) {
        remove(selected[selected.length - 1]);
      } else if (e.key === 'Escape') { setExpanded(false); }
    });
    // Clicking/tabbing away discards any leftover typed text (no free-form add).
    search.addEventListener('blur', function () { search.value = ''; });
    menu.addEventListener('mousedown', function (e) {
      var opt = e.target.closest('.cc-loc-opt');
      if (!opt) return;
      e.preventDefault();                 // keep focus in the search box
      add(opt.getAttribute('data-val'));
    });
    document.addEventListener('click', function (e) {
      if (!form.contains(e.target)) setExpanded(false);
    });
    box.addEventListener('click', function (e) {
      if (e.target === box) search.focus();
    });
    var suggestion = form.querySelector('[data-apply-location]');
    if (suggestion) suggestion.addEventListener('click', function () {
      if (selected.length) { search.focus(); return; }
      var wanted = splitVals(suggestion.getAttribute('data-apply-location'));
      selected = wanted.filter(function (value) {
        return options.some(function (option) {
          return option.toLowerCase() === value.toLowerCase();
        });
      });
      if (!selected.length) {
        showToast('That previous location is no longer available. Choose another location.', 'warning');
        search.focus();
        return;
      }
      suggestion.hidden = true;
      renderChips();
      save();
      search.focus();
    });
  });
})();

// Drop-off inbox uploader: "Choose files" opens the picker; once files are
// chosen (via picker or camera) the "Drop off" button appears with a count.
// (The camera button reuses the global .cc-cam-btn handler.)
(function () {
  var browse = document.getElementById('ccInboxBrowse');
  if (!browse) return;
  var form = document.getElementById('ccInboxForm');
  var file = document.getElementById('ccInboxFile');
  var cam = document.getElementById('ccInboxCam');
  var go = document.getElementById('ccInboxGo');
  var status = document.getElementById('ccInboxStatus');
  var progress = document.getElementById('ccInboxProgress');
  var fill = document.getElementById('ccInboxFill');
  var pct = document.getElementById('ccInboxPct');
  browse.addEventListener('click', function () { file.click(); });
  function show(inp) {
    var n = inp.files ? inp.files.length : 0;
    if (n) {
      status.textContent = n + ' file' + (n === 1 ? '' : 's') + ' ready';
      go.hidden = false;
    }
  }
  file.addEventListener('change', function () { show(file); });
  if (cam) cam.addEventListener('change', function () { show(cam); });

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var chosen = (cam && cam.files && cam.files.length) ? cam : file;
    if (!(chosen.files && chosen.files.length)) return;
    var fd = new FormData();
    Array.prototype.forEach.call(chosen.files, function (f) { fd.append('receipts', f); });
    var xhr = new XMLHttpRequest();
    xhr.open('POST', form.action);
    xhr.setRequestHeader('X-Requested-With', 'fetch');
    var token = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    if (token) xhr.setRequestHeader('X-CSRFToken', token);
    progress.hidden = false;
    form.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    xhr.upload.addEventListener('progress', function (ev) {
      if (!ev.lengthComputable) return;
      var n = Math.round(ev.loaded / ev.total * 100);
      fill.style.width = n + '%'; pct.textContent = n < 100 ? 'Uploading… ' + n + '%' : 'Processing…';
      progress.setAttribute('aria-valuenow', n);
    });
    function recover(message) {
      showToast(message, 'danger');
      progress.hidden = true;
      form.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
    }
    xhr.addEventListener('load', function () {
      var data = {};
      try { data = JSON.parse(xhr.responseText); } catch (ignore) {}
      if (xhr.status >= 200 && xhr.status < 300 && data.ok) {
        pct.textContent = 'Done'; progress.setAttribute('aria-valuenow', '100');
        window.location.reload();
      } else recover(data.error || 'Inbox upload failed — please try again.');
    });
    xhr.addEventListener('error', function () { recover('Upload failed — check your connection and try again.'); });
    xhr.timeout = 600000;
    xhr.addEventListener('timeout', function () { recover('Upload timed out — please try again.'); });
    fill.style.width = '0%'; pct.textContent = 'Uploading… 0%';
    xhr.send(fd);
  });
})();

// Bucket "Link" picker: open the checklist, filter it, enable when something's ticked.
(function () {
  document.addEventListener('click', function (e) {
    var t = e.target.closest('.cc-link-toggle');
    if (!t) return;
    var p = document.getElementById(t.getAttribute('data-target'));
    if (!p) return;
    p.hidden = !p.hidden;
    t.classList.toggle('open', !p.hidden);
    if (!p.hidden) { var s = p.querySelector('.cc-link-search'); if (s) s.focus(); }
  });
  document.querySelectorAll('.cc-linkpanel').forEach(function (panel) {
    var search = panel.querySelector('.cc-link-search');
    var go = panel.querySelector('.cc-link-go');
    panel.addEventListener('change', function () {
      go.disabled = !panel.querySelector('.cc-pick input:checked');
    });
    if (search) search.addEventListener('input', function () {
      var q = search.value.trim().toLowerCase();
      panel.querySelectorAll('.cc-pick').forEach(function (p) {
        p.style.display = (!q || (p.getAttribute('data-merchant') || '').indexOf(q) !== -1) ? '' : 'none';
      });
    });
  });
})();

// Custom "link an existing file" dropdown + the "show more" expanders.
// Delegated so it covers every per-transaction picker and both link panels.
(function () {
  function closeAllPickers(except) {
    document.querySelectorAll('.cc-filepick.open').forEach(function (fp) {
      if (fp === except) return;
      fp.classList.remove('open');
      var t = fp.querySelector('.cc-filepick-trigger');
      if (t) t.setAttribute('aria-expanded', 'false');
    });
  }
  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('.cc-filepick-trigger');
    if (trigger) {
      var fp = trigger.closest('.cc-filepick');
      var open = !fp.classList.contains('open');
      closeAllPickers(fp);
      fp.classList.toggle('open', open);
      trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
      return;
    }
    var moreBtn = e.target.closest('.cc-filepick-more-toggle');
    if (moreBtn) {
      var panel = moreBtn.nextElementSibling;
      if (panel && panel.classList.contains('cc-filepick-more')) {
        var op = panel.classList.toggle('open');
        moreBtn.classList.toggle('open', op);
        moreBtn.setAttribute('aria-expanded', op ? 'true' : 'false');
      }
      return;
    }
    if (!e.target.closest('.cc-filepick')) closeAllPickers(null);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeAllPickers(null);
  });
})();

// Bulk submit: tick transactions, then "Submit selected". Individual submit /
// undo are plain form posts (work without JS); this adds the multi-select path.
(function () {
  var bar = document.getElementById('ccBulkBar');
  if (!bar) return;
  var selectReady = document.getElementById('ccSelectReady'),
      clear = document.getElementById('ccBulkClear'), countEl = document.getElementById('ccBulkCount'),
      btn = document.getElementById('ccBulkSubmit'),
      action = bar.getAttribute('data-action'),
      sid = bar.getAttribute('data-sid');

  function boxes() { return Array.prototype.slice.call(document.querySelectorAll('.cc-sel:not(:disabled)')); }
  function checked() { return boxes().filter(function (b) { return b.checked; }); }
  // Respect active filters, but not the collapsed Completed container: ready
  // transactions are intentionally tucked there until selected.
  function filtered() {
    return boxes().filter(function (b) {
      var row = b.closest('.cc-txn');
      return row && row.style.display !== 'none';
    });
  }

  function refresh() {
    var n = checked().length;
    countEl.textContent = n + ' selected';
    btn.disabled = !n;
    bar.hidden = n === 0;
  }

  document.addEventListener('change', function (e) {
    if (e.target && e.target.classList && e.target.classList.contains('cc-sel')) refresh();
  });
  if (selectReady) selectReady.addEventListener('click', function () {
    filtered().forEach(function (b) { b.checked = true; });
    refresh();
  });
  if (clear) clear.addEventListener('click', function () {
    boxes().forEach(function (b) { b.checked = false; });
    refresh();
  });

  btn.addEventListener('click', function () {
    var ids = checked().map(function (b) { return b.value; });
    if (!ids.length) return;
    if (!confirm('Submit ' + ids.length + ' transaction' + (ids.length === 1 ? '' : 's') +
                 ' to finance? You can still change anything afterwards.')) return;
    var fd = new FormData();
    fd.append('statement_id', sid);
    ids.forEach(function (id) { fd.append('line_id', id); });
    btn.disabled = true;
    btn.innerHTML = '<i class="bi bi-hourglass-split me-1"></i>Submitting…';
    fetch(action, { method: 'POST', body: fd, headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) {
        return r.json().then(function (data) {
          if (!r.ok) throw new Error(data.error || 'Could not submit.');
          return data;
        });
      })
      .then(function () { window.location.reload(); })
      .catch(function (err) {
        showToast(err.message || 'Could not submit — please try again.', 'danger');
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-send-check me-1"></i>Submit selected';
      });
  });

  refresh();
})();

// "Continue" links land on the exact missing field, while the compact progress
// bar appears only after the full summary has scrolled away.
(function () {
  document.addEventListener('click', function (e) {
    var link = e.target.closest('a[data-next-action]');
    if (!link) return;
    var href = link.getAttribute('href') || '';
    if (href.charAt(0) !== '#') return;
    var card = document.getElementById(href.slice(1));
    if (!card) return;
    e.preventDefault();
    var done = document.getElementById('ccDone'), doneHead = document.getElementById('ccDoneHead');
    if (done && done.contains(card)) {
      done.hidden = false;
      if (doneHead) { doneHead.classList.add('open'); doneHead.setAttribute('aria-expanded', 'true'); }
    }
    card.classList.remove('is-collapsed');
    var toggle = card.querySelector('.cc-txn-toggle');
    if (toggle) toggle.setAttribute('aria-expanded', 'true');
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    window.setTimeout(function () {
      if (link.getAttribute('data-next-action') === 'receipt') {
        // Focus, don't click: "Add file" now opens the OS file picker, and a
        // picker opened from this timer has lost the user-activation it needs
        // (Safari blocks it outright). Land on the control instead — camera
        // first, since on a phone the slip is usually in the cardholder's hand.
        var entry = card.querySelector('.cc-rcpt-cam');
        if (!entry || !entry.offsetParent) entry = card.querySelector('.cc-rcpt-add');
        if (entry) entry.focus();
      } else {
        var reason = card.querySelector('.cc-reason-input');
        if (reason) reason.focus();
      }
      card.classList.add('cc-focus-pulse');
      window.setTimeout(function () { card.classList.remove('cc-focus-pulse'); }, 1100);
    }, 420);
  });

  var sticky = document.getElementById('ccStickyProgress'), head = document.querySelector('.cc-head');
  if (!sticky || !head) return;
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (entries) {
      var show = !entries[0].isIntersecting;
      sticky.classList.toggle('show', show);
      sticky.setAttribute('aria-hidden', show ? 'false' : 'true');
    }, { threshold: 0.05 }).observe(head);
  }
})();

// Image lightbox: tap a photo receipt to preview it inline (PDFs open in a tab).
(function () {
  var box = document.getElementById('ccLightbox');
  if (!box) return;
  var img = document.getElementById('ccLbImg'), name = document.getElementById('ccLbName'),
      close = document.getElementById('ccLbClose'), lastFocus = null;
  function open(src, label) {
    lastFocus = document.activeElement; img.src = src; name.textContent = label || 'Receipt preview';
    box.hidden = false; close.focus();
  }
  function hide() {
    box.hidden = true; img.src = '';
    if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
  }
  document.addEventListener('click', function (e) {
    var a = e.target.closest('a[data-src], a[data-preview-src]');
    if (!a) return;
    e.preventDefault();
    open(a.getAttribute('data-src') || a.getAttribute('data-preview-src'), a.getAttribute('data-name'));
  });
  close.addEventListener('click', hide);
  box.addEventListener('click', function (e) { if (e.target === box) hide(); });
  document.addEventListener('keydown', function (e) {
    if (box.hidden) return;
    if (e.key === 'Escape') hide();
    else if (e.key === 'Tab') { e.preventDefault(); close.focus(); }
  });
})();
