"use client";

// Phase 27.4: end-user consumer-chat home (the my-bots index).
//
// Vendor cards, one per actively-linked vendor. Each card surfaces
// the vendor's name, the customer-facing bot's name, an unread
// count (always 0 in v1 — see Phase 27.2 Done Log + the 27B
// follow-up for real per-vendor last-seen tracking), an "Open chat"
// link, and a "Settings" link to /c/{slug}/settings (built in 27.5).
//
// "+ Add vendor" opens a modal with a single-line invite-code
// input. On success the vendor list refreshes + the modal closes;
// errors show inline. Empty state shows the prominent Add CTA so
// a brand-new end user can redeem their first code immediately.

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  EndUserUnauthorizedError,
  EndUserVendorWithCount,
  fetchEndUserMe,
  fetchEndUserVendorsWithCounts,
  redeemEndUserInvite,
} from "../api";
import { clearEndUserSession } from "../endUserSession";
import EnablePushPrompt from "./EnablePushPrompt";

type State =
  | { kind: "loading" }
  | {
      kind: "ok";
      email: string;
      vendors: EndUserVendorWithCount[];
      // Phase 28.5: carry the push state into the rendered tree so
      // EnablePushPrompt can render with the right initial state.
      vapidPublicKey: string | null;
      hasActivePushSubscription: boolean;
    }
  | { kind: "needs-signin" }
  | { kind: "error"; message: string };

export default function ConsumerHomePage() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [addOpen, setAddOpen] = useState(false);

  const refresh = useCallback(async () => {
    try {
      // Fetch profile + vendors in parallel. /me/end-user gives us
      // the email for the header; /me/end-user/vendors adds the
      // unread_count slot the cards render.
      const [me, vendors] = await Promise.all([
        fetchEndUserMe(),
        fetchEndUserVendorsWithCounts(),
      ]);
      setState({
        kind: "ok",
        email: me.end_user.email,
        vendors: vendors.vendors,
        vapidPublicKey: me.push_vapid_public_key ?? null,
        hasActivePushSubscription:
          me.has_active_push_subscription ?? false,
      });
    } catch (e) {
      if (e instanceof EndUserUnauthorizedError) {
        setState({ kind: "needs-signin" });
        return;
      }
      setState({ kind: "error", message: (e as Error).message });
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  function signOut() {
    clearEndUserSession();
    setState({ kind: "needs-signin" });
  }

  async function onRedeemed() {
    setAddOpen(false);
    // Reload the vendor list so the just-added card appears.
    await refresh();
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
          You&apos;re signed out
        </h1>
        <p className="text-sm text-gray-500 mb-6">
          Sign in via the magic-link email from any vendor that
          invited you, or ask them to send a fresh one.
        </p>
      </main>
    );
  }

  if (state.kind === "error") {
    return (
      <main className="min-h-screen px-6 py-16 max-w-md mx-auto text-center">
        <p className="text-sm text-red-600">{state.message}</p>
      </main>
    );
  }

  const { email, vendors, vapidPublicKey, hasActivePushSubscription } = state;

  return (
    <main className="min-h-screen px-6 py-10 max-w-2xl mx-auto">
      <header className="flex items-baseline justify-between mb-10 gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight">
            Your chats
          </h1>
          <p className="text-xs text-gray-500 mt-1 truncate">
            Signed in as {email}
          </p>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <button
            type="button"
            onClick={() => setAddOpen(true)}
            className="text-sm rounded-md bg-indigo-600 text-white px-3 py-1.5 hover:bg-indigo-500"
          >
            + Add vendor
          </button>
          <button
            type="button"
            onClick={signOut}
            className="text-xs text-gray-500 hover:text-gray-700"
          >
            Sign out
          </button>
        </div>
      </header>

      <div className="mb-6">
        <EnablePushPrompt
          vapidPublicKey={vapidPublicKey}
          initiallySubscribed={hasActivePushSubscription}
        />
      </div>

      {vendors.length === 0 ? (
        <EmptyState onAdd={() => setAddOpen(true)} />
      ) : (
        <ul className="space-y-3">
          {vendors.map((v) => (
            <VendorCard key={v.id} vendor={v} />
          ))}
        </ul>
      )}

      {addOpen && (
        <AddVendorModal
          onClose={() => setAddOpen(false)}
          onRedeemed={onRedeemed}
        />
      )}
    </main>
  );
}

function VendorCard({ vendor }: { vendor: EndUserVendorWithCount }) {
  const canOpen = !!vendor.vendor_slug && !!vendor.widget_public_id;
  return (
    <li className="rounded-lg border border-gray-200 hover:border-indigo-400 hover:bg-indigo-50/30 px-4 py-4 transition-colors">
      <div className="flex items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <div className="text-base font-medium text-gray-900 truncate">
              {vendor.name}
            </div>
            {vendor.unread_count > 0 && (
              <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-indigo-600 text-white">
                {vendor.unread_count}
              </span>
            )}
          </div>
          {vendor.customer_facing_agent_name && (
            <div className="text-xs text-gray-500 mt-0.5">
              Chat with {vendor.customer_facing_agent_name}
            </div>
          )}
          {!canOpen && (
            <div className="text-xs text-amber-700 mt-1">
              This vendor hasn&apos;t finished setting up their chat
              surface yet.
            </div>
          )}
        </div>
        <div className="flex items-center gap-3 shrink-0">
          {canOpen && (
            <Link
              href={`/c/${encodeURIComponent(vendor.vendor_slug!)}`}
              className="text-sm text-indigo-600 hover:text-indigo-800"
            >
              Open chat →
            </Link>
          )}
          {/* Phase 27.5 builds the actual settings page. Linking
              it now means the my-bots index doesn't need a second
              UI iteration when that ships. */}
          {vendor.vendor_slug && (
            <Link
              href={`/c/${encodeURIComponent(vendor.vendor_slug)}/settings`}
              className="text-xs text-gray-500 hover:text-gray-800"
              title="Vendor settings"
            >
              Settings
            </Link>
          )}
        </div>
      </div>
    </li>
  );
}

function EmptyState({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="rounded-lg border border-dashed border-gray-300 px-6 py-12 text-center">
      <h2 className="text-base font-medium text-gray-900 mb-2">
        No vendors yet
      </h2>
      <p className="text-sm text-gray-500 max-w-sm mx-auto mb-5">
        Have an invite code from a vendor? Paste it below to link
        their bot to your account.
      </p>
      <button
        type="button"
        onClick={onAdd}
        className="text-sm rounded-md bg-indigo-600 text-white px-4 py-2 hover:bg-indigo-500"
      >
        Enter invite code
      </button>
    </div>
  );
}

function AddVendorModal({
  onClose,
  onRedeemed,
}: {
  onClose: () => void;
  onRedeemed: () => Promise<void> | void;
}) {
  const [code, setCode] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = code.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await redeemEndUserInvite(trimmed);
      await onRedeemed();
    } catch (e) {
      setError((e as Error).message);
      setSubmitting(false);
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Add a vendor"
      className="fixed inset-0 z-40 bg-black/40 flex items-center justify-center px-4"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-xl shadow-lg w-full max-w-sm p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold mb-1">Add a vendor</h2>
        <p className="text-xs text-gray-500 mb-4">
          Paste the invite code the vendor sent you.
        </p>
        <form onSubmit={onSubmit}>
          <input
            type="text"
            autoFocus
            placeholder="inv-..."
            value={code}
            onChange={(e) => setCode(e.target.value)}
            disabled={submitting}
            className="w-full text-sm font-mono rounded-md ring-1 ring-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-600"
          />
          {error && (
            <div className="mt-3 text-xs text-red-600">{error}</div>
          )}
          <div className="mt-4 flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="text-sm px-3 py-1.5 text-gray-500 hover:text-gray-800"
              disabled={submitting}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting || !code.trim()}
              className="text-sm px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              {submitting ? "Linking…" : "Add"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
