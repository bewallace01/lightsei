"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { consumeMagicLink, setSession } from "../../api";
import Logo from "../../Logo";

type Status =
  | { kind: "verifying" }
  | { kind: "ok"; isNewUser: boolean }
  | { kind: "error"; message: string };

function MagicLinkConsumer() {
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
        const res = await consumeMagicLink(token);
        if (cancelled) return;
        setSession(res.session_token, res.user, res.workspace);
        setStatus({ kind: "ok", isNewUser: !!res.is_new_user });
        // Give the user a beat to read the success state before
        // bouncing them into the dashboard.
        setTimeout(() => router.push("/"), 700);
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
              Signing you in
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
              Sign-in link didn&apos;t work
            </h1>
            <p className="text-sm text-gray-600 mb-6">{status.message}</p>
            <Link
              href="/login"
              className="text-sm text-accent-700 hover:text-accent-800 font-medium"
            >
              Request a fresh link
            </Link>
          </>
        )}
      </div>
    </main>
  );
}

export default function MagicLinkPage() {
  // useSearchParams requires a Suspense boundary in the App Router.
  return (
    <Suspense fallback={<div className="min-h-screen" />}>
      <MagicLinkConsumer />
    </Suspense>
  );
}
