/* Keep a person's place when a filter or period change reloads the same page.
 * Different routes still open at the top, and browser back/forward keeps its
 * native scroll restoration. Session storage makes the hand-off tab-local. */
(function () {
  'use strict';

  var STORAGE_KEY = 'northwind:pending-scroll';
  var MAX_AGE_MS = 5 * 60 * 1000;

  function readPending() {
    try {
      var value = window.sessionStorage.getItem(STORAGE_KEY);
      return value ? JSON.parse(value) : null;
    } catch (_error) {
      return null;
    }
  }

  function clearPending() {
    try { window.sessionStorage.removeItem(STORAGE_KEY); } catch (_error) { /* storage unavailable */ }
  }

  function cameFromSamePage() {
    if (!document.referrer) return false;
    try {
      var referrer = new URL(document.referrer);
      return referrer.origin === window.location.origin &&
        referrer.pathname === window.location.pathname;
    } catch (_error) {
      return false;
    }
  }

  var pending = readPending();
  var navigation = window.performance && window.performance.getEntriesByType
    ? window.performance.getEntriesByType('navigation')[0]
    : null;
  var isHistoryNavigation = navigation && navigation.type === 'back_forward';
  var isFresh = pending && Date.now() - pending.savedAt <= MAX_AGE_MS;
  var shouldRestore = Boolean(
    pending &&
    isFresh &&
    pending.pathname === window.location.pathname &&
    cameFromSamePage() &&
    !window.location.hash &&
    !isHistoryNavigation
  );

  // A pending position is deliberately one-shot. This prevents an old filter
  // position being reused after the person has visited a different route.
  clearPending();

  if (shouldRestore) {
    if ('scrollRestoration' in window.history) window.history.scrollRestoration = 'manual';
    document.documentElement.classList.add('is-restoring-scroll');

    var restore = function () {
      window.requestAnimationFrame(function () {
        window.requestAnimationFrame(function () {
          var maxY = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
          window.scrollTo(0, Math.min(Math.max(0, Number(pending.y) || 0), maxY));
          document.documentElement.classList.remove('is-restoring-scroll');
          if ('scrollRestoration' in window.history) window.history.scrollRestoration = 'auto';
        });
      });
    };

    // `pageshow` runs after the browser has applied navigation focus and its
    // own scroll handling. Restoring here prevents a newly active filter from
    // pulling the viewport away from the position we just recovered.
    window.addEventListener('pageshow', function () {
      // Let the navigation's focus task finish first; otherwise the focused
      // filter can perform one last scroll after `pageshow`.
      window.setTimeout(restore, 0);
    }, { once: true });
  }

  window.addEventListener('pagehide', function () {
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
        pathname: window.location.pathname,
        y: window.scrollY,
        savedAt: Date.now()
      }));
    } catch (_error) { /* storage unavailable */ }
  });
})();
