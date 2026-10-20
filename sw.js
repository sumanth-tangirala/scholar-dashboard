/* Scholar Dashboard service worker: the app opens without a connection.

   Online nothing changes: the page, the data and the link-preview pages come from the network as before, and each
   answer is kept. Offline, or when the network hasn't answered within a few seconds (a plane's Wi-Fi), the kept copy
   answers instead. The libraries the page loads from CDNs (PapaParse, PDF.js, the fonts, Markdown and maths for
   notes) have versioned addresses, so they're kept on first use and served from here after that; the page also lists
   the ones it only loads later (sw-warm), so the reader works offline before it has ever been opened.

   Left alone, always: GitHub's API and the encrypted notes (annotations/), the chat bridge, and PDFs (downloaded ones
   live in the page's own IndexedDB, see OFFLINE in index.html). A kept copy is only ever a real answer the network
   gave; this never makes up a "not found", which the notes would read as "deleted". */
const VERSION = 1;
const PAGES = 'scholar-pages-v' + VERSION, LIBS = 'scholar-libs-v' + VERSION;
const ROOT = new URL('./', self.location).href;   // where index.html lives
const NET_WAIT = 4000;
const CDN = /^https:\/\/(cdnjs\.cloudflare\.com|cdn\.jsdelivr\.net|fonts\.googleapis\.com|fonts\.gstatic\.com)\//;
const DATA = /^https:\/\/raw\.githubusercontent\.com\/[^/]+\/[^/]+\/[^/]+\/(papers|interests|groups)_database\.csv$/;

/* Kept from the start: the app, its icons and the data, so even a first visit opens offline next time */
self.addEventListener('install', e => {
  self.skipWaiting();
  e.waitUntil(caches.open(PAGES).then(c => Promise.all(['', 'manifest.webmanifest', 'icon.svg', 'icon-192.png', 'icon-512.png', 'apple-touch-icon.png',
    'papers_database.csv', 'interests_database.csv', 'groups_database.csv'].map(u => c.add(ROOT + u).catch(() => {})))));
});
self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k.startsWith('scholar-') && k !== PAGES && k !== LIBS) await caches.delete(k);
    await self.clients.claim();
  })());
});

/* The page lists the library files it uses or will use (sw-warm): keep any not kept yet. A Google Fonts stylesheet
   is read for the font files it names (the page never asks for them by name) */
self.addEventListener('message', e => {
  const d = e.data || {};
  if (d.type !== 'sw-warm' || !Array.isArray(d.urls)) return;
  e.waitUntil(caches.open(LIBS).then(async c => {
    const keep = async u => {
      if (typeof u !== 'string' || !CDN.test(u)) return;
      const css = /^https:\/\/fonts\.googleapis\.com\//.test(u), kept = await c.match(u);
      if (kept && !(css && kept.type === 'opaque')) return css ? (await kept.text()).match(/https:\/\/fonts\.gstatic\.com\/[^)'"\s]+/g) || [] : [];
      try {
        const r = await fetch(u, { mode: 'cors', credentials: 'omit' });
        if (!r.ok) return [];
        await c.put(u, r.clone());
        return css ? (await r.text()).match(/https:\/\/fonts\.gstatic\.com\/[^)'"\s]+/g) || [] : [];
      } catch (err) { return []; }
    };
    for (const u of d.urls) for (const f of (await keep(u)) || []) await keep(f);
  }));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = req.url.split('#')[0], bare = url.split('?')[0];
  if (url.startsWith(ROOT)) {
    const rel = bare.slice(ROOT.length);
    if (rel.startsWith('annotations/') || rel === 'sw.js') return;
    if (req.mode === 'navigate') e.respondWith(openPage(e, rel));
    else e.respondWith(networkFirst(e, req, ROOT + rel));
    return;
  }
  if (DATA.test(bare)) { e.respondWith(networkFirst(e, req, bare)); return; }
  if (CDN.test(url)) e.respondWith(keptFirst(req));
});

/* Fetch, keeping a good answer under key (the worker stays alive until it's kept, even if the page got the kept copy) */
function fetchKeeping(e, req, key) {
  let keep = null;
  const net = fetch(req).then(r => {
    if (r.ok && (r.type === 'basic' || r.type === 'cors')) { const copy = r.clone(); keep = caches.open(PAGES).then(c => c.put(key, copy)); }
    return r;
  });
  e.waitUntil(net.then(() => keep).catch(() => {}));
  return net;
}
const inTime = p => Promise.race([p, new Promise((_, no) => setTimeout(() => no(new Error('slow network')), NET_WAIT))]);
/* The network's answer when it comes in time; else the kept copy; else the network's answer whenever it comes */
async function networkFirst(e, req, key) {
  const net = fetchKeeping(e, req, key);
  try { return await inTime(net); }
  catch (err) { return (await caches.match(key, { cacheName: PAGES })) || net; }
}
/* Opening a page: the app (its root), or a paper's link-preview page p/<id>/ */
async function openPage(e, rel) {
  const key = rel === 'index.html' ? ROOT : ROOT + rel;
  const net = fetchKeeping(e, e.request, key);
  try { return await inTime(net); }
  catch (err) {
    const kept = await caches.match(key, { cacheName: PAGES });
    if (kept) return kept;
    const m = rel.match(/^p\/([a-z0-9][a-z0-9-]*)\/(index\.html)?$/);
    if (m) {   // a paper's preview page never opened on this device: forward to the app, as that page does
      return new Response(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><script>location.replace('../../' + (location.hash || '#paper/${m[1]}'));</script>`,
        { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
    }
    return net;
  }
}
/* Library files (their addresses carry a version): the kept copy, else the network, kept for next time. An opaque copy
   (from a tag without crossorigin) is never handed to a request that needs to read the file */
async function keptFirst(req) {
  const cache = await caches.open(LIBS);
  const k = await cache.match(req.url);
  if (k && !(k.type === 'opaque' && req.mode === 'cors')) return k;
  const r = await fetch(req);
  if (r.ok || (r.type === 'opaque' && !k)) cache.put(req.url, r.clone()).catch(() => {});
  return r;
}
