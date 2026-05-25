"use client";

// Phase 26.2: end-user consumer-chat home.
//
// Reads the end-user session token from localStorage (via
// endUserSession helper), calls /me/end-user to get the user's
// profile + linked vendors, renders a card per vendor with an
// "Open chat" link to /c/{slug}.
//
// Auth failures route to /c/auth/magic-link (no operator-side
// /login redirect — end users have their own flow).

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  EndUserMeResponse,
  EndUserUnauthorizedError,
  EndUserVendor,
  fetchEndUserMe,
} from "../api";
import { clearEndUserToken } from "../endUserSession";

type State =
  | { kind: "loading" }
  | { kind: "ok"; data: EndUserMeResponse }
  | { kind: "needs-signin" }
  | { kind: "error"; message: string };

export default function ConsumerHomePage() {
  const router = useRouter();
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await fetchEndUserMe();
        if (!alive) return;
        setState({ kind: "ok", data });
      } catch (e) {
        if (!alive) return;
        if (e instanceof EndUserUnauthorizedError) {
          setState({ kind: "needs-signin" });
          return;
        }
        setState({ kind: "error", message: (e as Error).message });
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  function signOut() {
    clearEndUserToken();
    setState({ kind: "needs-signin" });
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
        <p className="text-xs text-gray-400">
          New here? Magic-link sign-in arrives via email after a
          vendor adds you as a customer.
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

  const { end_user, linked_vendors } = state.data;

  return (
    <main className="min-h-screen px-6 py-10 max-w-2xl mx-auto">
      <header className="flex items-baseline justify-between mb-10">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Your chats
          </h1>
          <p className="text-xs text-gray-500 mt-1">
            Signed in as {end_user.email}
          </p>
        </div>
        <button
          type="button"
          onClick={signOut}
          className="text-xs text-gray-500 hover:text-gray-700"
        >
          Sign out
        </button>
      </header>

      {linked_vendors.length === 0 ? (
        <div className="rounded-lg border border-dashed border-gray-300 px-6 py-12 text-center">
          <h2 className="text-base font-medium text-gray-900 mb-2">
            No vendors yet
          </h2>
          <p className="text-sm text-gray-500 max-w-sm mx-auto">
            When a vendor invites you to chat with their bots, the
            invite will show up here. Cross-vendor invite redemption
            ships in Phase 27.
          </p>
        </div>
      ) : (
        <ul className="space-y-3">
          {linked_vendors.map((v) => (
            <VendorCard key={v.id} vendor={v} />
          ))}
        </ul>
      )}
    </main>
  );
}

function VendorCard({ vendor }: { vendor: EndUserVendor }) {
  const canOpen = !!vendor.vendor_slug && !!vendor.widget_public_id;
  const inner = (
    <div className="flex items-center justify-between rounded-lg border border-gray-200 hover:border-indigo-400 hover:bg-indigo-50/30 px-4 py-4 transition-colors">
      <div>
        <div className="text-base font-medium text-gray-900">
          {vendor.name}
        </div>
        {vendor.customer_facing_agent_name && (
          <div className="text-xs text-gray-500 mt-1">
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
      <span className="text-sm text-indigo-600">
        {canOpen ? "Open chat →" : ""}
      </span>
    </div>
  );
  if (!canOpen) {
    return <li>{inner}</li>;
  }
  return (
    <li>
      <Link href={`/c/${encodeURIComponent(vendor.vendor_slug!)}`}>
        {inner}
      </Link>
    </li>
  );
}
