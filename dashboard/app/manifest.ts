// Phase 26.4: PWA manifest served at /manifest.webmanifest.
//
// Next.js's manifest convention: an `app/manifest.ts` default export
// returns a `MetadataRoute.Manifest` object that Next serves at
// `/manifest.webmanifest`. The /c layout (Phase 26.4) links to this
// in the <head>, which is what tells iOS Safari + Chrome that the
// site is installable.
//
// `start_url: "/c"` so a home-screen launch lands on the consumer
// surface, not the operator dashboard. `display: "standalone"` strips
// browser chrome on iOS install (the whole point of the PWA in
// Phase 26 — operators see a real-app feel, not Safari).
//
// Maskable icons are included so Android's adaptive-icon clipping
// looks reasonable; iOS uses apple-touch-icon (set in layout
// metadata), not the manifest icons, for the home-screen icon.

import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Lightsei",
    short_name: "Lightsei",
    description:
      "Chat with the bots from the vendors you're subscribed to.",
    start_url: "/c",
    scope: "/c",
    display: "standalone",
    orientation: "portrait",
    background_color: "#ffffff",
    theme_color: "#6366f1",
    icons: [
      {
        src: "/icon-192.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icon-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icon-192-maskable.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "maskable",
      },
      {
        src: "/icon-512-maskable.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
  };
}
