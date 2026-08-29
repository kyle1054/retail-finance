/* Shared CSRF protection for every authenticated page shell. */
(function () {
  'use strict';

  const meta = document.querySelector('meta[name="csrf-token"]');
  const token = meta ? meta.content : '';
  const originalFetch = window.fetch;

  window.fetch = function (input, init) {
    const options = init || {};
    const method = (options.method || (input && input.method) || 'GET').toUpperCase();
    if (token && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const headers = new Headers(options.headers || {});
      if (!headers.has('X-CSRFToken')) headers.set('X-CSRFToken', token);
      options.headers = headers;
    }
    return originalFetch(input, options);
  };

  function addTokens(root) {
    (root || document).querySelectorAll('form').forEach((form) => {
      if ((form.getAttribute('method') || 'get').toLowerCase() === 'get') return;
      if (form.querySelector('input[name="csrf_token"]')) return;
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'csrf_token';
      input.value = token;
      form.appendChild(input);
    });
  }

  document.addEventListener('DOMContentLoaded', () => addTokens(document));
  window.addCsrfTokens = addTokens;
})();
