"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { RunSummary, UnauthorizedError, fetchRunSummaries, handleAuthError } from "../api";
import EmptyState from "../EmptyState";

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function triggerBadgeClass(kind: string): string {
  return kind === "webhook"
    ? "bg-sky-50 text-sky-700 ring-sky-600/20"
    : "bg-violet-50 text-violet-700 ring-violet-600/20";
}

export default function RunsPage() {
  // useSearchParams forces client-side bailout; Next requires it
  // to live inside a Suspense boundary at build time.
  return (
    <Suspense fallback={<main className="px-4 py-6 sm:px-8 sm:py-10 text-gray-400 text-sm">loading…</main>}>
      <RunsPageInner />
    </Suspense>
  );
}

function RunsPageInner() {
  const router = useRouter();
  const params = useSearchParams();
  const triggerId = params.get("trigger_id") || undefined;
  const [rows, setRows] = useState<RunSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const data = await fetchRunSummaries({ triggerId });
        if (!alive) return;
        setRows(data);
        setError(null);
      } catch (e) {
        if (!alive) return;
        if (handleAuthError(e, router)) return;
        setError(String(e));
      } finally {
        if (alive) setLoading(false);
      }
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [router, triggerId]);

  // When a trigger filter is active, surface the trigger name from
  // the first row (every row in the filtered set belongs to the same
  // trigger) so the filter banner is human-readable.
  const filterName =
    triggerId && rows.length > 0 ? (rows[0].trigger_name ?? null) : null;

  return (
    <main className="px-4 py-6 sm:px-8 sm:py-10 max-w-6xl mx-auto">
      <div className="flex items-baseline justify-between mb-8">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Runs</h1>
          <p className="text-sm text-gray-500 mt-1">
            Every call your agents made, newest first.
          </p>
        </div>
        <span className="text-xs text-gray-400">refreshes every 2s</span>
      </div>

      {triggerId && (
        <div className="mb-6 flex items-center justify-between text-sm bg-indigo-50 ring-1 ring-indigo-200 text-indigo-900 rounded px-3 py-2">
          <div>
            Filtered to trigger:{" "}
            <span className="font-medium">
              {filterName ?? <span className="font-mono text-xs">{triggerId}</span>}
            </span>
          </div>
          <Link
            href="/runs"
            className="text-indigo-700 hover:text-indigo-900 text-xs"
          >
            Clear filter
          </Link>
        </div>
      )}

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-gray-400 text-sm">loading…</div>
      ) : rows.length === 0 ? (
        <EmptyState
          title="No runs yet"
          body={
            <>
              A run is a span of work your bot did, typically one LLM call.
              Once you deploy a team, every call shows up here.
            </>
          }
          primary={{
            href: "/agents/team-from-readme",
            label: "✨ Drop a README to build your team",
          }}
          secondary={{ href: "/agents", label: "See my agents" }}
        />
      ) : (
        <div className="rounded-lg border border-gray-200 overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="bg-gray-50 text-[11px] uppercase tracking-wider text-gray-500">
              <tr>
                <th className="px-4 py-3 font-medium">Started</th>
                <th className="px-4 py-3 font-medium">Agent</th>
                <th className="px-4 py-3 font-medium">Model</th>
                <th className="px-4 py-3 font-medium">Events</th>
                <th className="px-4 py-3 font-medium">Tokens</th>
                <th className="px-4 py-3 font-medium">Latency</th>
                <th className="px-4 py-3 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr
                  key={r.id}
                  className={
                    "hover:bg-gray-50 transition-colors " +
                    (i !== rows.length - 1 ? "border-b border-gray-100" : "")
                  }
                >
                  <td className="px-4 py-3">
                    <Link
                      href={`/runs/${r.id}`}
                      className="text-accent-600 hover:text-accent-700 font-medium"
                    >
                      {fmtTime(r.started_at)}
                    </Link>
                  </td>
                  <td className="px-4 py-3 text-gray-800">
                    <Link
                      href={`/agents/${encodeURIComponent(r.agent_name)}`}
                      className="hover:text-accent-600 transition-colors"
                    >
                      {r.agent_name}
                    </Link>
                    {r.trigger_kind && (
                      <div className="mt-1">
                        <span
                          className={
                            "inline-block text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ring-1 " +
                            triggerBadgeClass(r.trigger_kind)
                          }
                          title={
                            r.trigger_name
                              ? `Triggered by ${r.trigger_kind}: ${r.trigger_name}`
                              : `Triggered by ${r.trigger_kind} (trigger deleted)`
                          }
                        >
                          {r.trigger_kind}
                        </span>
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-600">
                    {r.model ?? "—"}
                  </td>
                  <td className="px-4 py-3 text-gray-700">{r.event_count}</td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-600">
                    {r.input_tokens} / {r.output_tokens}
                  </td>
                  <td className="px-4 py-3 text-gray-700">
                    {r.latency_ms > 0 ? `${r.latency_ms} ms` : "—"}
                  </td>
                  <td className="px-4 py-3">
                    {r.denied ? (
                      <span
                        className="inline-block px-2 py-0.5 rounded-full bg-red-100 text-red-800 text-[11px] font-medium"
                        title={r.denial?.reason ?? "policy denied"}
                      >
                        denied
                      </span>
                    ) : r.ended_at ? (
                      <span className="inline-block px-2 py-0.5 rounded-full bg-gray-100 text-gray-700 text-[11px] font-medium">
                        ended
                      </span>
                    ) : (
                      <span className="inline-block px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 text-[11px] font-medium">
                        running
                      </span>
                    )}
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
