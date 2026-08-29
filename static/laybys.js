window.monthNavTarget = document.getElementById('laybysConfig').dataset.monthNavTarget;

function filterEmployees(storeSelId, empSelId) {
    const store = document.getElementById(storeSelId).value;
    const empSel = document.getElementById(empSelId);
    Array.from(empSel.options).forEach(opt => {
        if (!opt.value) return;
        opt.hidden = store ? opt.dataset.store !== store : false;
    });
    empSel.value = '';
}
document.getElementById('lbStoreSelect')?.addEventListener('change', () => {
    filterEmployees('lbStoreSelect', 'lbEmpSelect');
});

// Basket calculator for Add Lay-by modal
let itemCount = 1;
function recalcBasket() {
    let basket = 0;
    document.querySelectorAll('.basket-row').forEach(row => {
        const price = parseFloat(row.querySelector('.item-price').value) || 0;
        const qty   = parseFloat(row.querySelector('.item-qty').value)   || 0;
        const line  = price * qty;
        row.querySelector('.item-line-total').textContent = 'R ' + line.toFixed(2);
        basket += line;
    });
    document.getElementById('basketTotal').textContent = 'R ' + basket.toFixed(2);
    const disc       = parseFloat(document.getElementById('discountPct').value) || 0;
    const discounted = basket * (1 - disc / 100);
    document.getElementById('discountedTotal').textContent = 'R ' + discounted.toFixed(2);
    const months = parseInt(document.getElementById('termMonths').value) || 1;
    document.getElementById('monthlyCalc').textContent = 'R ' + (discounted / months).toFixed(2);
}
document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('basketItems').addEventListener('input', recalcBasket);
    document.getElementById('discountPct').addEventListener('input', recalcBasket);
    document.getElementById('termMonths').addEventListener('change', recalcBasket);
    document.getElementById('addItemRow').addEventListener('click', () => {
        const idx = itemCount++;
        const row = document.createElement('div');
        row.className = 'basket-row row g-1 mb-1 align-items-center';

        const field = (columnClass, inputClass, attributes) => {
            const column = document.createElement('div');
            column.className = columnClass;
            const input = document.createElement('input');
            input.className = `form-control form-control-sm ${inputClass}`.trim();
            Object.entries(attributes).forEach(([name, value]) => {
                if (value === true) input.setAttribute(name, '');
                else input.setAttribute(name, String(value));
            });
            column.appendChild(input);
            return column;
        };
        row.append(
            field('col-5', '', {
                type: 'text', name: `item_desc_${idx}`, placeholder: 'Description', required: true
            }),
            field('col-3', 'item-price', {
                type: 'number', name: `item_price_${idx}`, placeholder: 'Unit price',
                step: '0.01', min: '0', required: true
            }),
            field('col-2', 'item-qty', {
                type: 'number', name: `item_qty_${idx}`, placeholder: 'Qty',
                value: '1', min: '1', required: true
            })
        );
        const total = document.createElement('div');
        total.className = 'col-2 text-end item-line-total basket-line-total';
        total.textContent = 'R 0.00';
        row.appendChild(total);
        document.getElementById('basketItems').appendChild(row);
        row.addEventListener('input', recalcBasket);
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
        counterLabel.textContent = `${visibleCount} plan${visibleCount !== 1 ? 's' : ''} found`;
    }
});
