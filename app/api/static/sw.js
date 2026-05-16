// Minimal service worker: precache shell assets and stale-while-revalidate for
// the HTML shell and JS bundle so deploys refresh installed PWAs. Icons and
// manifest stay cache-first (they change rarely).

const VERSION = "domesti-bot-pwa-v16";

const PRECACHE = [
  "/",
  "/static/manifest.webmanifest",
  "/static/icons/app-icon-192x192.png",
  "/static/icons/app-icon-512x512.png",
  "/static/icons/app-icon.svg",
];

/** Paths that must pick up server updates after deploy (inline CSS lives in ``/``). */
const STALE_WHILE_REVALIDATE_PATHS = new Set(["/", "/static/dist/main.js"]);

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) => cache.addAll(PRECACHE)),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((key) => {
          if (key !== VERSION) {
            return caches.delete(key);
          }
          return undefined;
        }),
      ),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") {
    return;
  }
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  if (STALE_WHILE_REVALIDATE_PATHS.has(url.pathname)) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }

  event.respondWith(
    caches.match(request).then((hit) => hit ?? fetch(request)),
  );
});

function staleWhileRevalidate(request) {
  return caches.open(VERSION).then(async (cache) => {
    const cached = await cache.match(request);
    const network = fetch(request)
      .then((response) => {
        if (response.ok) {
          void cache.put(request, response.clone());
        }
        return response;
      })
      .catch(() => cached);
    return (await network) ?? cached ?? Response.error();
  });
}
