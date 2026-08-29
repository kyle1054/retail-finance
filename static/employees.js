(function () {
    let lastCol = -1, asc = true;
    document.querySelectorAll('#empTable th.sortable').forEach(th => {
        th.addEventListener('click', function () {
            const col = parseInt(this.dataset.col);
            asc = (col === lastCol) ? !asc : true;
            lastCol = col;
            const tbody = document.querySelector('#empTable tbody');
            const rows = Array.from(tbody.querySelectorAll('tr'));
            rows.sort((a, b) => {
                const av = a.cells[col]?.textContent.trim() || '';
                const bv = b.cells[col]?.textContent.trim() || '';
                if (col === 5) {
                    const an = parseFloat(av.replace(/[^0-9.]/g, '')) || 0;
                    const bn = parseFloat(bv.replace(/[^0-9.]/g, '')) || 0;
                    return asc ? an - bn : bn - an;
                }
                return asc ? av.localeCompare(bv) : bv.localeCompare(av);
            });
            rows.forEach(r => tbody.appendChild(r));
            document.querySelectorAll('#empTable th.sortable i').forEach(i => {
                i.className = 'bi bi-chevron-expand ms-1'; i.style.fontSize = '10px';
            });
            const icon = this.querySelector('i');
            icon.className = asc ? 'bi bi-chevron-up ms-1' : 'bi bi-chevron-down ms-1';
        });
    });

    // Client-side instant filter
    const searchInput = document.querySelector('input[name="q"]');
    if (searchInput) {
        const form = searchInput.closest('form');
        if (form) {
            form.addEventListener('submit', (e) => {
                // If it's a client-side search, don't submit the form to the server
                e.preventDefault();
            });
        }
        searchInput.addEventListener('input', function() {
            const query = this.value.toLowerCase().trim();
            const rows = document.querySelectorAll('#empTable tbody tr');
            let visibleCount = 0;
            rows.forEach(row => {
                if (row.cells.length < 2) return; // Skip "No employees found" row
                const text = row.textContent.toLowerCase();
                if (text.includes(query)) {
                    row.style.display = '';
                    visibleCount++;
                } else {
                    row.style.display = 'none';
                }
            });
            
            // Update counter label
            const countHeader = document.querySelector('.card-header span');
            if (countHeader) {
                countHeader.textContent = `${visibleCount} employee${visibleCount !== 1 ? 's' : ''} found`;
            }
        });
    }
})();
