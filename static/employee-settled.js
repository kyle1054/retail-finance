/* Settled-plan folds on the employee profile.
 *
 * Completed / written-off rows are rendered inside the same table as the live
 * ones (so the tfoot totals stay whole-history) but hidden by default. Each
 * section gets one summary row that reveals them in place. The open/closed
 * choice is remembered per section across employees, because whether you want
 * history on screen is a habit, not a per-record decision. */
(function () {
    'use strict';

    var PREFS_KEY = 'northwind.settledFolds';

    function readPrefs() {
        try {
            return JSON.parse(window.localStorage.getItem(PREFS_KEY)) || {};
        } catch (err) {
            return {};
        }
    }

    function writePrefs(prefs) {
        try {
            window.localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
        } catch (err) {
            /* Private mode / full quota — the fold still works for this page. */
        }
    }

    function apply(group, revealed) {
        document.querySelectorAll('tr.settled-row[data-settled-group="' + group + '"]')
            .forEach(function (row) {
                row.classList.toggle('is-revealed', revealed);
            });
        document.querySelectorAll('[data-settled-toggle="' + group + '"]')
            .forEach(function (button) {
                button.setAttribute('aria-expanded', String(revealed));
                var action = button.querySelector('.settled-fold-action');
                if (action) action.textContent = revealed ? 'Hide' : 'Show';
            });
    }

    document.addEventListener('DOMContentLoaded', function () {
        var prefs = readPrefs();
        document.querySelectorAll('[data-settled-toggle]').forEach(function (button) {
            var group = button.dataset.settledToggle;
            if (prefs[group]) apply(group, true);
        });
    });

    document.addEventListener('click', function (event) {
        var button = event.target.closest('[data-settled-toggle]');
        if (!button) return;
        var group = button.dataset.settledToggle;
        var revealed = button.getAttribute('aria-expanded') !== 'true';
        apply(group, revealed);
        var prefs = readPrefs();
        if (revealed) {
            prefs[group] = 1;
        } else {
            delete prefs[group];
        }
        writePrefs(prefs);
    });
})();
