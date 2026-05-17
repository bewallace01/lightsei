"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { completeGoogleOAuth, setSession } from "../../../api";
import Logo from "../../../Logo";

type Status =
  | { kind: "verifying" }
  | { kind: "ok"; isNewUser: boolean }
  | { kind: "error"; message: string };

function GoogleCallbackHandler() {
  const router = useRouter();
  const params = useSearchParams();
  const [status, setStatus] = useState<Status>({ kind: "verifying" });

  useEffect(() => {
    const code = params.get("code");
    const state = params.get("state");
    const oauthError = params.get("error");

    if (oauthError) {
      setStatus({
        kind: "error",
        message:
          oauthError === "access_denied"
            ? "You cancelled the Google sign-in."
            : `Google returned an error: ${oauthError}`,
      });
      return;
    }
    if (!code || !state) {
      setStatus({
        kind: "error",
        message: "Google didn't send back the expected sign-in parameters.",
      });
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        const res = await completeGoogleOAuth(code, state);
        if (cancelled) return;
        setSession(res.session_token, res.user, res.workspace);
        setStatus({ kind: "ok", isNewUser: !!res.is_new_user });
        const dest = res.redirect_after || "/";
        // Same brief pause as the magic-link page so the user sees
        // the success state before the dashboard takes over.
        setTimeout(() => router.push(dest), 700);
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
        <div className="mb-6 flex justify-center">
          <Logo size={28} />
        </div>
        {status.kind === "verifying" && (
          <>
            <h1 className="text-2xl font-semibold tracking-tight mb-3">
              Finishing Google sign-in
            </h1>
            <p className="text-sm text-gray-500">One moment…</p>
          </>
        )}
        {status.kind === "ok" && (
          <>
            <h1 className="text-2xl font-semibold tracking-tight mb-3">
              {status.isNewUser ? "Welcome to Lightsei" : "You're in"}
            </h1>
            <p className="text-sm text-gray-500">Taking you to the dashboard…</p>
          </>
        )}
        {status.kind === "error" && (
          <>
            <h1 className="text-2xl font-semibold tracking-tight mb-3">
              Google sign-in didn&apos;t complete
            </h1>
            <p className="text-sm text-gray-600 mb-6">{status.message}</p>
            <Link
              href="/login"
              className="text-sm text-accent-700 hover:text-accent-800 font-medium"
            >
              Back to sign-in
            </Link>
          </>
        )}
      </div>
    </main>
  );
}

export default function GoogleCallbackPage() {
  return (
    <Suspense fallback={<div className="min-h-screen" />}>
      <GoogleCallbackHandler />
    </Suspense>
  );
}
