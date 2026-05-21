"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { completeConnectorGoogleOAuth, UnauthorizedError } from "../../../api";
import Logo from "../../../Logo";

type Status =
  | { kind: "verifying" }
  | { kind: "ok"; connectorType?: string }
  | { kind: "error"; message: string };

function GoogleConnectorCallbackHandler(): JSX.Element {
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
            ? "You cancelled the Google connector install."
            : `Google returned an error: ${oauthError}`,
      });
      return;
    }
    if (!code || !state) {
      setStatus({
        kind: "error",
        message: "Google did not send back the expected install parameters.",
      });
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        const res = await completeConnectorGoogleOAuth(code, state);
        if (cancelled) return;
        setStatus({ kind: "ok", connectorType: res.connector_type });
        setTimeout(() => router.push(res.redirect_after || "/integrations"), 700);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof UnauthorizedError) {
          router.replace("/login");
          return;
        }
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
              Finishing Google connector install
            </h1>
            <p className="text-sm text-gray-500">One moment...</p>
          </>
        )}
        {status.kind === "ok" && (
          <>
            <h1 className="text-2xl font-semibold tracking-tight mb-3">
              Google connector connected
            </h1>
            <p className="text-sm text-gray-500">
              Taking you back to integrations...
            </p>
          </>
        )}
        {status.kind === "error" && (
          <>
            <h1 className="text-2xl font-semibold tracking-tight mb-3">
              Google connector install did not complete
            </h1>
            <p className="text-sm text-gray-600 mb-6">{status.message}</p>
            <Link
              href="/account"
              className="text-sm text-accent-700 hover:text-accent-800 font-medium"
            >
              Back to account
            </Link>
          </>
        )}
      </div>
    </main>
  );
}

export default function GoogleConnectorCallbackPage(): JSX.Element {
  return (
    <Suspense fallback={<div className="min-h-screen" />}>
      <GoogleConnectorCallbackHandler />
    </Suspense>
  );
}
