(() => {
    'use strict';

    // ── Review panels ───────────────────────────────────────────────────────
    // /cards/review renders one empty offcanvas shell per row and fetches the
    // body on demand. Inlining every panel put ~90 elements and three <form>s
    // per row into a page that is Cache-Control: no-store, so a
    // hundred-transaction month re-downloaded and re-laid-out about 500 KB of
    // panels a person reads one row at a time.
    //
    // The same fragment feeds two presentations of that panel:
    //   • narrow screens — the Bootstrap offcanvas, opened declaratively by the
    //     row's Review control, exactly as before;
    //   • wide screens — the persistent stage beside the queue, so finance
    //     reads evidence without opening and closing a drawer per transaction.
    // With JavaScript unavailable neither runs and the row's Review link
    // navigates to the card month, which is what it did before any of this.
    const WORKSPACE_QUERY = '(min-width: 1200px)';

    function panelMarkup(url) {
        return window.fetch(url, {
            headers: { 'X-Requested-With': 'fetch' },
            credentials: 'same-origin'
        }).then((response) => {
            if (!response.ok) throw new Error(String(response.status));
            return response.text();
        });
    }

    function panelError(container, fallback, message) {
        container.textContent = '';
        const note = document.createElement('p');
        note.className = 'cc-drawer-error';
        note.textContent = message;
        container.appendChild(note);
        if (!fallback) return;
        const link = document.createElement('a');
        link.className = 'btn btn-outline-secondary w-100';
        link.href = fallback;
        link.textContent = 'Open this card month';
        container.appendChild(link);
    }

    document.addEventListener('show.bs.offcanvas', (event) => {
        const drawer = event.target;
        if (!drawer || !drawer.dataset || !drawer.dataset.ccDrawerUrl) return;
        const state = drawer.dataset.ccDrawerState;
        if (state === 'loading' || state === 'ready') return;
        drawer.dataset.ccDrawerState = 'loading';
        panelMarkup(drawer.dataset.ccDrawerUrl).then((markup) => {
            // Same-origin, admin-only, Jinja-autoescaped template fragment —
            // the identical markup this page used to inline. No client-side
            // value is interpolated into it.
            drawer.innerHTML = markup;
            drawer.dataset.ccDrawerState = 'ready';
        }).catch(() => {
            drawer.dataset.ccDrawerState = 'error';
            const body = drawer.querySelector('[data-cc-drawer-body]');
            if (body) {
                panelError(body, drawer.dataset.ccDrawerFallback,
                    'This transaction could not be loaded.');
            }
        });
    });

    // ── Bulk "mark reconciled" selection ────────────────────────────────────
    const form = document.getElementById('ccReviewActionForm');
    if (!form) return;

    const selectAll = form.querySelector('[data-cc-select-all]');
    const rowBoxes = Array.from(form.querySelectorAll('[data-cc-line]:not(:disabled)'));
    const count = form.querySelector('[data-cc-selected-count]');
    const submit = form.querySelector('[data-cc-reconcile]');

    function refresh() {
        const selected = rowBoxes.filter((checkbox) => checkbox.checked).length;
        if (count) count.textContent = `${selected} selected`;
        if (submit) submit.disabled = selected === 0;
        if (selectAll) {
            selectAll.checked = rowBoxes.length > 0 && selected === rowBoxes.length;
            selectAll.indeterminate = selected > 0 && selected < rowBoxes.length;
        }
    }

    if (selectAll) {
        selectAll.addEventListener('change', () => {
            rowBoxes.forEach((checkbox) => {
                checkbox.checked = selectAll.checked;
            });
            refresh();
        });
    }
    rowBoxes.forEach((checkbox) => checkbox.addEventListener('change', refresh));
    form.addEventListener('submit', (event) => {
        const selected = rowBoxes.filter((checkbox) => checkbox.checked).length;
        if (!selected) {
            event.preventDefault();
            return;
        }
        const noun = selected === 1 ? 'transaction' : 'transactions';
        if (!window.confirm(`Mark ${selected} ${noun} reconciled in Xero?`)) {
            event.preventDefault();
        }
    });
    refresh();

    // ── Split workspace ─────────────────────────────────────────────────────
    const workspace = document.querySelector('[data-cc-workspace]');
    const stage = workspace && workspace.querySelector('[data-cc-stage]');
    const rows = workspace
        ? Array.from(workspace.querySelectorAll('[data-cc-row]')) : [];
    if (!stage || !rows.length || !window.matchMedia) return;

    const wide = window.matchMedia(WORKSPACE_QUERY);
    let active = null;

    function openControl(row) {
        return row.querySelector('.cc-review-open');
    }

    /* At workspace widths the row's Review control feeds the stage instead of
       the offcanvas. Bootstrap opens offcanvases from a delegated document
       listener, so the toggle attributes come off the control entirely (and go
       back on when the viewport narrows) rather than being fought with
       stopPropagation. */
    function claimControls() {
        rows.forEach((row) => {
            const control = openControl(row);
            if (!control || !control.dataset.bsToggle) return;
            control.dataset.ccBsTarget = control.dataset.bsTarget || '';
            delete control.dataset.bsToggle;
            delete control.dataset.bsTarget;
        });
    }

    function releaseControls() {
        rows.forEach((row) => {
            const control = openControl(row);
            if (!control || control.dataset.ccBsTarget === undefined) return;
            control.dataset.bsToggle = 'offcanvas';
            control.dataset.bsTarget = control.dataset.ccBsTarget;
            delete control.dataset.ccBsTarget;
        });
    }

    function show(row, { focusStage = false } = {}) {
        if (!row) return;
        if (active && active !== row) active.classList.remove('is-active');
        active = row;
        row.classList.add('is-active');
        if (stage.dataset.ccStageLine === row.dataset.ccLineId) return;
        stage.dataset.ccStageLine = row.dataset.ccLineId;
        stage.textContent = '';
        const loading = document.createElement('p');
        loading.className = 'cc-drawer-loading';
        loading.textContent = 'Loading transaction…';
        stage.appendChild(loading);
        panelMarkup(row.dataset.ccPanel).then((markup) => {
            if (stage.dataset.ccStageLine !== row.dataset.ccLineId) return;
            // Same-origin, admin-only, autoescaped fragment (see above).
            stage.innerHTML = markup;
            if (focusStage) {
                const heading = stage.querySelector('.offcanvas-title');
                if (heading) {
                    heading.setAttribute('tabindex', '-1');
                    heading.focus();
                }
            }
        }).catch(() => {
            if (stage.dataset.ccStageLine !== row.dataset.ccLineId) return;
            const control = openControl(row);
            panelError(stage, control ? control.getAttribute('href') : '',
                'This transaction could not be loaded.');
        });
    }

    function activate() {
        claimControls();
        stage.hidden = false;
        workspace.classList.add('is-split');
        show(active || rows[0]);
    }

    function deactivate() {
        releaseControls();
        stage.hidden = true;
        workspace.classList.remove('is-split');
        if (active) active.classList.remove('is-active');
        active = null;
        delete stage.dataset.ccStageLine;
        stage.textContent = '';
    }

    workspace.addEventListener('click', (event) => {
        if (!wide.matches) return;
        const row = event.target.closest('[data-cc-row]');
        if (!row) return;
        // Selection, the VAT toggle and the pager keep their own behaviour.
        if (event.target.closest('input, label, button')) return;
        const control = event.target.closest('.cc-review-open');
        if (control || !event.target.closest('a')) {
            event.preventDefault();
            show(row, { focusStage: Boolean(control) });
        }
    });

    /* "Reconcile and open next" drives the queue's own reconcile form with a
       single line, so it goes through the same POST, confirm and server-side
       readiness re-check as the bulk action. The reload lands on a queue this
       transaction has left, and the stage opens whatever is now first. */
    document.addEventListener('click', (event) => {
        const button = event.target.closest('[data-cc-close-line]');
        if (!button) return;
        const box = form.querySelector(
            `[data-cc-line][value="${CSS.escape(button.dataset.ccCloseLine)}"]`);
        if (!box || box.disabled) return;
        rowBoxes.forEach((checkbox) => { checkbox.checked = checkbox === box; });
        refresh();
        if (typeof form.requestSubmit === 'function') form.requestSubmit();
        else form.submit();
    });

    // Keyboard queue navigation, workspace widths only.
    document.addEventListener('keydown', (event) => {
        if (!wide.matches || event.metaKey || event.ctrlKey || event.altKey) return;
        const target = event.target;
        if (target && (target.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName))) return;
        const step = (event.key === 'ArrowDown' || event.key === 'j') ? 1
            : (event.key === 'ArrowUp' || event.key === 'k') ? -1 : 0;
        if (step) {
            event.preventDefault();
            const at = active ? rows.indexOf(active) : -1;
            const next = rows[Math.min(rows.length - 1, Math.max(0, at + step))];
            show(next);
            if (next) next.scrollIntoView({ block: 'nearest' });
            return;
        }
        if (!active) return;
        if (event.key === 'x') {
            const box = active.querySelector('[data-cc-line]:not(:disabled)');
            if (!box) return;
            event.preventDefault();
            box.checked = !box.checked;
            refresh();
            return;
        }
        if (event.key === 'r') {
            const close = stage.querySelector('[data-cc-close-line]');
            if (!close) return;
            event.preventDefault();
            close.click();
        }
    });

    if (wide.addEventListener) {
        wide.addEventListener('change', () => (wide.matches ? activate() : deactivate()));
    } else if (wide.addListener) {
        wide.addListener(() => (wide.matches ? activate() : deactivate()));
    }
    if (wide.matches) activate();
})();
