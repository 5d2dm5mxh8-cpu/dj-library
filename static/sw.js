/* Service worker: caches the app shell so the installed PWA opens fast and
   works offline. Network-first for the HTML (so the live API still wins),
   cache-first for static assets. */
const SHELL = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/icon.svg'
];
// Bump the version whenever the shell changes so the old cached HTML is
// purged on activate — a stale frontend keeps serving old bugs forever.
const CACHE = 'djlib-v2';

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k.startsWith('djlib-') && k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  // API calls are never cached — always hit the live server.
  if (url.pathname.startsWith('/api/')) return;
  // HTML goes network-first (falling back to the cached shell offline).
  // Cache-first HTML would pin users to whatever frontend was current at
  // install time — bug fixes would never reach the installed PWA.
  if (e.request.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html') {
    e.respondWith(
      fetch(e.request).then(res => {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      }).catch(() => caches.match(e.request).then(hit => hit || caches.match('/')))
    );
    return;
  }
  // Other static assets are cache-first — they change rarely.
  e.respondWith(
    caches.match(e.request).then(hit => hit || fetch(e.request).then(res => {
      const clone = res.clone();
      caches.open(CACHE).then(c => c.put(e.request, clone));
      return res;
    }))
  );
});
