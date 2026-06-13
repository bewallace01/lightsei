"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useRef, useState } from "react";
import { consumeMagicLink, setSession } from "../../api";
import Logo from "../../Logo";

type Status =
  | { kind: "ready" }
  | { kind: "verifying" }
  | { kind: "ok"; isNewUser: boolean }
  | { kind: "error"; message: string };

function MagicLinkConsumer() {
  const router = useRouter();
  const params = useSearchParams();
  const token = params.get("token");
  const [status, setStatus] = useState<Status>(
    token
      ? { kind: "ready" }
      : {
          kind: "error",
          message: "This link is missing its sign-in token. Request a new one.",
        },
  );
  // A magic-link token is single-use. We deliberately do NOT consume it on
  // page load: email security scanners (Outlook SafeLinks, some corporate /
  // Gmail scanners) pre-open links in a headless browser, which would burn
  // the token before the real click. Consuming only on an explicit tap
  // keeps the token alive for the human. The ref guards against a
  // double-tap firing two consume requests (the second would 422).
  const consuming = useRef(false);

  async function finishSignIn() {
    if (!token || consuming.current) return;
    consuming.current = true;
    setStatus({ kind: "verifying" });
    try {
      const res = await consumeMagicLink(token);
      setSession(res.session_token, res.user, res.workspace);
      setStatus({ kind: "ok", isNewUser: !!res.is_new_user });
      setTimeout(() => router.push("/"), 500);
    } catch (err) {
      consuming.current = false; // allow a retry of the (same) live token
      setStatus({ kind: "error", message: (err as Error).message });
    }
  }

  return (
    <main className="min-h-screen flex flex-col items-center justify-center px-6">
      <div className="w-full max-w-sm text-center">
        <div className="mb-6 flex justify-center">
          <Logo size={28} />
        </div>

        {status.kind === "ready" && (
          <>
            <h1 className="text-2xl font-semibold tracking-tight mb-3">
              You&apos;re almost in
            </h1>
            <p className="text-sm text-gray-500 mb-6">
              Tap below to finish signing in.
            </p>
            <button
              onClick={finishSignIn}
              autoFocus
              className="w-full text-sm px-4 py-2.5 rounded-md bg-accent-600 text-white hover:bg-accent-700 font-medium"
            >
              Finish signing in
            </button>
          </>
        )}

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
