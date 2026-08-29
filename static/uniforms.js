function filterEmployees(storeSelectId, empSelectId) {
    const store = document.getElementById(storeSelectId).value;
    const empSel = document.getElementById(empSelectId);
    Array.from(empSel.options).forEach(opt => {
        if (!opt.value) return;
        opt.hidden = store ? opt.dataset.store !== store : false;
    });
    empSel.value = '';
}
document.getElementById('uniStoreSelect')?.addEventListener('change', () => {
    filterEmployees('uniStoreSelect', 'uniEmpSelect');
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
        counterLabel.textContent = `${visibleCount} plan${visibleCount !== 1 ? 's' : ''} found`;
    }
});
