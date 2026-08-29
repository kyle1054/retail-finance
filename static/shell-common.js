/* CSP-safe shared toasts and server-flash hydration for all page shells. */
(function () {
  'use strict';

  const TYPE_ICON = {
    success: 'bi-check-circle-fill',
    danger: 'bi-x-circle-fill',
    error: 'bi-x-circle-fill',
    warning: 'bi-exclamation-triangle-fill',
    info: 'bi-info-circle-fill'
  };

  window.escHtml = function (value) {
    return String(value ?? '').replace(/[&<>"']/g, (character) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    })[character]);
  };

  window.showToast = function (message, type = 'success') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const normalizedType = TYPE_ICON[type] ? type : 'success';
    const toast = document.createElement('div');
    toast.className = `app-toast app-toast-${normalizedType}`;
    toast.setAttribute('role', 'alert');
    toast.setAttribute('aria-live', 'assertive');
    toast.setAttribute('aria-atomic', 'true');

    const icon = document.createElement('i');
    icon.className = `bi ${TYPE_ICON[normalizedType]} app-toast-icon`;
    icon.setAttribute('aria-hidden', 'true');

    const text = document.createElement('span');
    text.textContent = String(message ?? '');

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'app-toast-close';
    close.setAttribute('aria-label', 'Close notification');
    close.textContent = '×';

    toast.append(icon, text, close);
    container.appendChild(toast);
    window.requestAnimationFrame(() => toast.classList.add('show'));
    let removeTimer = null;
    const hideTimer = window.setTimeout(() => {
      toast.classList.remove('show');
      removeTimer = window.setTimeout(() => toast.remove(), 300);
    }, 4000);
    close.addEventListener('click', () => {
      window.clearTimeout(hideTimer);
      if (removeTimer) window.clearTimeout(removeTimer);
      toast.remove();
    });
  };

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-flash-message]').forEach((item) => {
      window.showToast(item.dataset.flashMessage || '', item.dataset.flashCategory || 'info');
      item.remove();
    });
  });
})();
