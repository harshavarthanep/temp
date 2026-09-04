/* ================================================================
   ZenPDF Studio — service worker
   Upload this file NEXT TO index.html (same folder) in your repo.
   Bump CACHE_VERSION whenever you ship a change you want pushed
   out aggressively (not required — HTML is always network-first).
   ================================================================ */
const CACHE_VERSION = 'zenpdf-v1';
const APP_SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './zenpdf-icon-192.png',
  './zenpdf-icon-512.png',
];

/* big engines are deliberately NOT cached — Pyodide alone is ~45 MB
   and the browser already caches it normally */
function isTooBig(url) {
  return /pyodide|tesseract|\.wasm($|\?)|qpdf/i.test(url);
}
function isCdn(url) {
  return /^https:\/\/(cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com|unpkg\.com|fonts\.googleapis\.com|fonts\.gstatic\.com)\//.test(url);
}
function cacheable(res) {
  return res && (res.ok || res.type === 'opaque');
}

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_VERSION)
      .then(c => Promise.allSettled(APP_SHELL.map(u => c.add(u))))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('message', (e) => {
  if (e.data === 'skip-waiting') self.skipWaiting();
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = req.url;
  if (!/^https?:/.test(url)) return;
  if (isTooBig(url)) return;                       // straight to the network

  // 1) the page itself — network first, so a new deploy is picked up
  //    immediately; the cached copy is only used when offline
  if (req.mode === 'navigate' || req.destination === 'document') {
    e.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        if (cacheable(fresh)) {
          const c = await caches.open(CACHE_VERSION);
          c.put('./index.html', fresh.clone()).catch(() => {});
        }
        return fresh;
      } catch (err) {
        return (await caches.match(req)) ||
               (await caches.match('./index.html')) ||
               (await caches.match('./')) ||
               new Response('<h1>Offline</h1><p>Open ZenPDF once while online to make it available offline.</p>',
                 { headers: { 'Content-Type': 'text/html' }, status: 503 });
      }
    })());
    return;
  }

  // 2) our own icons / manifest — cache first, refresh in the background
  const sameOrigin = url.startsWith(self.location.origin);
  if (sameOrigin || isCdn(url)) {
    e.respondWith((async () => {
      const cached = await caches.match(req);
      const network = fetch(req).then(res => {
        if (cacheable(res)) {
          caches.open(CACHE_VERSION).then(c => c.put(req, res.clone())).catch(() => {});
        }
        return res;
      }).catch(() => null);
      return cached || (await network) || Response.error();
    })());
  }
});
