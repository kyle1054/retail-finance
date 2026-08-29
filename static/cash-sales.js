(function () {
    "use strict";
    var table = document.getElementById("csTable");
    if (!table) return;
    var zeroToggle = document.querySelector("[data-show-zero-stores]");
    if (zeroToggle) {
        zeroToggle.addEventListener("change", function () {
            table.classList.toggle("show-zero", zeroToggle.checked);
            if (!zeroToggle.checked) {
                table.querySelectorAll('.cs-detail[data-zero-detail="true"]').forEach(function (detail) {
                    detail.hidden = true;
                });
                table.querySelectorAll('.cs-row.is-zero [data-cash-sales-toggle]').forEach(function (button) {
                    button.setAttribute("aria-expanded", "false");
                });
                table.querySelectorAll(".cs-row.is-zero .cs-chev").forEach(function (chevron) {
                    chevron.classList.remove("is-open");
                });
            }
        });
    }
    table.addEventListener("click", function (event) {
        var button = event.target.closest("[data-cash-sales-toggle]");
        var row = event.target.closest(".cs-row");
        if (!button && row && !event.target.closest("a, button, input, select, textarea")) {
            button = row.querySelector("[data-cash-sales-toggle]");
        }
        if (!button || !row) return;
        var detail = document.getElementById(button.getAttribute("aria-controls"));
        if (!detail || !detail.classList.contains("cs-detail")) return;
        detail.hidden = !detail.hidden;
        row.classList.toggle("is-open", !detail.hidden);
        button.setAttribute("aria-expanded", String(!detail.hidden));
        button.setAttribute(
            "aria-label",
            (detail.hidden ? "Show cash sales for " : "Hide cash sales for ") +
                button.querySelector("span").textContent.trim()
        );
        var chevron = row.querySelector(".cs-chev");
        if (chevron) chevron.classList.toggle("is-open", !detail.hidden);
    });
})();
