"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import {
  UnauthorizedError,
  uploadDeploymentBundle,
} from "../../api";

const AGENT_NAME_RE = /^[a-z][a-z0-9-_]{0,63}$/i;

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

export default function NewAgentDeployPage() {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [agentName, setAgentName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const accept = (next: File | null) => {
    setError(null);
    if (next && !next.name.toLowerCase().endsWith(".zip")) {
      setError(`expected a .zip file, got ${next.name}`);
      return;
    }
    setFile(next);
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) accept(f);
  };

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!agentName.trim() || !file) return;
    if (!AGENT_NAME_RE.test(agentName.trim())) {
      setError(
        "agent name must start with a letter and use only letters, digits, hyphen, or underscore",
      );
      return;
    }
    setUploading(true);
    setProgress(0);
    setError(null);
    try {
      const dep = await uploadDeploymentBundle(
        agentName.trim(),
        file,
        (loaded, total) => setProgress(Math.round((loaded / total) * 100)),
      );
      // Land on the deployment detail page so the user sees build + run
      // logs streaming in real time.
      router.push(`/deployments/${dep.id}`);
    } catch (e) {
      if (e instanceof UnauthorizedError) {
        router.replace("/login");
        return;
      }
      setError(String(e instanceof Error ? e.message : e));
      setUploading(false);
    }
  };

  return (
    <main className="px-8 py-10 max-w-3xl mx-auto">
      <div className="mb-6">
        <Link
          href="/deployments"
          className="text-sm text-gray-500 hover:text-gray-900"
        >
          ← deployments
        </Link>
      </div>

      <h1 className="text-2xl font-semibold tracking-tight mb-2">
        Deploy an agent
      </h1>
      <p className="text-sm text-gray-500 mb-8">
        Drop a zipped bot directory below, give it a name, hit deploy. The
        worker builds a venv from your <code>requirements.txt</code> and starts{" "}
        <code>bot.py</code>. No CLI required.
      </p>

      <form onSubmit={onSubmit} className="space-y-6">
        <label className="block">
          <span className="text-xs font-medium text-gray-600 uppercase tracking-wider">
            Agent name
          </span>
          <input
            type="text"
            value={agentName}
            onChange={(e) => setAgentName(e.target.value)}
            placeholder="e.g. polaris, atlas, my-runner"
            disabled={uploading}
            className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none disabled:opacity-50"
          />
          <span className="text-[11px] text-gray-500 mt-1 block">
            Letters, digits, hyphen, underscore. The dashboard surfaces this
            as the agent&apos;s display name everywhere.
          </span>
        </label>

        <div>
          <span className="text-xs font-medium text-gray-600 uppercase tracking-wider">
            Bundle
          </span>
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={onDrop}
            onClick={() => inputRef.current?.click()}
            className={
              "mt-1 cursor-pointer rounded-md border-2 border-dashed p-8 text-center transition-colors " +
              (dragOver
                ? "border-indigo-400 bg-indigo-50"
                : "border-gray-300 hover:border-gray-400")
            }
          >
            <input
              ref={inputRef}
              type="file"
              accept=".zip,application/zip"
              onChange={(e) => accept(e.target.files?.[0] ?? null)}
              disabled={uploading}
              className="hidden"
            />
            {file ? (
              <div className="text-sm">
                <div className="font-medium text-gray-900">{file.name}</div>
                <div className="text-xs text-gray-500 mt-1">
                  {fmtBytes(file.size)} ·{" "}
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      accept(null);
                    }}
                    className="text-accent-600 hover:underline"
                    disabled={uploading}
                  >
                    remove
                  </button>
                </div>
              </div>
            ) : (
              <>
                <div className="text-sm text-gray-700 font-medium">
                  Drop a .zip here, or click to choose
                </div>
                <div className="text-xs text-gray-500 mt-2">
                  Zip the directory containing your{" "}
                  <code>bot.py</code> + <code>requirements.txt</code>.
                </div>
                <div className="text-xs text-gray-400 mt-1">
                  e.g. on macOS: right-click the folder → Compress
                </div>
              </>
            )}
          </div>
        </div>

        {error && (
          <div className="p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
            {error}
          </div>
        )}

        {uploading && (
          <div>
            <div className="h-2 rounded bg-gray-100 overflow-hidden">
              <div
                className="h-full bg-accent-600 transition-all"
                style={{ width: `${progress}%` }}
              />
            </div>
            <div className="text-[11px] text-gray-500 mt-1">
              uploading… {progress}%
            </div>
          </div>
        )}

        <div className="flex items-center justify-between">
          <Link
            href="/deployments"
            className="text-sm text-gray-500 hover:text-gray-900"
          >
            cancel
          </Link>
          <button
            type="submit"
            disabled={uploading || !agentName.trim() || !file}
            className="px-5 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 disabled:opacity-50 transition-colors"
          >
            {uploading ? "deploying…" : "deploy"}
          </button>
        </div>
      </form>

      <section className="mt-12 rounded-lg border border-gray-200 bg-gray-50 p-5">
        <h2 className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider mb-3">
          What goes in the zip?
        </h2>
        <p className="text-sm text-gray-600 mb-2">
          The bundle is whatever the worker should run. Minimum:
        </p>
        <pre className="font-mono text-[12px] bg-white border border-gray-200 rounded p-3 overflow-x-auto">
{`my-bot/
  bot.py             # entrypoint — must define main()
  requirements.txt   # pip-install before bot.py runs`}
        </pre>
        <p className="text-xs text-gray-500 mt-3">
          On startup the worker calls <code>pip install -r requirements.txt</code>{" "}
          inside a fresh venv, then runs <code>python bot.py</code>. Workspace
          secrets you&apos;ve saved on{" "}
          <Link href="/account" className="text-accent-600 hover:underline">
            /account
          </Link>{" "}
          (e.g. <code>LIGHTSEI_API_KEY</code>, <code>OPENAI_API_KEY</code>) get
          injected as env vars.
        </p>
      </section>
    </main>
  );
}
