const SHELL_CACHE = 'ctbrec-shell-v21510';
const OFFLINE_MOSAIC_CACHE = 'ctbrec-offline-mosaics-v272';
const SHELL = [
  '/',
  '/static/index.html',
  '/static/styles.css?v=21510',
  '/static/app.js?v=21510',
  '/static/rapid.js?v=21510',
  '/static/manifest.webmanifest',
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(SHELL_CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter(name => name.startsWith('ctbrec-shell-') && name !== SHELL_CACHE).map(name => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith('/api/offline-media/')) {
    event.respondWith((async () => {
      const cached = await caches.match(request);
      if (cached) return cached;
      const response = await fetch(request);
      if (response.ok) {
        const cache = await caches.open(OFFLINE_MOSAIC_CACHE);
        await cache.put(request, response.clone());
      }
      return response;
    })());
    return;
  }

  if (url.pathname === '/' || url.pathname === '/static/index.html') {
    // HTML is network-first so a server upgrade cannot boot an old app shell
    // against a new API.  Offline still falls back to the cached shell.
    event.respondWith((async () => {
      try {
        const response = await fetch(request, { cache: 'no-store' });
        if (response.ok) (await caches.open(SHELL_CACHE)).put(request, response.clone());
        return response;
      } catch (_) {
        return (await caches.match(request)) || caches.match('/');
      }
    })());
    return;
  }

  if (url.pathname.startsWith('/static/')) {
    event.respondWith((async () => {
      const cached = await caches.match(request);
      if (cached) return cached;
      const response = await fetch(request);
      if (response.ok) (await caches.open(SHELL_CACHE)).put(request, response.clone());
      return response;
    })());
  }
});
