"use client";

import Link from "next/link";
import { useState } from "react";
import Logo from "../Logo";
import { requestMagicLink, startGoogleOAuth } from "../api";

export default function SignupPage() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState<"magic" | "google" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);

  const sendMagicLink = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy("magic");
    try {
      await requestMagicLink(email);
      setSent(email);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const signUpWithGoogle = async () => {
    setError(null);
    setBusy("google");
    try {
      const { authorization_url } = await startGoogleOAuth("/");
      window.location.href = authorization_url;
    } catch (err) {
      setError((err as Error).message);
      setBusy(null);
    }
  };

  if (sent) {
    return (
      <main className="min-h-screen flex flex-col items-center justify-center px-6">
        <div className="w-full max-w-sm text-center">
          <div className="mb-6 flex justify-center">
            <Logo size={28} />
          </div>
          <h1 className="text-2xl font-semibold tracking-tight mb-3">
            Check your email
          </h1>
          <p className="text-sm text-gray-500 mb-3">
            We sent a sign-in link to <span className="font-medium text-gray-900">{sent}</span>.
          </p>
          <p className="text-sm text-gray-500 mb-8">
            Clicking it will create your workspace and sign you in. The link is good for 15 minutes.
          </p>
          <button
            type="button"
            onClick={() => {
              setSent(null);
              setEmail("");
            }}
            className="text-sm text-accent-700 hover:text-accent-800 font-medium"
          >
            Use a different email
          </button>
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-screen flex flex-col items-center justify-center px-6">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex justify-center">
          <Logo size={28} />
        </div>
        <h1 className="text-2xl font-semibold tracking-tight mb-2 text-center">
          Create your workspace
        </h1>
        <p className="text-sm text-gray-500 text-center mb-8">
          Free to start, $5 of credits on us, no credit card.
        </p>
        <form onSubmit={sendMagicLink} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1.5">
              Email
            </label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoFocus
              placeholder="you@company.com"
              className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500 transition-shadow"
            />
          </div>
          {error && (
            <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-md p-2.5">
              {error}
            </div>
          )}
          <button
            type="submit"
            disabled={busy !== null}
            className="w-full bg-accent-600 hover:bg-accent-700 text-white rounded-md py-2.5 text-sm font-medium disabled:opacity-50 transition-colors"
          >
            {busy === "magic" ? "sending…" : "send magic link"}
          </button>
        </form>
        <div className="flex items-center my-6">
          <div className="flex-1 h-px bg-gray-200" />
          <span className="px-3 text-xs uppercase tracking-wider text-gray-400">
            or
          </span>
          <div className="flex-1 h-px bg-gray-200" />
        </div>
        <button
          type="button"
          onClick={signUpWithGoogle}
          disabled={busy !== null}
          className="w-full border border-gray-300 hover:bg-gray-50 text-gray-800 rounded-md py-2.5 text-sm font-medium disabled:opacity-50 transition-colors flex items-center justify-center gap-2"
        >
          {busy === "google" ? "opening…" : "continue with Google"}
        </button>
        <p className="text-sm text-gray-500 mt-8 text-center">
          Already have an account?{" "}
          <Link
            href="/login"
            className="text-accent-600 hover:text-accent-700 font-medium"
          >
            Log in
          </Link>
        </p>
        <p className="text-xs text-gray-400 mt-4 text-center">
          Developer using the SDK?{" "}
          <Link
            href="/signup/advanced"
            className="hover:text-gray-600 underline"
          >
            Get an API key directly
          </Link>
        </p>
      </div>
    </main>
  );
}
