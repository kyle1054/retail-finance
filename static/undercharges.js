function filterEmployees(storeSelId, empSelId) {
    const store = document.getElementById(storeSelId).value;
    const empSel = document.getElementById(empSelId);
    Array.from(empSel.options).forEach(opt => {
        if (!opt.value) return;
        opt.hidden = store ? opt.dataset.store !== store : false;
    });
    empSel.value = '';
}
function toggleSplitMonths() {
    const isSplit = document.getElementById('pgUcRecovery').value === 'split';
    document.getElementById('pgSplitRow').style.display = isSplit ? '' : 'none';
}
document.querySelectorAll('input[name="type"]').forEach(function(radio) {
    radio.addEventListener('change', function() {
        const isOver = this.value === 'overcharge';
        document.getElementById('pgRecoverySection').style.display = isOver ? 'none' : '';
        document.getElementById('pgOverchargeNote').style.display  = isOver ? ''     : 'none';
        document.getElementById('pgSubmitBtn').textContent           = isOver ? 'Add Overcharge' : 'Add Undercharge';
        document.getElementById('pgSubmitBtn').className             = isOver
            ? 'btn btn-success btn-sm px-4'
            : 'btn btn-primary btn-sm px-4';
    });
});

// Client-side text search
document.getElementById('listSearch')?.addEventListener('input', function() {
    const q = this.value.toLowerCase().trim();
    const rows = document.querySelectorAll('.table tbody tr');
    let visibleCount = 0;
    
    rows.forEach(r => {
        if (r.classList.contains('store-group-row')) return;
        const text = r.textContent.toLowerCase();
        if (text.includes(q)) {
            r.style.display = '';
            visibleCount++;
        } else {
            r.style.display = 'none';
        }
    });
    
    // Hide store group headers that have all sibling rows hidden
    const groups = document.querySelectorAll('.store-group-row');
    groups.forEach(g => {
        let sibling = g.nextElementSibling;
        let visibleSibling = false;
        while (sibling && !sibling.classList.contains('store-group-row')) {
            if (sibling.style.display !== 'none') {
                visibleSibling = true;
            }
            sibling = sibling.nextElementSibling;
        }
        g.style.display = visibleSibling ? '' : 'none';
    });
    
    // Update counter label
    const counterLabel = document.getElementById('plans-count-label');
    if (counterLabel) {
        counterLabel.textContent = `${visibleCount} item${visibleCount !== 1 ? 's' : ''} found`;
    }
});

// ── Edit Undercharge / Overcharge Modal Handlers ─────────────────
function toggleEditSplitMonths() {
    const isSplit = document.getElementById('editPgUcRecovery').value === 'split';
    document.getElementById('editPgSplitRow').style.display = isSplit ? '' : 'none';
}

function toggleEditStatusFields() {
    const status = document.getElementById('editUcStatus').value;
    const isReimburse = (status === 'paid_by_customer' || status === 'reimbursed');
    document.getElementById('editUcReimburseRow').style.display = isReimburse ? '' : 'none';
}

// Update status dropdown dynamically based on type selection in edit modal
function updateEditStatusOptions(type, currentStatus) {
    const statusSelect = document.getElementById('editUcStatus');
    statusSelect.innerHTML = '';
    
    let options = [];
    if (type === 'overcharge') {
        options = [
            { value: 'pending', text: 'Pending' },
            { value: 'accounted_for', text: 'Accounted For' },
            { value: 'written_off', text: 'Written Off' }
        ];
    } else {
        options = [
            { value: 'pending', text: 'Pending' },
            { value: 'partial', text: 'Partial' },
            { value: 'recovered', text: 'Recovered' },
            { value: 'paid_by_customer', text: 'Customer Paid' },
            { value: 'reimbursed', text: 'Reimbursed' },
            { value: 'written_off', text: 'Written Off' }
        ];
    }
    
    options.forEach(opt => {
        const optionEl = document.createElement('option');
        optionEl.value = opt.value;
        optionEl.textContent = opt.text;
        if (opt.value === currentStatus) {
            optionEl.selected = true;
        }
        statusSelect.appendChild(optionEl);
    });
    
    toggleEditStatusFields();
}

// Bind radio button triggers in Edit Modal
document.querySelectorAll('input[name="type"]').forEach(function(radio) {
    if (radio.id.startsWith('editPgType')) {
        radio.addEventListener('change', function() {
            const isOver = this.value === 'overcharge';
            document.getElementById('editPgRecoverySection').style.display = isOver ? 'none' : '';
            document.getElementById('editPgOverchargeNote').style.display  = isOver ? ''     : 'none';
            document.getElementById('editPgSubmitBtn').textContent         = 'Save Changes';
            document.getElementById('editPgSubmitBtn').className           = isOver
                ? 'btn btn-success btn-sm px-4'
                : 'btn btn-primary btn-sm px-4';
                
            // Regenerate status options based on type
            const currentStatus = document.getElementById('editUcStatus').value;
            updateEditStatusOptions(this.value, currentStatus);
        });
    }
});

// Bind edit buttons
document.querySelectorAll('.edit-uc-btn').forEach(btn => {
    btn.addEventListener('click', function() {
        const id = this.dataset.id;
        document.getElementById('editUcForm').action = `/undercharge/${id}/edit`;
        
        document.getElementById('editUcEmployeeName').value = this.dataset.employeeName;
        document.getElementById('editUcReason').value = this.dataset.reason;
        document.getElementById('editUcSale').value = this.dataset.sale;
        document.getElementById('editUcAmount').value = parseFloat(this.dataset.amount).toFixed(2);
        
        document.getElementById('editPgUcRecovery').value = this.dataset.recovery;
        document.getElementById('editUcSplitMonths').value = this.dataset.split;
        document.getElementById('editUcStartMonth').value = this.dataset.startMonth;
        document.getElementById('editUcStartYear').value = this.dataset.startYear;
        
        document.getElementById('editUcIncidentMonth').value = this.dataset.incidentMonth;
        document.getElementById('editUcIncidentYear').value = this.dataset.incidentYear;
        
        document.getElementById('editUcPaymentsMade').value = this.dataset.payments;
        document.getElementById('editUcNotes').value = this.dataset.notes;
        
        // Handle Reimbursement Month/Year
        document.getElementById('editUcReimburseMonth').value = this.dataset.reimburseMonth || '';
        document.getElementById('editUcReimburseYear').value = this.dataset.reimburseYear || '';
        
        // Type radio checks
        const isOver = this.dataset.type === 'overcharge';
        if (isOver) {
            document.getElementById('editPgTypeOver').checked = true;
            document.getElementById('editPgRecoverySection').style.display = 'none';
            document.getElementById('editPgOverchargeNote').style.display  = '';
            document.getElementById('editPgSubmitBtn').className = 'btn btn-success btn-sm px-4';
        } else {
            document.getElementById('editPgTypeUnder').checked = true;
            document.getElementById('editPgRecoverySection').style.display = '';
            document.getElementById('editPgOverchargeNote').style.display  = 'none';
            document.getElementById('editPgSubmitBtn').className = 'btn btn-primary btn-sm px-4';
        }
        
        // Toggle elements visibility
        toggleEditSplitMonths();
        
        // Dynamic status dropdown based on type
        updateEditStatusOptions(this.dataset.type, this.dataset.status);
        
        new bootstrap.Modal(document.getElementById('editUCModal')).show();
    });
});

document.getElementById('ucStoreSelect')?.addEventListener('change', () => {
    filterEmployees('ucStoreSelect', 'ucEmpSelect');
});
document.getElementById('pgUcRecovery')?.addEventListener('change', toggleSplitMonths);
document.getElementById('editPgUcRecovery')?.addEventListener('change', toggleEditSplitMonths);
document.getElementById('editUcStatus')?.addEventListener('change', toggleEditStatusFields);

// ── Ledger-safe remaining-balance reschedule ─────────────────────
const rescheduleState = { remainingCents: 0 };

function addMonth(year, month, offset) {
    const d = new Date(Number(year), Number(month) - 1 + offset, 1);
    return {
        year: d.getFullYear(),
        month: d.toLocaleString('en-ZA', { month: 'short' })
    };
}

function renderReschedulePreview() {
    const months = Number(document.getElementById('rescheduleUcMonths')?.value || 0);
    const year = Number(document.getElementById('rescheduleUcYear')?.value || 0);
    const month = Number(document.getElementById('rescheduleUcMonth')?.value || 0);
    const target = document.getElementById('rescheduleUcPreview');
    if (!target || months < 1 || year < 2020 || month < 1) return;
    const base = Math.floor(rescheduleState.remainingCents / months);
    const remainder = rescheduleState.remainingCents % months;
    const rows = [];
    for (let i = 0; i < months; i += 1) {
        const due = addMonth(year, month, i);
        const cents = base + (i === months - 1 ? remainder : 0);
        rows.push(`${due.month} ${due.year}: R ${(cents / 100).toFixed(2)}`);
    }
    target.textContent = rows.join(' · ');
}

document.querySelectorAll('.reschedule-uc-btn').forEach(btn => {
    btn.addEventListener('click', function() {
        rescheduleState.remainingCents = Math.round(Number(this.dataset.remaining) * 100);
        document.getElementById('rescheduleUcForm').action =
            `/undercharge/${this.dataset.id}/reschedule`;
        document.getElementById('rescheduleUcEmployee').textContent = this.dataset.employeeName;
        document.getElementById('rescheduleUcRemaining').textContent =
            (rescheduleState.remainingCents / 100).toFixed(2);
        document.getElementById('rescheduleUcMonth').value = this.dataset.startMonth;
        document.getElementById('rescheduleUcYear').value = this.dataset.startYear;
        document.getElementById('rescheduleUcMonths').value = this.dataset.months || 1;
        renderReschedulePreview();
        new bootstrap.Modal(document.getElementById('rescheduleUCModal')).show();
    });
});

document.getElementById('rescheduleUcMonths')?.addEventListener('input', renderReschedulePreview);
document.getElementById('rescheduleUcMonth')?.addEventListener('change', renderReschedulePreview);
document.getElementById('rescheduleUcYear')?.addEventListener('input', renderReschedulePreview);
