const ta = document.getElementById('employeeList');
const lc = document.getElementById('lineCount');
const clearButton = document.getElementById('clearEmployeeList');
ta.addEventListener('input', () => {
    const n = ta.value.split('\n').filter(l => l.trim()).length;
    lc.textContent = n + ' employee' + (n !== 1 ? 's' : '');
});
if (clearButton) {
    clearButton.addEventListener('click', () => {
        ta.value = '';
        lc.textContent = '0 employees';
        ta.focus();
    });
}
