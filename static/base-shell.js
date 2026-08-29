/* CSP-safe interactions specific to the authenticated admin shell. */
(function () {
  'use strict';

  const wrapper = document.getElementById('datePickerWrapper');
  const yearElement = document.getElementById('pickerYear');
  if (wrapper && yearElement) {
    let pickerYear = Number.parseInt(yearElement.textContent, 10);
    const pageMonth = Number.parseInt(wrapper.dataset.pageMonth || '0', 10);
    const pageYear = Number.parseInt(wrapper.dataset.pageYear || '0', 10);

    wrapper.addEventListener('click', (event) => {
      const yearButton = event.target.closest('[data-year-delta]');
      if (yearButton) {
        pickerYear += Number.parseInt(yearButton.dataset.yearDelta, 10);
        yearElement.textContent = String(pickerYear);
        return;
      }
      const monthLink = event.target.closest('[data-month]');
      if (!monthLink) return;
      event.preventDefault();
      const month = Number.parseInt(monthLink.dataset.month, 10);
      const targetTemplate = window.monthNavTarget || wrapper.dataset.monthTarget;
      if (targetTemplate) {
        window.location.assign(
          targetTemplate.replace('{year}', String(pickerYear)).replace('{month}', String(month))
        );
      }
    });

    wrapper.addEventListener('show.bs.dropdown', () => {
      wrapper.querySelectorAll('.month-pill').forEach((pill) => {
        pill.classList.remove('month-pill-active');
      });
      if (pickerYear === pageYear) {
        const active = wrapper.querySelector(`.month-pill[data-month="${pageMonth}"]`);
        if (active) active.classList.add('month-pill-active');
      }
    });
  }

  window.copyToClipboard = function (text, button) {
    if (!text) return;

    function flashSuccess() {
      window.showToast('Copied to clipboard!', 'success');
      const icon = button && button.querySelector('i');
      if (!icon) return;
      const originalClass = icon.className;
      icon.className = 'bi bi-check2 text-success';
      window.setTimeout(() => { icon.className = originalClass; }, 1500);
    }

    function fallbackCopy() {
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.className = 'clipboard-fallback';
      document.body.appendChild(textarea);
      textarea.select();
      try {
        if (document.execCommand('copy')) {
          flashSuccess();
        } else {
          window.showToast('Failed to copy to clipboard', 'danger');
        }
      } catch (error) {
        console.error('Fallback copy failed:', error);
        window.showToast('Failed to copy to clipboard', 'danger');
      }
      textarea.remove();
    }

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(flashSuccess).catch(fallbackCopy);
    } else {
      fallbackCopy();
    }
  };

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      const search = document.querySelector('input[name="q"], #listSearch');
      if (search && document.activeElement === search) {
        search.value = '';
        search.dispatchEvent(new Event('input'));
        search.blur();
      }
      document.querySelectorAll('.modal.show').forEach((element) => {
        const modal = window.bootstrap && window.bootstrap.Modal.getInstance(element);
        if (modal) modal.hide();
      });
    }

    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      const globalSearch = document.getElementById('globalSearchInput');
      if (globalSearch) {
        event.preventDefault();
        globalSearch.focus();
        globalSearch.select();
      }
      return;
    }

    if (event.key !== '/') return;
    const active = document.activeElement;
    if (active && (
      ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName) || active.isContentEditable
    )) return;
    const search = document.querySelector('input[name="q"], #listSearch')
      || document.getElementById('globalSearchInput');
    if (search) {
      event.preventDefault();
      search.focus();
      search.select();
    }
  });

  const searchBox = document.getElementById('globalSearch');
  const searchInput = document.getElementById('globalSearchInput');
  const searchPanel = document.getElementById('globalSearchResults');
  if (searchBox && searchInput && searchPanel) {
    const groups = [
      { key: 'employees', label: 'People', icon: 'bi-person' },
      { key: 'stores', label: 'Stores', icon: 'bi-shop' },
      { key: 'cards', label: 'Cards', icon: 'bi-credit-card' }
    ];
    let items = [];
    let activeIndex = -1;
    let timer = null;
    let sequence = 0;

    function closeSearch() {
      searchPanel.hidden = true;
      searchInput.setAttribute('aria-expanded', 'false');
      activeIndex = -1;
    }

    function appendResult(row, group) {
      const index = items.length;
      items.push(row);
      const link = document.createElement('a');
      link.className = 'gs-item';
      link.role = 'option';
      link.dataset.idx = String(index);
      link.href = row.url;

      const icon = document.createElement('i');
      icon.className = `bi ${group.icon} gs-item-icon`;
      const text = document.createElement('span');
      text.className = 'gs-item-text';
      const label = document.createElement('span');
      label.className = 'gs-item-label';
      label.textContent = row.label || '';
      text.appendChild(label);
      if (row.sub) {
        const sub = document.createElement('span');
        sub.className = 'gs-item-sub';
        sub.textContent = row.sub;
        text.appendChild(sub);
      }
      link.append(icon, text);
      searchPanel.appendChild(link);
    }

    function renderSearch(data) {
      items = [];
      searchPanel.replaceChildren();
      groups.forEach((group) => {
        const rows = data[group.key] || [];
        if (!rows.length) return;
        const heading = document.createElement('div');
        heading.className = 'gs-group-label';
        heading.textContent = group.label;
        searchPanel.appendChild(heading);
        rows.forEach((row) => appendResult(row, group));
      });
      if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'gs-empty';
        empty.textContent = 'No matches';
        searchPanel.appendChild(empty);
      }
      searchPanel.hidden = false;
      searchInput.setAttribute('aria-expanded', 'true');
      activeIndex = -1;
    }

    function highlight(next) {
      const nodes = searchPanel.querySelectorAll('.gs-item');
      if (!nodes.length) return;
      if (activeIndex >= 0 && nodes[activeIndex]) nodes[activeIndex].classList.remove('is-active');
      activeIndex = (next + nodes.length) % nodes.length;
      nodes[activeIndex].classList.add('is-active');
      nodes[activeIndex].scrollIntoView({ block: 'nearest' });
    }

    function query() {
      const value = searchInput.value.trim();
      if (value.length < 2) {
        closeSearch();
        return;
      }
      const requestSequence = ++sequence;
      window.fetch(`/search?q=${encodeURIComponent(value)}`, {
        headers: { 'X-Requested-With': 'fetch' }
      })
        .then((response) => response.ok ? response.json() : null)
        .then((data) => {
          if (data && requestSequence === sequence) renderSearch(data);
        })
        .catch(() => {});
    }

    searchInput.addEventListener('input', () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(query, 180);
    });
    searchInput.addEventListener('focus', () => {
      if (items.length && searchInput.value.trim().length >= 2) searchPanel.hidden = false;
    });
    searchInput.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        if (searchPanel.hidden) query();
        else highlight(activeIndex + 1);
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        highlight(activeIndex - 1);
      } else if (event.key === 'Enter') {
        const nodes = searchPanel.querySelectorAll('.gs-item');
        if (activeIndex >= 0 && nodes[activeIndex]) {
          event.preventDefault();
          window.location.assign(nodes[activeIndex].href);
        }
      } else if (event.key === 'Escape' && !searchPanel.hidden) {
        event.stopPropagation();
        closeSearch();
      }
    });
    document.addEventListener('click', (event) => {
      if (!searchBox.contains(event.target)) closeSearch();
    });
  }

  document.querySelectorAll('.nav-group-btn').forEach((button) => {
    const target = document.querySelector(button.dataset.bsTarget);
    if (!target) return;
    const chevron = button.querySelector('.nav-chevron');
    target.addEventListener('show.bs.collapse', () => chevron && chevron.classList.add('open'));
    target.addEventListener('hide.bs.collapse', () => chevron && chevron.classList.remove('open'));
  });

  const sidebar = document.querySelector('.sidebar');
  const sidebarToggle = document.getElementById('sidebarToggle');
  const sidebarOverlay = document.getElementById('sidebarOverlay');
  if (sidebar && sidebarToggle && sidebarOverlay) {
    const closeSidebar = () => {
      sidebar.classList.remove('open');
      sidebarOverlay.classList.remove('show');
    };
    sidebarToggle.addEventListener('click', () => {
      sidebar.classList.toggle('open');
      sidebarOverlay.classList.toggle('show');
    });
    sidebarOverlay.addEventListener('click', closeSidebar);
    sidebar.querySelectorAll('.sidebar-link:not(.nav-group-btn)').forEach((link) => {
      link.addEventListener('click', () => {
        if (window.innerWidth <= 820) closeSidebar();
      });
    });
  }
})();
