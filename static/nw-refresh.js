document.addEventListener('DOMContentLoaded', () => {
  const sidebar = document.getElementById('appSidebar');
  const menuButton = document.querySelector('[data-mobile-menu]');
  const sidebarToggle = document.getElementById('sidebarToggle');

  if (!sidebar || !menuButton || !sidebarToggle) return;

  const syncMenuState = () => {
    menuButton.setAttribute('aria-expanded', sidebar.classList.contains('open') ? 'true' : 'false');
  };

  menuButton.addEventListener('click', () => {
    sidebarToggle.click();
    syncMenuState();
    if (sidebar.classList.contains('open')) {
      window.requestAnimationFrame(() => sidebar.querySelector('.sidebar-brand')?.focus());
    }
  });

  new MutationObserver(syncMenuState).observe(sidebar, {
    attributes: true,
    attributeFilter: ['class'],
  });
});
