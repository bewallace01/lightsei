"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { fetchRunSummaries, RunSummary, UnauthorizedError } from "../api";

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function RunsPage() {
  const router = useRouter();
  const [rows, setRows] = useState<RunSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const data = await fetchRunSummaries();
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
    const id = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [router]);

  return (
    <main className="px-8 py-10 max-w-6xl mx-auto">
      <div className="flex items-baseline justify-between mb-8">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Runs</h1>
          <p className="text-sm text-gray-500 mt-1">
            Every call your agents made, newest first.
          </p>
        </div>
        <span className="text-xs text-gray-400">refreshes every 2s</span>
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
          <div className="text-gray-700 font-medium mb-2">No runs yet</div>
          <p className="text-sm text-gray-500 mb-4">
            A &quot;run&quot; is a span of work your bot did — typically one
            LLM call (OpenAI / Anthropic / Gemini) auto-instrumented by the
            SDK. Deploy a bot first; runs land here as it works.
          </p>
          <div className="flex items-center justify-center gap-3">
            <Link
              href="/getting-started"
              className="px-4 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 no-underline"
            >
              Get started →
            </Link>
            <Link
              href="/agents/new"
              className="px-4 py-2 border border-gray-300 text-gray-700 rounded-md text-sm font-medium hover:bg-gray-50 no-underline"
            >
              Deploy a bot
            </Link>
          </div>
        </div>
      ) : (
        <div className="rounded-lg border border-gray-200 overflow-hidden">
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
