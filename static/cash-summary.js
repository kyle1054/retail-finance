(function () {
    "use strict";
    function openDay(row) {
        window.location.assign(row.dataset.cashDayUrl);
    }
    document.addEventListener("click", function (event) {
        var row = event.target.closest("[data-cash-day-url]");
        if (!row || event.target.closest("a, button")) return;
        openDay(row);
    });
    document.addEventListener("keydown", function (event) {
        var row = event.target.closest("[data-cash-day-url]");
        if (!row || (event.key !== "Enter" && event.key !== " ")) return;
        event.preventDefault();
        openDay(row);
    });
})();
