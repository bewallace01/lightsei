"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { login, setSession } from "../api";
import Logo from "../Logo";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const res = await login(email, password);
      setSession(res.session_token, res.user, res.workspace);
      router.push("/");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="min-h-screen flex flex-col items-center justify-center px-6">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex justify-center">
          <Logo size={28} />
        </div>
        <h1 className="text-2xl font-semibold tracking-tight mb-2 text-center">
          Welcome back
        </h1>
        <p className="text-sm text-gray-500 text-center mb-8">
          Log in to your workspace.
        </p>
        <form onSubmit={onSubmit} className="space-y-4">
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
              className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500 transition-shadow"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1.5">
              Password
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
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
            disabled={busy}
            className="w-full bg-accent-600 hover:bg-accent-700 text-white rounded-md py-2.5 text-sm font-medium disabled:opacity-50 transition-colors"
          >
            {busy ? "logging in…" : "log in"}
          </button>
        </form>
        <p className="text-sm text-gray-500 mt-6 text-center">
          New here?{" "}
          <Link
            href="/signup"
            className="text-accent-600 hover:text-accent-700 font-medium"
          >
            Create an account
          </Link>
        </p>
      </div>
    </main>
  );
}
