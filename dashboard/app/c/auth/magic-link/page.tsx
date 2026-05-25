"use client";

// Phase 26.2: end-user magic-link consume page.
//
// The 25.2 magic-link email points here:
//   /c/auth/magic-link?token=<plaintext>
//
// On mount we POST the token to /auth/end-user/magic-link/consume,
// persist the returned session_token via the endUserSession helper,
// and redirect to /c. Errors (invalid / expired / consumed token)
// render a "request a new link" hint without leaking which case
// hit.

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { consumeEndUserMagicLink } from "../../../api";
import { setEndUserSession } from "../../../endUserSession";

type Status =
  | { kind: "verifying" }
  | { kind: "ok"; isNew: boolean; displayName: string | null }
  | { kind: "error"; message: string };

function ConsumeInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [status, setStatus] = useState<Status>({ kind: "verifying" });

  useEffect(() => {
    const token = params.get("token");
    if (!token) {
      setStatus({
        kind: "error",
        message: "This link is missing its sign-in token. Request a new one.",
      });
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const res = await consumeEndUserMagicLink(token, {
          vendor_invite_code:
            params.get("vendor_invite_code") || undefined,
        });
        if (cancelled) return;
        setEndUserSession(res.session_token);
        setStatus({
          kind: "ok",
          isNew: res.is_new_end_user,
          displayName: res.end_user.display_name,
        });
        // Brief pause so the user sees the success state before
        // bouncing into the vendor list.
        setTimeout(() => router.replace("/c"), 700);
      } catch (err) {
        if (cancelled) return;
        setStatus({ kind: "error", message: (err as Error).message });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [params, router]);

  return (
    <main className="min-h-screen flex flex-col items-center justify-center px-6">
      <div className="w-full max-w-sm text-center">
        {status.kind === "verifying" && (
          <>
            <h1 className="text-2xl font-semibold tracking-tight mb-3">
              Signing you in
            </h1>
            <p className="text-sm text-gray-500">One moment…</p>
          </>
        )}
        {status.kind === "ok" && (
          <>
            <h1 className="text-2xl font-semibold tracking-tight mb-3">
              {status.isNew ? "Welcome" : "Welcome back"}
              {status.displayName ? `, ${status.displayName}` : ""}
            </h1>
            <p className="text-sm text-gray-500">
              Taking you to your chats…
            </p>
          </>
        )}
        {status.kind === "error" && (
          <>
            <h1 className="text-2xl font-semibold tracking-tight mb-3">
              Sign-in link expired
            </h1>
            <p className="text-sm text-gray-500 mb-6">{status.message}</p>
            <p className="text-xs text-gray-400">
              Magic links expire 15 minutes after they&apos;re issued and
              can only be used once. Ask the vendor to send you a fresh
              one, or check the email account you signed up with.
            </p>
          </>
        )}
      </div>
    </main>
  );
}

export default function MagicLinkConsumePage() {
  // Next.js requires useSearchParams to live under a Suspense
  // boundary at build time.
  return (
    <Suspense
      fallback={
        <main className="min-h-screen flex items-center justify-center text-sm text-gray-400">
          loading…
        </main>
      }
    >
      <ConsumeInner />
    </Suspense>
  );
}
