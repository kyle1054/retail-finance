function toggleAll(group, checked) {
    let cls;
    if (group === 'move') cls = '.move-check';
    else if (group === 'term') cls = '.term-check';
    else if (group === 'fuzzy') cls = '.fuzzy-check';
    else if (group === 'addnew') cls = '.addnew-check';
    
    document.querySelectorAll(cls).forEach(cb => cb.checked = checked);
}

document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-toggle-group]');
    if (!button) return;
    toggleAll(button.dataset.toggleGroup, button.dataset.checked === 'true');
});

document.addEventListener('change', (event) => {
    const master = event.target.closest('[data-toggle-group-master]');
    if (!master) return;
    toggleAll(master.dataset.toggleGroupMaster, master.checked);
});
