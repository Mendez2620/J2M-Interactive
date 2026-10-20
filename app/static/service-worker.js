const CACHE = 'j2m-staff-static-v1';
const SAFE_ASSETS = [
  '/static/css/tailwind.min.css',
  '/static/js/web-scanner.js',
  '/static/js/pwa-install.js',
  '/static/vendor/html5-qrcode/html5-qrcode.min.js',
  '/static/pwa/j2m-staff-192.png',
  '/static/pwa/j2m-staff-512.png',
  '/static/icons.svg'
];
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SAFE_ASSETS)).then(() => self.skipWaiting())));
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim())));
self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (SAFE_ASSETS.includes(url.pathname)) {
    event.respondWith(caches.match(request).then(cached => cached || fetch(request)));
    return;
  }
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => new Response('<!doctype html><html lang="es"><meta name="viewport" content="width=device-width"><title>Sin conexión</title><body><main><h1>Sin conexión</h1><p>J2M requiere conexión para consultar clientes o registrar movimientos. No se guardó ninguna operación.</p></main></body></html>', {status:503, headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store'}})));
  }
});
