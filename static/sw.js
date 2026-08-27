/* Service worker: caches the app shell so the installed PWA opens fast and
   works offline. Network-first for the HTML (so the live API still wins),
   cache-first for static assets. */
const SHELL = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/icon.svg'
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open('djlib-v1').then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k.startsWith('djlib-') && k !== 'djlib-v1').map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  // API calls are never cached — always hit the live server.
  if (url.pathname.startsWith('/api/')) return;
  e.respondWith(
    caches.match(e.request).then(hit => hit || fetch(e.request).then(res => {
      const clone = res.clone();
      caches.open('djlib-v1').then(c => c.put(e.request, clone));
      return res;
    }).catch(() => caches.match('/'))
  ));
});
