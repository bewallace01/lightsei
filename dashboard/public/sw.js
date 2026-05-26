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

// Bumped from v1 → v2 in Phase 28.4 to force the new install path
// (adds `push` + `notificationclick` listeners). Browsers run the
// new install handler when this file's content changes; the v1
// activate handler still prunes the v1 cache below.
const CACHE_NAME = "lightsei-c-shell-v2";

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


// Phase 28.4: web-push handler.
//
// Payload shape (from backend/push.py + main.py 28.3 wiring):
//   { title: string, body: string, deep_link_url?: string, icon?: string }
//
// Backend lazy-imports pywebpush + signs the payload via VAPID;
// the browser hands it here as event.data. We render the
// notification via showNotification — service workers can show
// notifications even when no /c tab is open, which is the whole
// point of push (vs in-page polling).
self.addEventListener("push", (event) => {
  let payload = null;
  if (event.data) {
    try {
      payload = event.data.json();
    } catch {
      // Some push services / payloads aren't JSON. Fall back to a
      // plain text body so the user still sees SOMETHING rather
      // than a silent push.
      payload = { title: "Lightsei", body: event.data.text() };
    }
  }
  payload = payload || {};

  const title = payload.title || "Lightsei";
  const body = payload.body || "You have a new message.";
  const deepLinkUrl = payload.deep_link_url || "/c";

  event.waitUntil(
    self.registration.showNotification(title, {
      body: body,
      // iOS 16.4+ uses the apple-touch-icon for the notification
      // when no explicit icon is provided. Passing one here ensures
      // Chrome/Firefox/Android pick up the indigo "L" too.
      icon: payload.icon || "/icon-192.png",
      badge: "/icon-192.png",
      // Stash the deep-link target on the notification so the
      // notificationclick handler below can read it without
      // re-parsing the payload.
      data: { deep_link_url: deepLinkUrl },
    }),
  );
});


// Phase 28.4: tap-to-open handler.
//
// When the user taps the notification, focus the existing /c tab
// at the deep-link URL if one is open, otherwise open a fresh one.
// Standard service-worker pattern via clients.matchAll +
// clients.openWindow.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const deepLinkUrl =
    (event.notification.data && event.notification.data.deep_link_url) || "/c";

  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clients) => {
        // Try to reuse an existing /c-scope tab so the user doesn't
        // pile up duplicates. Match on origin only; the navigation
        // below moves the tab to the deep link.
        for (const client of clients) {
          try {
            const url = new URL(client.url);
            if (url.origin === self.location.origin && "focus" in client) {
              return client.focus().then(() => {
                // Best-effort: navigate to the deep link via
                // postMessage so the React app can router.replace,
                // since service workers can't call navigate() on a
                // client directly. The client checks for
                // {kind:'deep-link', url} messages.
                if ("postMessage" in client) {
                  client.postMessage({ kind: "deep-link", url: deepLinkUrl });
                }
                return client;
              });
            }
          } catch {
            // Skip malformed client URLs.
          }
        }
        // No tab open → open a fresh one at the deep link.
        if (self.clients.openWindow) {
          return self.clients.openWindow(deepLinkUrl);
        }
        return null;
      }),
  );
});
