(function () {
    "use strict";

    var config = document.getElementById("payrollReconcileConfig");
    var currentYear = config ? config.dataset.year : "";
    var currentMonth = config ? config.dataset.month : "";
    var currentStoreName = "";
    var placeholder = document.getElementById("placeholder-view");
    var loading = document.getElementById("loading-view");
    var content = document.getElementById("content-view");
    var listWrapper = document.getElementById("employee-list-wrapper");
    var bulkButton = document.getElementById("bulk-tick-btn");

    function escapeHtml(value) {
        var element = document.createElement("div");
        element.textContent = value == null ? "" : String(value);
        return element.innerHTML;
    }

    function money(value) {
        return Number(value).toFixed(2);
    }

    function setButtonContent(button, loadingState, bulk) {
        button.replaceChildren();
        var icon = document.createElement("span");
        if (loadingState) {
            icon.className = "spinner-border spinner-border-sm" + (bulk ? " me-2" : "");
            icon.setAttribute("role", "status");
        } else {
            icon.className = bulk ? "bi bi-check-all payroll-bulk-icon" : "bi bi-check-lg";
        }
        button.appendChild(icon);
        if (bulk) button.appendChild(document.createTextNode(loadingState ? "Ticking..." : "Bulk Tick Store"));
    }

    function planHtml(type, label, description, metadata, amount, id) {
        var tone = type === "uniform" ? "amber" : type === "layby" ? "teal" : "red";
        var outline = type === "uniform" ? "warning" : type === "layby" ? "teal" : "danger";
        return `
            <div class="d-flex align-items-center justify-content-between py-2 border-bottom border-light plan-row"
                 data-plan-type="${type}" data-plan-id="${id}">
                <div>
                    <span class="badge rounded-pill bg-${tone}-soft text-${tone} me-2 reconcile-plan-badge">${label}</span>
                    <span class="fw-semibold text-light">${escapeHtml(description)}</span>
                    <div class="text-muted small reconcile-plan-meta">${escapeHtml(metadata)}</div>
                </div>
                <div class="d-flex align-items-center gap-3">
                    <span class="fw-bold text-${tone}">R ${money(amount)}</span>
                    <button type="button"
                            class="btn btn-sm btn-outline-${outline} text-${outline} rounded-circle px-2 py-1 flex-shrink-0 reconcile-tick-button"
                            data-tick-plan data-plan-type="${type}" data-plan-id="${id}"
                            aria-label="Tick ${escapeHtml(label)} deduction">
                        <i class="bi bi-check-lg"></i>
                    </button>
                </div>
            </div>`;
    }

    function renderEmployees(employees) {
        listWrapper.replaceChildren();
        bulkButton.disabled = employees.length === 0;
        if (!employees.length) {
            var empty = document.createElement("div");
            empty.className = "text-center py-5 text-muted";
            empty.innerHTML = '<i class="bi bi-clipboard-check text-success reconcile-empty-icon"></i><p class="mb-0">All employees ticked for this store!</p>';
            listWrapper.appendChild(empty);
            return;
        }

        employees.forEach(function (entry) {
            var employee = entry.employee;
            var plans = "";
            entry.uniform_plans.forEach(function (plan) {
                plans += planHtml(
                    "uniform", "Uniform", plan.description || "Staff Uniform",
                    `Payments: ${plan.payments_made}/${plan.term_months} · Monthly: R ${money(plan.monthly_amount)}`,
                    plan.monthly_amount, plan.id
                );
            });
            entry.layby_plans.forEach(function (plan) {
                plans += planHtml(
                    "layby", "Lay-by", plan.description || "Lay-by",
                    `Payments: ${plan.payments_made}/${plan.term_months} · Monthly: R ${money(plan.monthly_amount)}`,
                    plan.monthly_amount, plan.id
                );
            });
            entry.undercharge_rows.forEach(function (undercharge) {
                var amount = undercharge.recovery_method === "full"
                    ? undercharge.total_amount
                    : undercharge.total_amount / undercharge.split_months;
                plans += planHtml(
                    "undercharge", "Undercharge", undercharge.reason || "Undercharge Incident",
                    `Method: ${undercharge.recovery_method} · Payments: ${undercharge.payments_made}/${undercharge.split_months || 1}`,
                    amount, undercharge.id
                );
            });

            var card = document.createElement("div");
            card.className = "card mb-3 p-3 reconcile-employee-card";
            card.innerHTML = `
                <div class="d-flex align-items-center justify-content-between border-bottom border-light pb-2 mb-2">
                    <div>
                        <a href="/employees/${encodeURIComponent(employee.id)}"
                           class="fw-bold text-light text-decoration-none reconcile-employee-name">${escapeHtml(employee.full_name)}</a>
                        <span class="text-muted ms-2 reconcile-employee-id">${escapeHtml(employee.id)}</span>
                    </div>
                    <span class="fw-bold text-primary reconcile-employee-total">Total R ${money(entry.total)}</span>
                </div>
                <div class="plans-list">${plans}</div>`;
            listWrapper.appendChild(card);
        });
    }

    function selectedStoreItem() {
        return document.querySelector(`.store-list-item[data-store-name="${CSS.escape(currentStoreName)}"]`);
    }

    function selectStore(storeName, element) {
        currentStoreName = storeName;
        document.querySelectorAll(".store-list-item").forEach(function (item) {
            item.classList.toggle("is-selected", item === element);
        });
        placeholder.classList.add("d-none");
        content.classList.add("d-none");
        loading.classList.remove("d-none");

        fetch(`/api/payroll/reconcile/store/${encodeURIComponent(storeName)}?year=${currentYear}&month=${currentMonth}`)
            .then(function (response) {
                if (!response.ok) throw new Error("Failed to fetch data");
                return response.json();
            })
            .then(function (data) {
                loading.classList.add("d-none");
                if (!data.success) {
                    window.showToast(data.message || "Error loading store", "danger");
                    placeholder.classList.remove("d-none");
                    return;
                }
                document.getElementById("selected-store-title").textContent = data.store;
                document.getElementById("selected-store-emp-count").textContent = String(data.employees.length);
                renderEmployees(data.employees);
                content.classList.remove("d-none");
            })
            .catch(function () {
                loading.classList.add("d-none");
                placeholder.classList.remove("d-none");
                window.showToast("Failed to load store records.", "danger");
            });
    }

    function updateStoreBadges() {
        fetch(`/api/payroll/reconcile/stores-summary?year=${currentYear}&month=${currentMonth}`)
            .then(function (response) { return response.json(); })
            .then(function (data) {
                if (!data.success) return;
                data.summaries.forEach(function (summary) {
                    var badge = document.getElementById("badge-count-" + summary.name.replace(/\s+/g, "-"));
                    if (badge) {
                        badge.className = summary.pending_count === 0
                            ? "badge rounded-pill bg-success text-white fw-bold px-2.5 py-1"
                            : "badge rounded-pill bg-warning text-dark fw-bold px-2.5 py-1";
                        badge.textContent = summary.pending_count === 0 ? "✓" : String(summary.pending_count);
                    }
                    var item = document.querySelector(`.store-list-item[data-store-name="${CSS.escape(summary.name)}"]`);
                    var amount = item && item.querySelector(".store-pending-amount");
                    if (amount) amount.textContent = money(summary.pending_amount);
                });
            });
    }

    function tickPlan(button) {
        button.disabled = true;
        setButtonContent(button, true, false);
        fetch(`/${button.dataset.planType}/${button.dataset.planId}/tick`, {
            method: "POST",
            headers: {Accept: "application/json"}
        })
            .then(function (response) {
                if (!response.ok) throw new Error("Ticking failed");
                return response.json();
            })
            .then(function (data) {
                if (!data.success) {
                    window.showToast(data.message || "Ticking failed", "danger");
                    button.disabled = false;
                    setButtonContent(button, false, false);
                    return;
                }
                window.showToast("Deduction successfully reconciled!", "success");
                selectStore(currentStoreName, selectedStoreItem());
                updateStoreBadges();
            })
            .catch(function () {
                window.showToast("Connection error, ticking failed.", "danger");
                button.disabled = false;
                setButtonContent(button, false, false);
            });
    }

    function bulkTick() {
        if (!currentStoreName ||
            !window.confirm(`Reconcile and tick ALL pending deductions for ${currentStoreName}?`)) return;
        bulkButton.disabled = true;
        setButtonContent(bulkButton, true, true);
        var formData = new FormData();
        formData.append("year", currentYear);
        formData.append("month", currentMonth);
        fetch(`/api/payroll/reconcile/store/${encodeURIComponent(currentStoreName)}/bulk-tick`, {
            method: "POST",
            body: formData
        })
            .then(function (response) {
                if (!response.ok) throw new Error("Bulk ticking failed");
                return response.json();
            })
            .then(function (data) {
                bulkButton.disabled = false;
                setButtonContent(bulkButton, false, true);
                if (!data.success) {
                    window.showToast(data.message || "Bulk ticking failed", "danger");
                    return;
                }
                window.showToast(
                    `Reconciled ${data.ticked_count} plan(s) for ${currentStoreName} successfully!`,
                    "success"
                );
                selectStore(currentStoreName, selectedStoreItem());
                updateStoreBadges();
            })
            .catch(function () {
                bulkButton.disabled = false;
                setButtonContent(bulkButton, false, true);
                window.showToast("Connection error, bulk reconcile failed.", "danger");
            });
    }

    document.addEventListener("click", function (event) {
        var store = event.target.closest(".store-list-item");
        if (store) {
            selectStore(store.dataset.storeName, store);
            return;
        }
        var tickButton = event.target.closest("[data-tick-plan]");
        if (tickButton) tickPlan(tickButton);
    });
    if (bulkButton) bulkButton.addEventListener("click", bulkTick);

    var firstStore = document.querySelector(".store-list-item");
    if (firstStore) selectStore(firstStore.dataset.storeName, firstStore);
})();
