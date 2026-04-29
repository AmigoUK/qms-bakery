// Register the QMS service worker once the page has loaded so SW install
// doesn't compete with first-paint resources. Quietly no-op when SW isn't
// supported (older mobile browsers, file:// previews, etc.).

(function () {
  if (!('serviceWorker' in navigator)) return;
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function (err) {
      // Registration failure is not fatal — app still works online.
      console.warn('QMS SW registration failed:', err);
    });
  });
})();
