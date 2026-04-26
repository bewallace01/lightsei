"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { setSession, signup } from "../api";
import Logo from "../Logo";

type Result = {
  apiKey: string;
  apiKeyPrefix: string;
};

export default function SignupPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [workspaceName, setWorkspaceName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<Result | null>(null);
  const [copied, setCopied] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const res = await signup(email, password, workspaceName);
      setSession(res.session_token, res.user, res.workspace);
      setDone({ apiKey: res.api_key.plaintext, apiKeyPrefix: res.api_key.prefix });
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (done) {
    return (
      <main className="min-h-screen flex flex-col items-center justify-center px-6">
        <div className="w-full max-w-md">
          <div className="mb-6 flex justify-center">
            <Logo size={28} />
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-center mb-2">
            You&apos;re in.
          </h1>
          <p className="text-sm text-gray-500 text-center mb-8">
            Save your API key below. It&apos;s shown once.
          </p>
          <div className="border border-amber-300 bg-amber-50 rounded-lg p-5">
            <div className="text-[10px] uppercase tracking-wider font-semibold text-amber-800 mb-1.5">
              api key
            </div>
            <code className="block font-mono text-sm break-all text-amber-900 mb-3">
              {done.apiKey}
            </code>
            <button
              type="button"
              onClick={async () => {
                await navigator.clipboard.writeText(done.apiKey);
                setCopied(true);
              }}
              className="text-sm text-accent-700 hover:text-accent-800 font-medium"
            >
              {copied ? "copied ✓" : "copy to clipboard"}
            </button>
          </div>
          <pre className="mt-6 text-xs bg-gray-50 border border-gray-200 rounded-lg p-4 overflow-x-auto font-mono text-gray-700">
{`pip install -e ./sdk openai
export LIGHTSEI_API_KEY="${done.apiKey}"
python examples/demo_bot.py`}
          </pre>
          <button
            type="button"
            onClick={() => router.push("/")}
            className="mt-6 w-full bg-accent-600 hover:bg-accent-700 text-white rounded-md py-2.5 text-sm font-medium transition-colors"
          >
            continue to dashboard
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
          Free, no credit card.
        </p>
        <form onSubmit={onSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1.5">
              Workspace name
            </label>
            <input
              value={workspaceName}
              onChange={(e) => setWorkspaceName(e.target.value)}
              required
              placeholder="acme"
              autoFocus
              className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500 transition-shadow"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1.5">
              Email
            </label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
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
              minLength={8}
              className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500 transition-shadow"
            />
            <p className="text-xs text-gray-500 mt-1">at least 8 characters</p>
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
            {busy ? "creating…" : "create account"}
          </button>
        </form>
        <p className="text-sm text-gray-500 mt-6 text-center">
          Already have an account?{" "}
          <Link
            href="/login"
            className="text-accent-600 hover:text-accent-700 font-medium"
          >
            Log in
          </Link>
        </p>
      </div>
    </main>
  );
}
