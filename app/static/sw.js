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

const CACHE_VERSION = 'qms-v1';
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
    event.respondWith(cacheFirst(request));
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(networkFirstWithOfflineFallback(request));
  }
});

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const fresh = await fetch(request);
    if (fresh.ok) {
      const cache = await caches.open(CACHE_VERSION);
      cache.put(request, fresh.clone());
    }
    return fresh;
  } catch (err) {
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
