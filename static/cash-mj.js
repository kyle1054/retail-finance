(function () {
    "use strict";
    var vatRate = 0.15;
    var table = document.getElementById("mjTable");
    if (!table) return;

    function money(cents) {
        return "R " + (cents / 100).toFixed(2);
    }

    function recalculate() {
        var grossCents = 0;
        var netCents = 0;
        var vatCents = 0;
        document.querySelectorAll(".mj-row").forEach(function (row) {
            var included = row.querySelector(".mj-include").checked;
            var gross = Math.round((parseFloat(row.querySelector(".mj-gross").value) || 0) * 100);
            var vatType = row.querySelector(".mj-vat").value;
            var net = vatType === "standard" ? Math.floor(gross / (1 + vatRate) + 0.5) : gross;
            var vat = vatType === "standard" ? Math.floor(net * vatRate + 0.5) : 0;
            row.querySelector(".mj-net").textContent = money(net);
            row.classList.toggle("is-excluded", !included);
            if (included) {
                grossCents += gross;
                netCents += net;
                vatCents += vat;
            }
        });

        document.getElementById("grossTotal").textContent = money(grossCents);
        document.getElementById("netTotal").textContent = money(netCents);
        document.getElementById("vatTotal").textContent = money(vatCents);
        var debit = netCents + vatCents;
        var rounding = debit - grossCents;
        document.getElementById("contraCell").textContent = "Cr " + money(grossCents);
        var roundingCell = document.getElementById("roundingCell");
        roundingCell.classList.remove("rounding-credit", "rounding-debit", "rounding-zero");
        if (rounding > 0) {
            roundingCell.textContent = "Cr " + money(rounding);
            roundingCell.classList.add("rounding-credit");
        } else if (rounding < 0) {
            roundingCell.textContent = "Dr " + money(-rounding);
            roundingCell.classList.add("rounding-debit");
        } else {
            roundingCell.textContent = "—";
            roundingCell.classList.add("rounding-zero");
        }

        var credit = grossCents + (rounding > 0 ? rounding : 0);
        var debitSide = debit + (rounding < 0 ? -rounding : 0);
        document.getElementById("balanceCheck").textContent =
            "Balances ✓  ·  Dr " + money(debitSide) + " = Cr " + money(credit);
    }

    table.addEventListener("input", recalculate);
    table.addEventListener("change", recalculate);
    recalculate();
})();
