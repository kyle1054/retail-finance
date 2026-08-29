/* Invoice Fast-Fill — shared invoice-text parser + the /requests drawer wiring.
 *
 * `parseInvoiceText` was lifted verbatim out of the inline script in
 * templates/employee.html so the staff-request queue can reuse it instead of
 * carrying a second copy: one parser, two front doors. employee.html still owns
 * its own field-filling (it has three different modals to fill); everything
 * below the parser drives the Approve panel on /requests, which is declarative —
 * it finds its fields by name inside the form it lives in, so no page-specific
 * ids and no inline JavaScript (the CSP inventory gate forbids both).
 */

// ── Invoice Text Parser for Uniform & Lay-by ───────────────────────────
// Robust parser that handles inconsistent PDF copy-paste layouts.
// Instead of parsing line-by-line, normalises the full text to a single
// line and uses "Item qty price discount% tax" as a reliable anchor to
// split multi-item invoices even when SKUs get bunched together.
function parseInvoiceText(text) {
    const lines = text.split('\n');
    let saleNumber = '';
    let customerName = '';
    const items = [];
    let discountAmount = 0;
    let totalDue = 0;

    // 1. Invoice / SO number
    const invMatch = text.match(/(INV-\d+|SO-\d+)/i);
    if (invMatch) {
        saleNumber = invMatch[1];
    } else {
        const invNoIndex = lines.findIndex(l => l.trim().toLowerCase().includes('invoice no'));
        if (invNoIndex !== -1 && invNoIndex + 1 < lines.length) {
            saleNumber = lines[invNoIndex + 1].trim();
        }
    }

    // 2. Customer reference (employee name)
    const custIndex = lines.findIndex(l => l.trim().toLowerCase().includes('customer reference'));
    if (custIndex !== -1 && custIndex + 1 < lines.length) {
        customerName = lines[custIndex + 1].trim();
    }

    // 3. Normalise full text to one line so inconsistent PDF line-breaks
    //    don't affect parsing.
    const fullText = text.replace(/\n/g, ' ').replace(/\s+/g, ' ');

    // 4. Locate the start of product data (after the table header)
    let prodStart = 0;
    const hdrMatch = fullText.match(/Amount\s+ZAR|Amount\s*\(ZAR\)/i);
    if (hdrMatch) {
        prodStart = hdrMatch.index + hdrMatch[0].length;
    }
    const productText = fullText.substring(prodStart);

    // 5. Collect every 660200-prefixed SKU in the product section, in order
    const skuRegex = /660200\d{6}/g;
    const allSkus = [];
    let m;
    while ((m = skuRegex.exec(productText)) !== null) {
        allSkus.push(m[0]);
    }

    // 6. Find every "Item qty price discount% tax" occurrence — this is
    //    the one pattern that is always present for every line-item.
    const itemDataRegex = /(?:Item|Each|Unit|Pcs|Pc)\s+(\d+\.?\d*)\s+([\d,]+\.\d{2})\s+(\d+\.?\d*)\s*%\s+([\d,]+\.\d{2})/g;
    const itemMatches = [];
    while ((m = itemDataRegex.exec(productText)) !== null) {
        itemMatches.push({
            index:    m.index,
            endIndex: m.index + m[0].length,
            qty:   parseFloat(m[1]) || 1,
            price: parseFloat(m[2].replace(/,/g, '')) || 0,
            disc:  parseFloat(m[3]) || 0,
            tax:   parseFloat(m[4].replace(/,/g, '')) || 0,
        });
    }

    // 7. Build items — description is the text *before* each "Item" keyword,
    //    after the previous match ended (or after the header).
    for (let i = 0; i < itemMatches.length; i++) {
        const im = itemMatches[i];
        const prevEnd = i === 0 ? 0 : itemMatches[i - 1].endIndex;
        let descSection = productText.substring(prevEnd, im.index).trim();

        // Remove SKU codes (assigned by ordinal position below)
        descSection = descSection.replace(/660200\d{6}/g, '').trim();
        // Remove leading stray amounts left from a previous item row
        descSection = descSection.replace(/^([\d,]+\.\d{2}\s*)+/, '').trim();
        // Tidy whitespace and leading/trailing punctuation
        descSection = descSection.replace(/\s+/g, ' ')
                                 .replace(/^[\s,\-]+/, '')
                                 .replace(/[\s,\-]+$/, '')
                                 .trim();

        // Assign SKU by ordinal position (1st SKU → 1st item, etc.)
        const sku = i < allSkus.length ? allSkus[i] : '';

        items.push({
            sku,
            desc: descSection || 'Item',
            qty:   im.qty,
            price: im.price,
        });
    }

    // 8. Total due — look for "Due" immediately followed by an amount
    //    (this naturally skips "Due Date" since "Date" isn't a number).
    const dueMatch = fullText.match(/\bDue\s+([\d,]+\.\d{2})/);
    if (dueMatch) {
        totalDue = parseFloat(dueMatch[1].replace(/,/g, '')) || 0;
    }
    // Fallback: "Due" on its own line, amount on the next
    if (totalDue === 0) {
        const dueIndex = lines.findIndex(l => l.trim().toLowerCase() === 'due');
        if (dueIndex !== -1 && dueIndex + 1 < lines.length) {
            totalDue = parseFloat(lines[dueIndex + 1].trim().replace(/,/g, '')) || 0;
        }
    }

    return {
        saleNumber,
        customerName,
        items,
        discountAmount,
        totalDue
    };
}

// ── /requests: fill the Approve panel from a pasted invoice ────────────
(function () {
    'use strict';

    var money = function (n) { return 'R ' + n.toFixed(2); };

    /** Same name check employee.html does, but non-blocking: the queue already
     *  shows whose request this is, so a mismatch is a warning line, not an alert
     *  that has to be dismissed before the numbers can be checked. */
    function nameMismatch(parsedName, expected) {
        if (!parsedName || !expected) return false;
        var clean = function (s) { return s.replace(/\s/g, '').toLowerCase(); };
        var a = clean(expected), b = clean(parsedName);
        if (a === b) return false;
        if (b.indexOf(',') !== -1) {                     // "Surname, First"
            var parts = b.split(',');
            if (parts.length === 2 && a === clean(parts[1] + parts[0])) return false;
        }
        return true;
    }

    function field(form, name) { return form.querySelector('[name="' + name + '"]'); }

    /** One editable lay-by basket line: description, qty, shop price. Built with
     *  createElement (never innerHTML) so nothing here can inject markup. */
    function lineRow(index, item) {
        var row = document.createElement('div');
        row.className = 'rq-line';
        var specs = [
            ['item_desc_' + index, 'text', 'Item', item.desc, 'rq-field'],
            ['item_qty_' + index, 'number', 'Qty', item.qty, 'rq-field rq-field-tiny'],
            ['item_price_' + index, 'number', 'Shop price', item.price.toFixed(2), 'rq-field'],
        ];
        specs.forEach(function (spec) {
            var label = document.createElement('label');
            label.className = spec[4];
            var caption = document.createElement('span');
            caption.textContent = spec[2];
            var input = document.createElement('input');
            input.type = spec[1];
            input.name = spec[0];
            input.value = spec[3];
            input.required = true;
            if (spec[1] === 'number') {
                input.min = spec[0].indexOf('qty') !== -1 ? '1' : '0';
                if (spec[0].indexOf('price') !== -1) input.step = '0.01';
            }
            label.appendChild(caption);
            label.appendChild(input);
            row.appendChild(label);
        });
        return row;
    }

    function fill(panel) {
        var form = panel.closest('form');
        var input = panel.querySelector('[data-fastfill-input]');
        var status = panel.querySelector('[data-fastfill-status]');
        var text = (input && input.value) || '';
        if (!text.trim() || !form) return;

        var parsed = parseInvoiceText(text);
        var notes = [];

        if (parsed.saleNumber) {
            var sale = field(form, 'sale_number');
            if (sale) sale.value = parsed.saleNumber;
        }

        var lines = form.querySelector('[data-fastfill-lines]');
        if (lines && parsed.items.length) {
            // Lay-by: the invoice is the truth about what was actually bought, so
            // it replaces the requested basket rather than merging into it.
            lines.textContent = '';
            parsed.items.forEach(function (item, i) { lines.appendChild(lineRow(i, item)); });
            var basket = parsed.items.reduce(function (sum, it) { return sum + it.price * it.qty; }, 0);
            var discount = field(form, 'discount_pct');
            if (discount && basket > 0 && parsed.totalDue > 0) {
                var pct = Math.max(0, Math.min(100, (1 - parsed.totalDue / basket) * 100));
                discount.value = pct.toFixed(1);
                notes.push('discount ' + pct.toFixed(1) + '%');
            }
            notes.unshift(parsed.items.length + (parsed.items.length === 1 ? ' item' : ' items') +
                          ' totalling ' + money(basket));
        } else if (parsed.items.length || parsed.totalDue) {
            // Uniform: one plan, so the invoice's lines become the total + SKUs.
            var total = field(form, 'total_amount');
            if (total && parsed.totalDue > 0) {
                total.value = parsed.totalDue.toFixed(2);
                notes.push('total ' + money(parsed.totalDue));
            }
            var skus = parsed.items.map(function (it) { return it.sku; })
                                   .filter(function (s) { return s; }).join(', ');
            var skuField = field(form, 'sku');
            if (skuField && skus) { skuField.value = skus; notes.push('SKU ' + skus); }
            if (parsed.items.length) {
                notes.unshift(parsed.items.length +
                              (parsed.items.length === 1 ? ' item' : ' items') + ' read');
            }
        }

        if (parsed.saleNumber) notes.push(parsed.saleNumber);
        if (nameMismatch(parsed.customerName, panel.getAttribute('data-employee-name'))) {
            notes.push('⚠ invoice is for "' + parsed.customerName + '" — check this is the right person');
        }
        if (status) {
            status.textContent = notes.length ? 'Filled in: ' + notes.join(' · ')
                                              : 'Nothing recognised — check the numbers by hand.';
        }
    }

    document.addEventListener('click', function (event) {
        var button = event.target.closest('[data-fastfill-go]');
        if (!button) return;
        var panel = button.closest('[data-fastfill]');
        if (!panel) return;
        event.preventDefault();
        fill(panel);
    });
})();
