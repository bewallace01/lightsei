"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  Deployment,
  UnauthorizedError,
  fetchDeployments,
} from "../api";

function fmtRelative(iso: string | null): string {
  if (!iso) return "—";
  try {
    const ts = new Date(iso).getTime();
    const diff = Math.max(0, Date.now() - ts);
    const m = Math.round(diff / 60000);
    if (m < 1) return "just now";
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    const d = Math.round(h / 24);
    return `${d}d ago`;
  } catch {
    return "—";
  }
}

const STATUS_STYLES: Record<Deployment["status"], string> = {
  queued: "bg-gray-100 text-gray-700",
  building: "bg-blue-100 text-blue-800",
  running: "bg-emerald-100 text-emerald-800",
  stopped: "bg-gray-100 text-gray-500",
  failed: "bg-red-100 text-red-800",
};

export default function DeploymentsPage() {
  const router = useRouter();
  const [rows, setRows] = useState<Deployment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const data = await fetchDeployments();
        if (!alive) return;
        setRows(data);
        setError(null);
      } catch (e) {
        if (!alive) return;
        if (e instanceof UnauthorizedError) {
          router.replace("/login");
          return;
        }
        setError(String(e));
      } finally {
        if (alive) setLoading(false);
      }
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [router]);

  return (
    <main className="px-8 py-10 max-w-6xl mx-auto">
      <div className="flex items-baseline justify-between mb-8">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Deployments</h1>
          <p className="text-sm text-gray-500 mt-1">
            Every bot the worker is running, plus stopped + failed history.
          </p>
        </div>
        <div className="flex items-center gap-4">
          <span className="text-xs text-gray-400">refreshes every 5s</span>
          <Link
            href="/agents/new"
            className="px-3 py-1.5 text-sm bg-accent-600 text-white rounded-md hover:bg-accent-700 no-underline"
          >
            + new deploy
          </Link>
        </div>
      </div>

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-gray-400 text-sm">loading…</div>
      ) : rows.length === 0 ? (
        <div className="border border-dashed border-gray-200 rounded-lg p-10 text-center">
          <div className="text-gray-700 font-medium mb-1">No deployments yet</div>
          <p className="text-sm text-gray-500 mb-4">
            Drop a .zip on{" "}
            <Link href="/agents/new" className="text-accent-600 hover:underline">
              /agents/new
            </Link>
            , push to a registered agent path on{" "}
            <Link href="/github" className="text-accent-600 hover:underline">
              /github
            </Link>
            , or use the CLI: <code>lightsei deploy ./your-bot</code>.
          </p>
          <Link
            href="/agents/new"
            className="inline-block px-4 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 no-underline"
          >
            + new deploy
          </Link>
        </div>
      ) : (
        <div className="rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full text-left text-sm">
            <thead className="bg-gray-50 text-[11px] uppercase tracking-wider text-gray-500">
              <tr>
                <th className="px-4 py-3 font-medium">Agent</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Source</th>
                <th className="px-4 py-3 font-medium">Heartbeat</th>
                <th className="px-4 py-3 font-medium">Created</th>
                <th className="px-4 py-3 font-medium">Error</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((d, i) => (
                <tr
                  key={d.id}
                  className={
                    "hover:bg-gray-50 transition-colors " +
                    (i !== rows.length - 1 ? "border-b border-gray-100" : "")
                  }
                >
                  <td className="px-4 py-3">
                    <Link
                      href={`/deployments/${d.id}`}
                      className="text-accent-600 hover:text-accent-700 font-medium"
                    >
                      {d.agent_name}
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={
                        "inline-block px-2 py-0.5 rounded-full text-[11px] font-medium " +
                        (STATUS_STYLES[d.status] ?? "bg-gray-100 text-gray-700")
                      }
                    >
                      {d.status}
                    </span>
                    {d.desired_state === "stopped" && d.status !== "stopped" && (
                      <span className="ml-2 text-[10px] text-gray-400">
                        (stopping)
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-600">
                    {d.source}
                    {d.source_commit_sha && (
                      <span className="text-gray-400">
                        {" "}
                        @ {d.source_commit_sha.slice(0, 7)}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-700 text-xs">
                    {fmtRelative(d.heartbeat_at)}
                  </td>
                  <td className="px-4 py-3 text-gray-700 text-xs">
                    {fmtRelative(d.created_at)}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-red-700 max-w-xs truncate">
                    {d.error ?? ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
