// Phase 26.4: layout for /c/* consumer routes.
//
// Wraps every /c route with PWA metadata (manifest link, apple-touch
// icon, standalone display hints) + the ServiceWorkerRegister client
// component that registers /sw.js on mount.
//
// The root layout already pulls in the operator-aware Header; this
// layout deliberately omits it because the consumer surface has no
// operator nav. Per Phase 26 spec: "Layout chrome distinct from
// operator dashboard (no header nav, no constellation map, just chat)."
//
// Next.js merges this metadata with the root layout's; the manifest
// + apple-touch settings here apply on top of the root font setup.

import type { Metadata, Viewport } from "next";
import ServiceWorkerRegister from "./ServiceWorkerRegister";

export const metadata: Metadata = {
  title: "Lightsei chat",
  description: "Chat with the bots from the vendors you're subscribed to.",
  // Manifest link tells iOS Safari + Chrome the site is installable.
  manifest: "/manifest.webmanifest",
  appleWebApp: {
    capable: true,
    title: "Lightsei",
    // 'default' = white status bar matching the manifest background.
    statusBarStyle: "default",
  },
  // Next.js maps this to <link rel="apple-touch-icon" href="...">.
  icons: {
    apple: "/apple-touch-icon.png",
    icon: [
      { url: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { url: "/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
  },
};

export const viewport: Viewport = {
  // Matches the manifest theme_color. iOS uses this for the
  // status-bar tint when installed standalone.
  themeColor: "#6366f1",
  // Lock initial-scale so the chat composer doesn't auto-zoom on
  // iOS when the input is focused.
  initialScale: 1,
  width: "device-width",
};

export default function ConsumerLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <>
      <ServiceWorkerRegister />
      {children}
    </>
  );
}
