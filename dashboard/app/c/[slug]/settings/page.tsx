"use client";

// Phase 27.5: per-vendor end-user settings page.
//
// End user manages, for a single vendor:
//   - display_name_override (text input, empty = clear, falls back
//     to end_users.display_name on bot/operator read paths).
//   - notification_pref (radio: all / mentions / off). 'mentions'
//     is a future hook (bots will need to @-mention end users
//     explicitly); for v1 only 'all' and 'off' gate Phase 28's
//     push delivery.
//   - Unsubscribe button: soft-revokes (sets removed_at). Past
//     conversations stay accessible per spec; today the unlinked
//     vendor just drops off /c.
//
// Lives at /c/{slug}/settings. Reached from the per-card "Settings"
// link on /c (Phase 27.4). Back-link returns to /c/{slug}.

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import {
  EndUserUnauthorizedError,
  EndUserVendor,
  fetchEndUserVendor,
  patchEndUserVendor,
  unlinkEndUserVendor,
} from "../../../api";

type State =
  | { kind: "loading" }
  | { kind: "ok"; vendor: EndUserVendor }
  | { kind: "needs-signin" }
  | { kind: "not-found" }
  | { kind: "error"; message: string };

type NotificationPref = "all" | "mentions" | "off";

const PREF_OPTIONS: { value: NotificationPref; label: string; hint: string }[] = [
  {
    value: "all",
    label: "All replies",
    hint: "Notify me whenever the bot or an operator sends a message.",
  },
  {
    value: "mentions",
    label: "Mentions only",
    hint: "Notify me only when the bot explicitly @-mentions me. (Coming soon.)",
  },
  {
    value: "off",
    label: "Off",
    hint: "No push notifications from this vendor.",
  },
];

export default function VendorSettingsPage() {
  const params = useParams();
  const router = useRouter();
  const slug = String(params?.slug ?? "");
  const [state, setState] = useState<State>({ kind: "loading" });
  const [displayName, setDisplayName] = useState("");
  const [pref, setPref] = useState<NotificationPref>("all");
  const [savingSettings, setSavingSettings] = useState(false);
  const [unlinking, setUnlinking] = useState(false);
  const [saveOk, setSaveOk] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const vendor = await fetchEndUserVendor(slug);
      setState({ kind: "ok", vendor });
      setDisplayName(vendor.display_name_override ?? "");
      setPref(
        (vendor.notification_pref as NotificationPref | undefined) ?? "all",
      );
    } catch (e) {
      if (e instanceof EndUserUnauthorizedError) {
        setState({ kind: "needs-signin" });
        return;
      }
      const msg = (e as Error).message || "";
      if (msg.toLowerCase().includes("not linked")) {
        setState({ kind: "not-found" });
        return;
      }
      setState({ kind: "error", message: msg });
    }
  }, [slug]);

  useEffect(() => {
    load();
  }, [load]);

  async function onSave() {
    if (state.kind !== "ok" || savingSettings) return;
    setSavingSettings(true);
    setSaveOk(false);
    setError(null);
    try {
      await patchEndUserVendor(state.vendor.id, {
        display_name_override: displayName.trim() || null,
        notification_pref: pref,
      });
      setSaveOk(true);
      setTimeout(() => setSaveOk(false), 1800);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSavingSettings(false);
    }
  }

  async function onUnlink() {
    if (state.kind !== "ok" || unlinking) return;
    const ok = window.confirm(
      `Unsubscribe from ${state.vendor.name}? You'll lose access to chat with them; existing conversations stay readable from your history.`,
    );
    if (!ok) return;
    setUnlinking(true);
    setError(null);
    try {
      await unlinkEndUserVendor(state.vendor.id);
      router.replace("/c");
    } catch (e) {
      setError((e as Error).message);
      setUnlinking(false);
    }
  }

  if (state.kind === "loading") {
    return (
      <main className="min-h-screen flex items-center justify-center text-sm text-gray-400">
        loading…
      </main>
    );
  }
  if (state.kind === "needs-signin") {
    return (
      <main className="min-h-screen px-6 py-16 max-w-md mx-auto text-center">
        <h1 className="text-2xl font-semibold tracking-tight mb-3">
          Sign in to continue
        </h1>
        <p className="text-sm text-gray-500">
          Your session expired. Ask the vendor to send a fresh
          magic-link email.
        </p>
      </main>
    );
  }
  if (state.kind === "not-found") {
    return (
      <main className="min-h-screen px-6 py-16 max-w-md mx-auto text-center">
        <h1 className="text-2xl font-semibold tracking-tight mb-3">
          Vendor not found
        </h1>
        <p className="text-sm text-gray-500 mb-6">
          Either this vendor doesn&apos;t exist, or you haven&apos;t been
          invited.
        </p>
        <Link
          href="/c"
          className="text-sm text-indigo-600 hover:text-indigo-700"
        >
          ← Back to your chats
        </Link>
      </main>
    );
  }
  if (state.kind === "error") {
    return (
      <main className="min-h-screen px-6 py-16 max-w-md mx-auto text-center">
        <p className="text-sm text-red-600 mb-6">{state.message}</p>
        <Link
          href="/c"
          className="block text-sm text-indigo-600 hover:text-indigo-700"
        >
          ← Back to your chats
        </Link>
      </main>
    );
  }

  const { vendor } = state;
  const dirty =
    displayName.trim() !== (vendor.display_name_override ?? "") ||
    pref !== (vendor.notification_pref ?? "all");

  return (
    <main className="min-h-screen flex flex-col">
      <header className="border-b border-gray-200 px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link
            href={`/c/${encodeURIComponent(slug)}`}
            className="text-sm text-gray-500 hover:text-gray-800"
          >
            ←
          </Link>
          <div>
            <div className="text-base font-medium text-gray-900">
              {vendor.name}
            </div>
            <div className="text-xs text-gray-500">Settings</div>
          </div>
        </div>
      </header>

      <div className="max-w-xl mx-auto w-full px-6 py-8 space-y-8">
        {error && (
          <div className="p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
            {error}
          </div>
        )}

        {/* Display name override */}
        <section>
          <h2 className="text-[11px] font-semibold text-gray-500 mb-2 uppercase tracking-wider">
            Display name
          </h2>
          <p className="text-xs text-gray-500 mb-3">
            How {vendor.name}&apos;s bots see you. Empty = use the
            display name from your Lightsei account.
          </p>
          <input
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="(default)"
            maxLength={128}
            className="w-full text-sm rounded-md ring-1 ring-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-600"
          />
        </section>

        {/* Notification pref */}
        <section>
          <h2 className="text-[11px] font-semibold text-gray-500 mb-2 uppercase tracking-wider">
            Notifications
          </h2>
          <p className="text-xs text-gray-500 mb-3">
            When should we push notify you about messages from
            {" "}{vendor.name}?
          </p>
          <fieldset className="space-y-2">
            {PREF_OPTIONS.map((opt) => (
              <label
                key={opt.value}
                className={
                  "block rounded-md border px-3 py-2 cursor-pointer transition-colors " +
                  (pref === opt.value
                    ? "border-indigo-400 bg-indigo-50/40"
                    : "border-gray-200 hover:border-gray-300")
                }
              >
                <div className="flex items-start gap-3">
                  <input
                    type="radio"
                    name="notification_pref"
                    value={opt.value}
                    checked={pref === opt.value}
                    onChange={() => setPref(opt.value)}
                    className="mt-0.5"
                  />
                  <div>
                    <div className="text-sm font-medium text-gray-900">
                      {opt.label}
                    </div>
                    <div className="text-xs text-gray-500">{opt.hint}</div>
                  </div>
                </div>
              </label>
            ))}
          </fieldset>
        </section>

        <div className="flex items-center justify-between">
          <button
            type="button"
            onClick={onSave}
            disabled={savingSettings || !dirty}
            className="text-sm rounded-md bg-indigo-600 text-white px-4 py-2 hover:bg-indigo-500 disabled:opacity-50"
          >
            {savingSettings ? "Saving…" : "Save"}
          </button>
          {saveOk && (
            <span className="text-xs text-indigo-700">Saved.</span>
          )}
        </div>

        {/* Unsubscribe */}
        <section className="pt-8 border-t border-gray-200">
          <h2 className="text-[11px] font-semibold text-red-700 mb-2 uppercase tracking-wider">
            Unsubscribe
          </h2>
          <p className="text-xs text-gray-500 mb-3">
            Stop receiving messages from {vendor.name}. Your past
            conversations stay accessible from your history; you
            won&apos;t be able to send new messages until the vendor
            re-invites you.
          </p>
          <button
            type="button"
            onClick={onUnlink}
            disabled={unlinking}
            className="text-sm rounded-md bg-red-600 text-white px-3 py-1.5 hover:bg-red-500 disabled:opacity-50"
          >
            {unlinking ? "Unsubscribing…" : `Unsubscribe from ${vendor.name}`}
          </button>
        </section>
      </div>
    </main>
  );
}
