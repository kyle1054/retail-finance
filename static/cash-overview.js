(function () {
    "use strict";
    var table = document.getElementById("ovTable");
    var config = document.getElementById("cashOverviewConfig");

    function toggleDetail(button, row, detail, chevronSelector, label) {
        detail.hidden = !detail.hidden;
        row.classList.toggle("is-open", !detail.hidden);
        button.setAttribute("aria-expanded", String(!detail.hidden));
        button.setAttribute("aria-label", (detail.hidden ? "Show " : "Hide ") + label);
        var chevron = row.querySelector(chevronSelector);
        if (chevron) chevron.classList.toggle("is-open", !detail.hidden);
    }

    if (table) {
        table.addEventListener("click", function (event) {
            var interactive = event.target.closest("a, button, input, select, textarea");
            var dayRow = event.target.closest(".day-row");
            var dayButton = event.target.closest("[data-cash-day-toggle]");
            if (!dayButton && dayRow && !interactive) {
                dayButton = dayRow.querySelector("[data-cash-day-toggle]");
            }
            if (dayButton && dayRow && table.contains(dayRow)) {
                var dayDetail = dayRow.nextElementSibling;
                if (dayDetail && dayDetail.classList.contains("day-detail")) {
                    toggleDetail(
                        dayButton, dayRow, dayDetail, ".daychev",
                        "entries for " + dayButton.dataset.cashDayLabel
                    );
                }
                return;
            }

            var row = event.target.closest(".store-row");
            var storeButton = event.target.closest("[data-cash-store-toggle]");
            if (!storeButton && row && !interactive) {
                storeButton = row.querySelector("[data-cash-store-toggle]");
            }
            if (!storeButton || !row) return;
            var expansion = document.getElementById(storeButton.getAttribute("aria-controls"));
            if (!expansion || !expansion.classList.contains("expand-row")) return;
            toggleDetail(
                storeButton, row, expansion, ".chev",
                "daily breakdown for " + storeButton.querySelector("span").textContent.trim()
            );
            if (expansion.hidden || row.dataset.loaded !== "0") return;

            row.dataset.loaded = "1";
            var body = expansion.querySelector(".expand-body");
            fetch(row.dataset.url, {credentials: "same-origin"})
                .then(function (response) {
                    if (!response.ok) throw new Error(String(response.status));
                    return response.text();
                })
                .then(function (html) { body.innerHTML = html; })
                .catch(function () {
                    body.replaceChildren();
                    var error = document.createElement("div");
                    error.className = "cash-load-error";
                    error.textContent = "Could not load breakdown.";
                    body.appendChild(error);
                    row.dataset.loaded = "0";
                });
        });

        var search = document.querySelector("[data-cash-store-search]");
        var filterButtons = Array.prototype.slice.call(document.querySelectorAll("[data-cash-store-filter]"));
        var result = document.querySelector("[data-cash-filter-result]");
        var surface = table.closest(".cash-store-surface");
        var activeFilter = "all";

        function closeDetail(row) {
            var button = row.querySelector("[data-cash-store-toggle]");
            var detail = button && document.getElementById(button.getAttribute("aria-controls"));
            if (!button || !detail) return;
            detail.hidden = true;
            row.classList.remove("is-open");
            button.setAttribute("aria-expanded", "false");
            button.setAttribute("aria-label", "Show daily breakdown for " +
                button.querySelector("strong").textContent.trim());
            var chevron = row.querySelector(".chev");
            if (chevron) chevron.classList.remove("is-open");
        }

        function filterMatches(row) {
            if (activeFilter === "attention") return row.dataset.needsAttention === "true";
            if (activeFilter === "in") return row.dataset.hasIn === "true";
            if (activeFilter === "out") return row.dataset.hasOut === "true";
            if (activeFilter === "empty") return row.dataset.hasEntries !== "true";
            return true;
        }

        function applyFilters() {
            var query = search ? search.value.trim().toLocaleLowerCase() : "";
            var visible = 0;
            table.querySelectorAll(".store-row").forEach(function (row) {
                var show = row.dataset.storeName.indexOf(query) !== -1 && filterMatches(row);
                row.hidden = !show;
                if (show) {
                    visible += 1;
                } else {
                    closeDetail(row);
                }
            });
            if (surface) surface.classList.toggle("is-filtered", Boolean(query) || activeFilter !== "all");
            if (result) {
                result.textContent = visible === 0 ? "No stores match these filters" :
                    "Showing " + visible + " of " + table.querySelectorAll(".store-row").length + " stores";
            }
        }

        if (search) search.addEventListener("input", applyFilters);
        filterButtons.forEach(function (button) {
            button.addEventListener("click", function () {
                activeFilter = button.dataset.cashStoreFilter;
                filterButtons.forEach(function (candidate) {
                    var selected = candidate === button;
                    candidate.classList.toggle("is-active", selected);
                    candidate.setAttribute("aria-pressed", String(selected));
                });
                applyFilters();
            });
        });
    }

    function iso(date) {
        return date.getFullYear() + "-" + String(date.getMonth() + 1).padStart(2, "0") +
            "-" + String(date.getDate()).padStart(2, "0");
    }

    document.querySelectorAll(".preset").forEach(function (button) {
        button.addEventListener("click", function () {
            var now = new Date();
            var start;
            var end = now;
            var preset = button.dataset.preset;
            if (preset === "this-month") {
                start = new Date(now.getFullYear(), now.getMonth(), 1);
                end = new Date(now.getFullYear(), now.getMonth() + 1, 0);
            } else if (preset === "last-month") {
                start = new Date(now.getFullYear(), now.getMonth() - 1, 1);
                end = new Date(now.getFullYear(), now.getMonth(), 0);
            } else if (preset === "last-7") {
                start = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6);
            } else if (preset === "this-year") {
                start = new Date(now.getFullYear(), 0, 1);
                end = new Date(now.getFullYear(), 11, 31);
            }
            if (start && config) {
                window.location.assign(config.dataset.baseUrl + "?start=" + iso(start) + "&end=" + iso(end));
            }
        });
    });
})();
