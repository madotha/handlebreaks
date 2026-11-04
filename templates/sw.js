// Service worker, rendered by webui.py at /sw.js. It keeps the static files of this version (their
// URLs carry ?v=) and shows the offline page when the server can't be reached. Pages and data are
// never cached: they always come from the server.
const CACHE = {{ cache|tojson }};
const OFFLINE = {{ offline|tojson }};
const ASSETS = {{ assets|tojson }};
const STATIC = {{ static|tojson }};

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll([OFFLINE, ...ASSETS])).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith("handlebreaks-") && k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
  } else if (url.pathname.startsWith(STATIC) && url.searchParams.has("v")) {
    e.respondWith(caches.open(CACHE).then((c) => c.match(req).then((hit) => hit || fetch(req).then((resp) => {
      if (resp.ok) c.put(req, resp.clone());
      return resp;
    }))));
  }
});
