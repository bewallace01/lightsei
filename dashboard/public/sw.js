// Phase 26.4: service worker for the /c consumer surface.
//
// Two jobs:
//   1. Precache the /c shell on install so a home-screen launch
//      opens to "your chats" even offline (data fetches will fail
//      but the UI renders an empty-loading state instead of a
//      browser error page).
//   2. On fetch, serve /c shell + icons from cache first (fast +
//      offline-safe). Everything else (API calls, operator routes,
//      JS chunks not in the precache list) is network-only so live
//      data + new builds load correctly.
//
// What this is NOT:
//   - A general-purpose offline mode. API calls don't get cached
//     because the data has to come from the backend; users that
//     open the app offline see the shell + an error state when
//     their fetches fail.
//   - A background-sync queue. Sending a message while offline
//     just fails; Phase 28 (push) will add the inverse direction
//     but bot/operator replies still need the network.
//
// Bump CACHE_NAME on any incremental change to force clients to
// re-fetch the precache list. Browsers run the new install handler
// when the file content changes; the activate handler prunes old
// caches.

const CACHE_NAME = "lightsei-c-shell-v1";

const PRECACHE_URLS = [
  "/c",
  "/manifest.webmanifest",
  "/apple-touch-icon.png",
  "/icon-192.png",
  "/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) =>
        // addAll is atomic: any 404 aborts the install so we never
        // ship a half-warm cache.
        cache.addAll(PRECACHE_URLS),
      )
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k.startsWith("lightsei-") && k !== CACHE_NAME)
            .map((k) => caches.delete(k)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Same-origin only — don't try to cache cross-origin API calls
  // (e.g. fonts.googleapis.com is loaded by next/font but next/font
  // self-hosts in this app so it's same-origin anyway).
  if (url.origin !== self.location.origin) return;

  // Cache-first for the /c shell + icons + manifest.
  const isPrecached =
    PRECACHE_URLS.includes(url.pathname) ||
    url.pathname.startsWith("/c/auth/magic-link"); // shell-only, no API
  if (isPrecached) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((res) => {
          // Best-effort: stash a fresh copy for next time. Failures
          // don't break the original response.
          if (res && res.status === 200) {
            const clone = res.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, clone));
          }
          return res;
        });
      }),
    );
    return;
  }
  // Everything else (API calls, JS chunks, operator routes) is
  // network-only. The browser's HTTP cache still applies via the
  // server's Cache-Control headers; we just don't shadow it here.
});
