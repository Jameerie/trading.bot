/* Service worker: makes the app shell available offline and installable.
 *
 * Strategy is deliberately split:
 *   - the shell (HTML/CSS/JS/icons) is cache-first, so the app opens instantly
 *     and still opens with no connection;
 *   - every /api/ call is network-only, never cached.
 *
 * That second rule matters more than it looks. A cached scan would show a setup
 * priced against a market that has since moved, which is worse than showing
 * nothing at all. Stale market data is not a degraded experience, it is a wrong
 * answer, so offline mode deliberately has no signals in it.
 */
'use strict';

const CACHE = 'trading-bot-v1';
const SHELL = [
  '/', '/index.html', '/app.css', '/app.js',
  '/manifest.webmanifest', '/icon.svg', '/icon-maskable.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Never serve market data from cache - see the note at the top of this file.
  if (url.pathname.startsWith('/api/')) return;

  event.respondWith(
    caches.match(request, { ignoreSearch: true }).then((hit) => {
      if (hit) return hit;
      return fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
          }
          return response;
        })
        .catch(() => caches.match('/index.html'));
    })
  );
});
