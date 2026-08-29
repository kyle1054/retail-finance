/* Cash-recon ledger: category picker, inline validation, and a saved draft.
 *
 * The draft matters more than it looks. Stores fill this in on a shop-floor
 * tablet, where the tab gets evicted when someone switches app and a POST can
 * come back as an expired-session redirect. Both of those used to land the
 * person on a blank form with everything they had typed gone. So every
 * keystroke is mirrored to localStorage, keyed by store+month, and the ONLY
 * thing that clears it is the server confirming the entry landed (?added=1).
 */
(function () {
    "use strict";

    document.addEventListener("click", function (event) {
        var editButton = event.target.closest("[data-edit-entry]");
        if (!editButton) return;
        var row = document.getElementById("editRow-" + editButton.dataset.editEntry);
        if (row) row.hidden = !row.hidden;
    });

    var categoryId = document.getElementById("catId");
    if (!categoryId) return;
    var typeBar = document.getElementById("typeBar");
    var grid = document.getElementById("tileGrid");
    var tiles = Array.from(grid.querySelectorAll(".cr-tile"));
    var badge = document.getElementById("dirBadge");
    var noteInput = document.getElementById("noteInput");
    var amount = document.getElementById("amountInput");
    var dateInput = document.getElementById("dateInput");
    var form = document.getElementById("addEntryForm");
    var pickerButton = document.getElementById("pickerBtn");
    var pickerLabel = document.getElementById("pickerLabel");
    var panel = document.getElementById("pickerPanel");
    var addButton = document.getElementById("addBtn");
    var draftNote = document.getElementById("draftNote");
    var draftNoteText = document.getElementById("draftNoteText");
    var errors = {
        category: document.getElementById("catError"),
        amount: document.getElementById("amountError"),
        note: document.getElementById("noteError")
    };
    var selectedTile = null;

    // ── Draft storage ────────────────────────────────────────────────────────
    // Keyed by the form's POST target, so it is per store AND per month: a draft
    // typed for August must not reappear when someone pages back to July.
    var DRAFT_KEY = "northwind:cash-draft:" + form.getAttribute("action");
    var DRAFT_MAX_AGE_MS = 24 * 60 * 60 * 1000;

    function readDraft() {
        try {
            var raw = window.localStorage.getItem(DRAFT_KEY);
            if (!raw) return null;
            var draft = JSON.parse(raw);
            if (!draft || !draft.savedAt || Date.now() - draft.savedAt > DRAFT_MAX_AGE_MS) {
                clearDraft();
                return null;
            }
            return draft;
        } catch (_error) {
            return null;   // private mode / storage disabled — carry on without drafts
        }
    }

    function saveDraft() {
        // An empty form is not a draft worth keeping (and would otherwise resurrect
        // itself as a "we brought your entry back" note on every page load).
        if (!categoryId.value && !amount.value && !noteInput.value.trim()) {
            clearDraft();
            return;
        }
        try {
            window.localStorage.setItem(DRAFT_KEY, JSON.stringify({
                categoryId: categoryId.value,
                amount: amount.value,
                note: noteInput.value,
                entryDate: dateInput ? dateInput.value : "",
                savedAt: Date.now()
            }));
        } catch (_error) { /* quota or storage unavailable — nothing to do */ }
    }

    function clearDraft() {
        try { window.localStorage.removeItem(DRAFT_KEY); } catch (_error) { /* as above */ }
    }

    // ── Field errors (inline, not alert(): a modal on a tablet hides the field
    //    it is complaining about and takes a second tap to clear) ─────────────
    function showError(field, message) {
        var slot = errors[field];
        if (!slot) return;
        slot.textContent = message;
        slot.hidden = false;
        var input = field === "amount" ? amount : field === "note" ? noteInput : null;
        if (input) input.setAttribute("aria-invalid", "true");
        if (field === "category") pickerButton.classList.add("invalid");
    }

    function clearError(field) {
        var slot = errors[field];
        if (!slot) return;
        slot.hidden = true;
        slot.textContent = "";
        var input = field === "amount" ? amount : field === "note" ? noteInput : null;
        if (input) input.removeAttribute("aria-invalid");
        if (field === "category") pickerButton.classList.remove("invalid");
    }

    // ── Category picker ──────────────────────────────────────────────────────
    function openPanel() {
        panel.hidden = false;
        pickerButton.classList.add("open");
        pickerButton.setAttribute("aria-expanded", "true");
    }
    function closePanel() {
        panel.hidden = true;
        pickerButton.classList.remove("open");
        pickerButton.setAttribute("aria-expanded", "false");
    }

    pickerButton.addEventListener("click", function () {
        if (panel.hidden) openPanel();
        else closePanel();
    });

    document.addEventListener("click", function (event) {
        if (!panel.hidden && !panel.contains(event.target) && !pickerButton.contains(event.target)) {
            closePanel();
        }
    });

    function showKind(kind) {
        typeBar.querySelectorAll(".cr-type").forEach(function (item) {
            item.classList.toggle("active", item.dataset.kind === kind);
        });
        tiles.forEach(function (tile) {
            tile.hidden = tile.dataset.kind !== kind;
        });
    }

    typeBar.addEventListener("click", function (event) {
        var button = event.target.closest(".cr-type");
        if (!button) return;
        showKind(button.dataset.kind);
    });

    function selectTile(tile) {
        if (selectedTile) selectedTile.classList.remove("selected");
        tile.classList.add("selected");
        selectedTile = tile;
        categoryId.value = tile.dataset.id;
        pickerLabel.textContent = tile.dataset.name;
        pickerButton.classList.add("chosen");
        clearError("category");
        refresh();
    }

    grid.addEventListener("click", function (event) {
        var tile = event.target.closest(".cr-tile");
        if (!tile) return;
        selectTile(tile);
        closePanel();
        saveDraft();
    });

    function refresh() {
        var direction = selectedTile ? selectedTile.dataset.dir : null;
        var hint = selectedTile ? selectedTile.dataset.hint || "" : "";
        badge.classList.remove("direction-in", "direction-out");
        if (direction === "in") {
            badge.hidden = false;
            badge.classList.add("direction-in");
            badge.textContent = "↑  Money IN — adds to the float";
        } else if (direction === "out") {
            badge.hidden = false;
            badge.classList.add("direction-out");
            badge.textContent = "↓  Money OUT — taken from the float";
        } else {
            badge.hidden = true;
        }
        noteInput.placeholder = hint || "Reason for this entry";
    }

    // ── Restore, or drop the draft if the server confirmed the save ───────────

    // The draft is deliberately kept on every path except ?added=1 — but "no
    // ?added=1" does not prove the entry was NOT written. A POST can commit and
    // then lose its response: the tablet drops wifi, or iPadOS evicts the tab
    // mid-request. That is the very failure this draft exists for, so it is a
    // likely path, not a remote one — and re-offering the entry there would let a
    // store post the same cash line twice, with nothing in the DB to stop it.
    // So before restoring, look for the exact line this draft would create among the
    // rows already on screen. Category, cents, day AND reason must all agree — the
    // reason is what keeps a deliberate second identical line (two R50 milk runs in
    // one day) from being read as a lost response, since those are typed with their
    // own wording while a re-offered draft carries the byte-identical one. A miss
    // just restores the draft as before, so either way the tie-break is the safe one.
    function alreadyOnLedger(draft) {
        var cents = Math.round(parseFloat(draft.amount) * 100);
        if (!draft.categoryId || !draft.entryDate || !isFinite(cents)) return false;
        var key = draft.categoryId + "|" + cents + "|" + draft.entryDate;
        var note = (draft.note || "").trim();
        return Array.prototype.some.call(
            document.querySelectorAll("tr.cr-row[data-line]"),
            function (row) {
                return row.dataset.line === key && (row.dataset.note || "").trim() === note;
            });
    }

    function restoreDraft() {
        var params = new URLSearchParams(window.location.search);
        if (params.has("added")) {
            // The entry is on the ledger — the draft has served its purpose. Drop
            // ?added=1 from the URL too, so a later reload isn't read as a fresh
            // save and wipe a draft typed in the meantime.
            clearDraft();
            params.delete("added");
            var query = params.toString();
            window.history.replaceState({}, "",
                window.location.pathname + (query ? "?" + query : ""));
            return;
        }
        var draft = readDraft();
        if (!draft) return;
        if (alreadyOnLedger(draft)) {
            // It saved; only the response was lost. Leave the form empty and say so,
            // rather than refilling it and inviting a second identical entry.
            clearDraft();
            if (draftNote && draftNoteText) {
                draftNoteText.textContent = "That entry did save — it is on the ledger below.";
                draftNote.hidden = false;
            }
            return;
        }
        if (draft.amount) amount.value = draft.amount;
        if (draft.note) noteInput.value = draft.note;
        if (draft.entryDate && dateInput) dateInput.value = draft.entryDate;
        if (draft.categoryId) {
            var tile = tiles.find(function (item) { return item.dataset.id === draft.categoryId; });
            if (tile) {
                showKind(tile.dataset.kind);   // the tile is hidden unless its type is showing
                selectTile(tile);
            }
        }
        if (draftNote) draftNote.hidden = false;
    }

    refresh();
    restoreDraft();

    [amount, noteInput, dateInput].forEach(function (input) {
        if (!input) return;
        input.addEventListener("input", function () {
            if (input === amount) clearError("amount");
            if (input === noteInput) clearError("note");
            if (draftNote) draftNote.hidden = true;
            saveDraft();
        });
    });

    form.addEventListener("submit", function (event) {
        var value = parseFloat(amount.value);
        clearError("category");
        clearError("amount");
        clearError("note");

        if (!categoryId.value) {
            event.preventDefault();
            showError("category", "Choose a category first.");
            openPanel();
            pickerButton.focus();
            return;
        }
        if (!(value > 0)) {
            event.preventDefault();
            showError("amount", "Enter an amount greater than zero.");
            amount.focus();
            return;
        }
        if (!noteInput.value.trim()) {
            event.preventDefault();
            showError("note", "A reason is required for every entry.");
            noteInput.focus();
            return;
        }
        // Valid: keep the draft (only ?added=1 clears it) and lock the button so a
        // second impatient tap on slow store wifi can't post the entry twice.
        saveDraft();
        addButton.disabled = true;
        addButton.innerHTML = '<i class="bi bi-hourglass-split"></i> Saving…';
    });

    // Back-button / restored-from-cache: the button must not stay stuck on "Saving…".
    window.addEventListener("pageshow", function (event) {
        if (!event.persisted) return;
        addButton.disabled = false;
        addButton.innerHTML = '<i class="bi bi-plus-lg"></i> Add to ledger';
    });
})();
