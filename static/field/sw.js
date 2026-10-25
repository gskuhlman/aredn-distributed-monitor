/*
 * Field mode service worker (scope /field/ only: the main app is never
 * controlled by it). Network first for everything the field pages fetch from
 * this server; the last good answer is kept and served when the network is
 * gone, marked with an X-Field-Cache header (when it was stored) so the page
 * can say how old the data is. Live alignment readings and offline map tiles
 * (range requests) are never cached.
 */
const CACHE = 'aredn-field-v1';
const SHELL = [
    '/field/', '/field/align',
    '/static/css/style.css', '/static/js/auth.js', '/static/js/channel-map.js',
    '/static/js/field.js', '/static/js/align.js', '/static/field/icon.svg',
    '/static/vendor/leaflet-1.9.4/leaflet.js', '/static/vendor/leaflet-1.9.4/leaflet.css',
    '/static/vendor/protomaps-leaflet-5.1.0.min.js'
];

self.addEventListener('install', (event) => {
    event.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => null).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
    event.waitUntil(caches.keys()
        .then(keys => Promise.all(keys.filter(k => k.startsWith('aredn-field-') && k !== CACHE).map(k => caches.delete(k))))
        .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
    const req = event.request;
    const url = new URL(req.url);
    if (req.method !== 'GET' || url.origin !== self.location.origin) return;
    if (url.pathname.startsWith('/api/align/') || url.pathname.startsWith('/maps/') || req.headers.has('range')) return;
    if (url.pathname.startsWith('/socket.io/')) return;
    event.respondWith((async () => {
        const cache = await caches.open(CACHE);
        try {
            const resp = await fetch(req);
            if (resp.ok && resp.status === 200) {
                const headers = new Headers(resp.headers);
                headers.set('X-Field-Stored', new Date().toLocaleString());
                const body = await resp.clone().blob();
                cache.put(req, new Response(body, { status: 200, statusText: 'OK', headers }));
            }
            return resp;
        } catch (err) {
            const hit = await cache.match(req, { ignoreSearch: url.pathname === '/field/align' });
            if (!hit) throw err;
            const headers = new Headers(hit.headers);
            headers.set('X-Field-Cache', hit.headers.get('X-Field-Stored') || 'earlier');
            return new Response(await hit.blob(), { status: 200, statusText: 'OK (cached)', headers });
        }
    })());
});
