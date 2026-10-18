/* Service worker KhairyPustaka (PWA) — mulai 2026-10-18.
   Strategi aman buat SPA + API:
   - Navigasi (HTML): network-first, fallback cache (app shell) saat offline.
   - Aset statis same-origin (js/css/gambar/font): stale-while-revalidate.
   - API & request non-GET: TIDAK disentuh (selalu jaringan).
   Naikkan versi CACHE tiap rilis yang mengubah strategi ini. */
const CACHE = "kp-pwa-v1";
const SHELL = ["/", "/index.html", "/manifest.webmanifest",
               "/icon-192.png", "/icon-512.png", "/maskot.jpg"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;
  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req)
        .then(res => {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put("/index.html", copy));
          return res;
        })
        .catch(() => caches.match("/index.html"))
    );
    return;
  }
  e.respondWith(
    caches.match(req).then(hit => {
      const net = fetch(req).then(res => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy));
        }
        return res;
      }).catch(() => hit);
      return hit || net;
    })
  );
});
