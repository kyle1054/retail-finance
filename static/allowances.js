document.querySelectorAll('.edit-allocation').forEach(btn => {
    btn.addEventListener('click', function() {
        document.getElementById('allocEmpId').value = this.dataset.empId;
        document.getElementById('allocEmpName').textContent = this.dataset.empName;
        const amount = document.getElementById('allocAmount');
        amount.value = parseFloat(this.dataset.allocated) > 0 ? this.dataset.allocated : '';
        setTimeout(() => amount.focus(), 300);
    });
});
