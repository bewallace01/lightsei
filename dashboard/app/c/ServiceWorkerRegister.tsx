"use client";

// Phase 26.4: registers /sw.js on every /c route mount.
// Phase 28.4: listens for `deep-link` postMessages from the SW's
// notificationclick handler so a tap-to-open from a push
// notification lands on the right /c/{slug}/conversation/{id} URL.
//
// Idempotent: navigator.serviceWorker.register is a no-op when the
// same script URL is already registered. Bumping CACHE_NAME in sw.js
// is what forces a fresh install on the next visit.
//
// Soft-fail: a browser that doesn't support service workers (very
// old iOS Safari) just doesn't register; the /c surface still works
// online and the install-to-home-screen path is missing for those
// users only.

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function ServiceWorkerRegister() {
  const router = useRouter();

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!("serviceWorker" in navigator)) return;

    // Fire-and-forget registration.
    navigator.serviceWorker
      .register("/sw.js", { scope: "/c" })
      .catch(() => {
        // Best-effort. A registration failure shouldn't break the
        // chat surface; it just means home-screen install + offline
        // open + push notifications won't work for this session.
      });

    // Phase 28.4: SW's notificationclick handler posts
    // {kind:'deep-link', url} when the user taps a notification.
    // We listen here so the React router takes us to the deep link
    // without a full page reload — client.navigate() isn't reliable
    // cross-browser, and the postMessage bridge IS.
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { kind?: string; url?: string } | null;
      if (data && data.kind === "deep-link" && data.url) {
        router.replace(data.url);
      }
    };
    navigator.serviceWorker.addEventListener("message", onMessage);
    return () => {
      navigator.serviceWorker.removeEventListener("message", onMessage);
    };
  }, [router]);
  return null;
}
