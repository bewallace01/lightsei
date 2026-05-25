"use client";

// Phase 26.5: "Add to Home Screen" prompt for iOS Safari visitors.
//
// Renders a small dismissible bottom banner with the iOS install
// instructions (share icon → Add to Home Screen). Only shows when:
//
//   1. The platform is iOS Safari (not Chrome, not a webview).
//   2. The user hasn't already installed (display-mode != standalone).
//   3. The user hasn't previously dismissed the prompt.
//
// Auto-hides if the display mode flips to standalone mid-session
// (browser fires a media-query change event when the user
// completes the install flow without reloading the tab).
//
// Why iOS Safari only:
//   - Android Chrome handles its own beforeinstallprompt event and
//     surfaces a native install banner; we don't need to add UI.
//   - Desktop Safari + Chrome can install too, but the home-screen
//     install is the Phase 26 motivator (consumer-facing PWA).
//   - Embedded browsers (Instagram, Facebook, etc.) can't install
//     at all; showing the prompt would just confuse the user.
//
// Persistence: dismissal is local-only (no backend round trip). If
// the user clears localStorage or switches devices, they see it
// again. Acceptable for a one-time onboarding nudge.

import { useEffect, useState } from "react";

const DISMISSED_KEY = "lightsei.c_install_prompt_dismissed";

function isIOSSafari(): boolean {
  if (typeof window === "undefined") return false;
  const ua = window.navigator.userAgent;
  // iPhone / iPad / iPod (note: iPadOS 13+ reports as Mac; treat
  // touch + Safari as iPad-equivalent).
  const isIOSDevice =
    /iPhone|iPad|iPod/.test(ua) ||
    (ua.includes("Macintosh") && "ontouchend" in document);
  if (!isIOSDevice) return false;
  // Safari only — exclude in-app browsers (FBAN/FBAV/Instagram) and
  // Chrome on iOS (CriOS) which lacks the install flow.
  const isSafari =
    /Safari/.test(ua) &&
    !/CriOS|FxiOS|EdgiOS|FBAN|FBAV|Instagram|Line/.test(ua);
  return isSafari;
}

function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  // Standard PWA-installed signal.
  if (window.matchMedia("(display-mode: standalone)").matches) return true;
  // iOS-specific legacy fallback.
  const nav = window.navigator as Navigator & { standalone?: boolean };
  return nav.standalone === true;
}

function wasDismissed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(DISMISSED_KEY) === "1";
  } catch {
    return false;
  }
}

export default function InstallPrompt() {
  const [show, setShow] = useState(false);

  useEffect(() => {
    if (!isIOSSafari()) return;
    if (isStandalone()) return;
    if (wasDismissed()) return;
    setShow(true);

    // Auto-hide if the user completes the install mid-session.
    const mq = window.matchMedia("(display-mode: standalone)");
    const onChange = () => {
      if (mq.matches) setShow(false);
    };
    mq.addEventListener?.("change", onChange);
    return () => {
      mq.removeEventListener?.("change", onChange);
    };
  }, []);

  function dismiss() {
    try {
      window.localStorage.setItem(DISMISSED_KEY, "1");
    } catch {
      // Quota / private mode — accept that the prompt will reappear
      // on next visit. Better than a UI that does nothing on click.
    }
    setShow(false);
  }

  if (!show) return null;

  return (
    <div
      role="dialog"
      aria-label="Install Lightsei on your home screen"
      className="fixed bottom-3 left-3 right-3 z-30 rounded-xl bg-gray-900 text-white shadow-lg px-4 py-3 flex items-start gap-3"
    >
      <div className="flex-1 text-sm leading-relaxed">
        <div className="font-medium mb-0.5">Install Lightsei</div>
        <div className="text-gray-200 text-[13px]">
          Tap the share icon <ShareIcon /> in Safari&apos;s toolbar,
          then choose <span className="font-medium">Add to Home Screen</span>{" "}
          for a one-tap launch.
        </div>
      </div>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss install prompt"
        className="shrink-0 -mr-1 -mt-1 p-1 text-gray-400 hover:text-white text-lg leading-none"
      >
        ×
      </button>
    </div>
  );
}

function ShareIcon() {
  // Inline SVG of the iOS share glyph: arrow up out of a box.
  // Sized to sit on the body-text baseline.
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      width="14"
      height="14"
      className="inline-block align-text-bottom mx-0.5"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M12 3v12" />
      <polyline points="7 8 12 3 17 8" />
      <path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7" />
    </svg>
  );
}
