/* QMS service worker — offline-first app shell.
 *
 * Strategy:
 *   - Pre-cache the app shell (CSS, registration script, manifest, offline
 *     fallback) on install. Versioned cache name so a new SW build clears
 *     stale assets cleanly.
 *   - Static assets under /static/  → cache-first, fall back to network,
 *     then to whatever's already cached if the network fails.
 *   - Navigation requests (HTML)    → network-first; on failure fall back
 *     to /offline so the operator sees a friendly screen instead of a
 *     browser dinosaur.
 *   - Mutating requests (POST/PUT/DELETE) bypass the cache entirely; we
 *     don't pretend writes succeeded. Background sync / IndexedDB queue
 *     is a separate, larger feature.
 *
 * Bump CACHE_VERSION when shipping a new app shell so old caches get
 * cleaned up in `activate`.
 */

// Bumped after shipping the study/flash-card CSS — old caches still
// hold an app.css without .flashcard* rules, which breaks the answer
// hide/show toggle on the practice page.
// v3: per-card [Next] button added; .flashcard-actions layout rules
// need to ship with the new study.js wiring.
// v4: switched /static/* to network-first so dev/test iterations land
// without forcing operators to do double-refreshes.
// v5: fixed .btn-ghost contrast — was white-on-white on every light
// card; now defaults to dark-on-light with a topbar override.
// v6: exam page got a "back to practice" link + .exam-actions row.
// v7: training compliance matrix — new .compliance-* + .matrix-* CSS,
// plus the dashboard template rewrite. Bump so old caches let go.
// v8: trainee email/channel + matrix filters + drill-down + CSV/PDF
// exports — fresh CSS for .compliance-link + .matrix-exports rows.
// v9: matrix multi-select filters + issue-now button on drill-down +
// auto-landscape PDF.
// v10: trainees list — multi-select filters + checkbox bulk-issue
// magic-links + sticky bulk-bar + new trainees_bulk.js.
// v11: /help blueprint + topbar help link + 410-page reason branching
// + 6 silent-failure copy keys + .help-page CSS.
const CACHE_VERSION = 'qms-v11';
const APP_SHELL = [
  '/offline',
  '/static/css/app.css',
  '/static/js/sw-register.js',
  '/manifest.webmanifest',
  '/static/icons/qms-icon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(APP_SHELL))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const { request } = event;

  if (request.method !== 'GET') {
    return; // let the network handle writes
  }

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return; // don't intercept cross-origin (CDN, telemetry, etc.)
  }

  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.webmanifest') {
    // Dev/test iteration matters more than the offline-first speed
    // win — operators see CSS / JS changes on the next normal reload
    // instead of waiting for a cache-version bump. Cache stays warm
    // as a fallback when the network actually fails.
    event.respondWith(networkFirstStaticAsset(request));
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(networkFirstWithOfflineFallback(request));
  }
});

async function networkFirstStaticAsset(request) {
  try {
    const fresh = await fetch(request);
    if (fresh.ok) {
      const cache = await caches.open(CACHE_VERSION);
      cache.put(request, fresh.clone());
    }
    return fresh;
  } catch (err) {
    const cached = await caches.match(request);
    return cached || Response.error();
  }
}

async function networkFirstWithOfflineFallback(request) {
  try {
    const fresh = await fetch(request);
    return fresh;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    const offline = await caches.match('/offline');
    return offline || new Response('offline', { status: 503 });
  }
}
