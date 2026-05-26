"use client";

// Phase 28.5: "Enable notifications" prompt for /c.
//
// Renders a card on the consumer surface that toggles a web-push
// subscription on this device. On enable:
//
//   1. Ask the browser for Notification permission (no-op if granted).
//   2. Get the registered service worker (registered by
//      ServiceWorkerRegister on /c mount).
//   3. Call PushManager.subscribe({ userVisibleOnly, applicationServerKey })
//      with the workspace VAPID public key.
//   4. POST the resulting endpoint + p256dh + auth to the backend so
//      the Phase 28.2 send fan-out can reach this device.
//
// Hidden entirely when:
//   - The browser doesn't support service workers or PushManager.
//   - The user has explicitly denied permission (no point nagging).
//   - The backend isn't configured for live push (vapidPublicKey is null
//     in capture mode / local dev).
//
// Subscribed state mirrors backend truth on mount via the
// `initiallySubscribed` prop (from GET /me/end-user) + flips on
// successful subscribe / unsubscribe.

import { useEffect, useState } from "react";

import { subscribeEndUserPush, unsubscribeEndUserPush } from "../api";

type Props = {
  vapidPublicKey: string | null;
  initiallySubscribed: boolean;
};

function pushSupported(): boolean {
  if (typeof window === "undefined") return false;
  return "serviceWorker" in navigator && "PushManager" in window;
}

// VAPID public key arrives from the backend as a base64url string;
// PushManager.subscribe wants a Uint8Array. This is the standard
// translation (pad + URL-safe → standard base64 → byte array).
function urlBase64ToUint8Array(b64: string): Uint8Array {
  const padding = "=".repeat((4 - (b64.length % 4)) % 4);
  const base64 = (b64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = window.atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

function serializeKey(
  sub: PushSubscription,
  name: "p256dh" | "auth",
): string {
  const buf = sub.getKey(name);
  if (!buf) return "";
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.byteLength; i++) {
    s += String.fromCharCode(bytes[i]);
  }
  return window
    .btoa(s)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

export default function EnablePushPrompt({
  vapidPublicKey,
  initiallySubscribed,
}: Props): JSX.Element | null {
  const [subscribed, setSubscribed] = useState(initiallySubscribed);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [permission, setPermission] = useState<NotificationPermission | null>(
    null,
  );

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!("Notification" in window)) return;
    setPermission(Notification.permission);
  }, []);

  if (!pushSupported()) return null;
  if (!vapidPublicKey) return null;
  if (permission === "denied") return null;

  async function enable() {
    setError(null);
    setBusy(true);
    try {
      if (
        "Notification" in window &&
        Notification.permission === "default"
      ) {
        const p = await Notification.requestPermission();
        setPermission(p);
        if (p !== "granted") {
          setBusy(false);
          return;
        }
      }
      const reg = await navigator.serviceWorker.ready;
      const existing = await reg.pushManager.getSubscription();
      const sub =
        existing ??
        (await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapidPublicKey!),
        }));
      await subscribeEndUserPush({
        endpoint: sub.endpoint,
        p256dh: serializeKey(sub, "p256dh"),
        auth: serializeKey(sub, "auth"),
      });
      setSubscribed(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    setError(null);
    setBusy(true);
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        await unsubscribeEndUserPush(sub.endpoint);
        await sub.unsubscribe();
      }
      setSubscribed(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      className="rounded-lg border border-gray-200 bg-white p-4 flex items-start gap-3"
      aria-label="Push notifications"
    >
      <div className="flex-1">
        <div className="text-sm font-semibold text-gray-900">
          {subscribed ? "Notifications enabled" : "Enable notifications"}
        </div>
        <p className="text-xs text-gray-600 mt-1 leading-relaxed">
          {subscribed
            ? "You'll get a notification on this device when a bot or operator replies."
            : "Get a notification on this device when a bot or operator replies, even when this tab is closed."}
        </p>
        {error && (
          <p className="text-xs text-red-700 mt-2">{error}</p>
        )}
      </div>
      <button
        type="button"
        onClick={() => void (subscribed ? disable() : enable())}
        disabled={busy}
        className={
          subscribed
            ? "rounded border border-gray-200 text-gray-700 px-3 py-1.5 text-xs hover:bg-gray-50 disabled:opacity-50"
            : "rounded bg-accent-600 hover:bg-accent-700 text-white px-3 py-1.5 text-xs disabled:opacity-50"
        }
      >
        {busy
          ? subscribed
            ? "Turning off…"
            : "Enabling…"
          : subscribed
            ? "Turn off"
            : "Enable"}
      </button>
    </section>
  );
}
