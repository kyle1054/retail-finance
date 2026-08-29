(function () {
    "use strict";

    var config = document.getElementById("monthlyConfig");
    window.monthNavTarget = config ? config.dataset.monthNavTarget : "";

    var payAllForm = document.getElementById("payAllForm");
    var confirmPayAllBtn = document.getElementById("confirmPayAllBtn");
    if (payAllForm) {
        payAllForm.addEventListener("submit", function (event) {
            if (payAllForm.dataset.confirmed === "1") return;
            event.preventDefault();
            new bootstrap.Modal(document.getElementById("payAllModal")).show();
        });
    }
    if (payAllForm && confirmPayAllBtn) {
        confirmPayAllBtn.addEventListener("click", function () {
            payAllForm.dataset.confirmed = "1";
            payAllForm.requestSubmit();
        });
    }

    var search = document.getElementById("monthlySearch");

    /* On phones the complete category/action matrix is progressively disclosed
       per employee. Without JavaScript the class is never added, so every
       amount and action remains visible as the safe fallback. */
    var monthlyTable = document.querySelector(".monthly-data-table");
    if (monthlyTable) monthlyTable.classList.add("is-mobile-compact");
    document.addEventListener("click", function (event) {
        var toggle = event.target.closest(".monthly-row-toggle");
        if (!toggle || !monthlyTable || !monthlyTable.contains(toggle)) return;
        var row = toggle.closest("tr");
        if (!row) return;
        var expanded = row.classList.toggle("is-expanded");
        toggle.setAttribute("aria-expanded", String(expanded));
        var label = toggle.querySelector("span");
        if (label) label.textContent = expanded ? "Hide breakdown" : "Actions & breakdown";
    });

    if (search) {
        search.addEventListener("input", function () {
            var query = search.value.toLowerCase();
            document.querySelectorAll("tbody tr").forEach(function (row) {
                if (row.classList.contains("monthly-store-row") || row.querySelector("td[colspan]")) return;
                var nameCell = row.querySelector("td:nth-child(2)");
                var name = nameCell ? nameCell.textContent.toLowerCase() : "";
                row.hidden = Boolean(query && !name.includes(query));
            });
        });
    }

    var selectAll = document.getElementById("mtickAll");
    var selectedButton = document.getElementById("tickSelectedBtn");
    var selectedCount = document.getElementById("tickSelectedCount");
    var selectedForm = document.getElementById("tickSelectedForm");
    if (selectedButton && selectedForm) {
        function boxes() {
            return Array.from(document.querySelectorAll(".mtick"));
        }
        function visible(box) {
            var row = box.closest("tr");
            return !row || !row.hidden;
        }
        function checked() {
            return boxes().filter(function (box) { return box.checked && visible(box); });
        }
        function updateSelected() {
            var count = checked().length;
            if (selectedCount) selectedCount.textContent = String(count);
            selectedButton.disabled = count === 0;
        }
        document.addEventListener("change", function (event) {
            if (event.target.classList && event.target.classList.contains("mtick")) updateSelected();
        });
        if (search) search.addEventListener("input", updateSelected);
        if (selectAll) {
            selectAll.addEventListener("change", function () {
                boxes().forEach(function (box) {
                    if (visible(box)) box.checked = selectAll.checked;
                });
                updateSelected();
            });
        }
        selectedForm.addEventListener("submit", function (event) {
            var selection = checked();
            if (!selection.length) {
                event.preventDefault();
                return;
            }
            selectedForm.querySelectorAll('input[name="emp_ids"]').forEach(function (input) {
                input.remove();
            });
            selection.forEach(function (box) {
                var input = document.createElement("input");
                input.type = "hidden";
                input.name = "emp_ids";
                input.value = box.value;
                selectedForm.appendChild(input);
            });
        });
    }

    function toggleInvoice(button) {
        var row = button.closest(".invoice-row");
        var detail = document.getElementById(button.getAttribute("aria-controls"));
        if (!detail) return;
        var isOpen = !detail.classList.contains("d-none");
        detail.classList.toggle("d-none", isOpen);
        row.classList.toggle("is-open", !isOpen);
        button.setAttribute("aria-expanded", String(!isOpen));
        button.setAttribute(
            "aria-label",
            (isOpen ? "Show" : "Hide") + " employees for invoice " + row.dataset.so
        );
    }

    document.addEventListener("click", function (event) {
        var copyButton = event.target.closest("[data-invoice-copy]");
        if (copyButton) {
            event.stopPropagation();
            var amount = Number(copyButton.dataset.amount);
            var text = copyButton.dataset.so + "\tR " + amount.toFixed(2);
            window.copyToClipboard(text, copyButton);
            return;
        }

        var copyAll = event.target.closest("[data-copy-all-invoices]");
        if (copyAll) {
            var lines = Array.from(document.querySelectorAll(".invoice-row")).map(function (row) {
                return row.querySelector("td:nth-child(2)").textContent.trim() + "\t" +
                    row.querySelector("td:nth-child(5)").textContent.trim();
            });
            window.copyToClipboard(lines.join("\n"), copyAll);
            return;
        }

        var invoiceToggle = event.target.closest("[data-invoice-toggle]");
        if (invoiceToggle) toggleInvoice(invoiceToggle);
    });
})();
