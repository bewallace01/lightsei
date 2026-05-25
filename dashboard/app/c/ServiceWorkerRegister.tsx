"use client";

// Phase 26.4: registers /sw.js on every /c route mount.
//
// Idempotent: navigator.serviceWorker.register is a no-op when the
// same script URL is already registered. Bumping CACHE_NAME in sw.js
// is what forces a fresh install on the next visit.
//
// Soft-fail: a browser that doesn't support service workers (very
// old iOS Safari) just doesn't register; the /c surface still works
// online and the install-to-home-screen path is missing for those
// users only.

import { useEffect } from "react";

export default function ServiceWorkerRegister() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!("serviceWorker" in navigator)) return;
    // Use the cleanup hook only to avoid registering during SSR;
    // the registration itself is fire-and-forget.
    navigator.serviceWorker
      .register("/sw.js", { scope: "/c" })
      .catch(() => {
        // Best-effort. A registration failure shouldn't break the
        // chat surface; it just means home-screen install + offline
        // open won't work for this session.
      });
  }, []);
  return null;
}
