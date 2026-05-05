"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  Agent,
  ConstellationAgent,
  UnauthorizedError,
  fetchAgents,
  fetchConstellation,
} from "../api";


function fmtUsd(n: number): string {
  if (n === 0) return "$0";
  if (n < 0.01) return "<$0.01";
  if (n < 1) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
}


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


function fmtInterval(s: number | null): string {
  if (s == null) return "—";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${(s / 3600).toFixed(s % 3600 === 0 ? 0 : 1)}h`;
  return `${(s / 86400).toFixed(s % 86400 === 0 ? 0 : 1)}d`;
}


// Combined view of an agent: pin/config from /agents + status/activity from
// /constellation. Constellation filters out fully-dormant agents (no events
// in 24h, no recent heartbeat) so both lists' lengths can differ — we merge
// inner-join style on name.
type AgentRow = {
  name: string;
  role: ConstellationAgent["role"] | null;
  status: ConstellationAgent["status"] | null;
  // The pinned model (DB) takes precedence over the recently-observed
  // model (events). When unpinned, fall back to recent.
  pinned_provider: string | null;
  pinned_model: string | null;
  recent_model: string | null;
  runs_24h: number;
  cost_24h_usd: number;
  tick_interval_s: number | null;
  last_event_at: string | null;
};


export default function AgentsPage() {
  const router = useRouter();
  const [rows, setRows] = useState<AgentRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    try {
      const [agents, constellation] = await Promise.all([
        fetchAgents(),
        fetchConstellation(),
      ]);
      const byName = new Map<string, AgentRow>();
      // Seed every agent that exists in the DB so dormant ones still show.
      for (const a of agents) {
        byName.set(a.name, {
          name: a.name,
          role: null,
          status: null,
          pinned_provider: a.provider,
          pinned_model: a.model,
          recent_model: null,
          runs_24h: 0,
          cost_24h_usd: 0,
          tick_interval_s: a.tick_interval_s,
          last_event_at: null,
        });
      }
      // Layer in the constellation data (status, recent activity, role).
      for (const c of constellation.agents) {
        const existing = byName.get(c.name);
        if (existing) {
          existing.role = c.role;
          existing.status = c.status;
          existing.recent_model = c.model;
          existing.runs_24h = c.runs_24h;
          existing.cost_24h_usd = c.cost_24h_usd;
          existing.last_event_at = c.last_event_at;
        } else {
          // Agent in constellation but not in /agents — shouldn't really
          // happen since constellation derives from the same table, but
          // be defensive.
          byName.set(c.name, {
            name: c.name,
            role: c.role,
            status: c.status,
            pinned_provider: null,
            pinned_model: null,
            recent_model: c.model,
            runs_24h: c.runs_24h,
            cost_24h_usd: c.cost_24h_usd,
            tick_interval_s: null,
            last_event_at: c.last_event_at,
          });
        }
      }
      // Sort: orchestrator first, then by activity (most active up top).
      const all = Array.from(byName.values()).sort((a, b) => {
        if (a.role === "orchestrator") return -1;
        if (b.role === "orchestrator") return 1;
        return b.runs_24h - a.runs_24h;
      });
      setRows(all);
      setError(null);
    } catch (e) {
      if (e instanceof UnauthorizedError) {
        router.replace("/login");
        return;
      }
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <main className="px-8 py-10 max-w-6xl mx-auto">
      <div className="flex items-baseline justify-between mb-8">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Agents</h1>
          <p className="text-sm text-gray-500 mt-1">
            Every bot in this workspace. Click a name to open its detail
            page where you can change the pinned model, set a tick
            interval, edit the system prompt, or send a command.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Link
            href="/agents/generate"
            className="px-4 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 no-underline"
          >
            ✨ generate
          </Link>
          <Link
            href="/agents/new"
            className="px-4 py-2 border border-gray-300 text-gray-700 rounded-md text-sm font-medium hover:bg-gray-50 no-underline"
          >
            + drop a zip
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
          <div className="text-gray-700 font-medium mb-2">
            No agents yet
          </div>
          <p className="text-sm text-gray-500 mb-4">
            Deploy a bot or generate one from a description; it will land
            here with its role, pinned model, and recent activity.
          </p>
          <div className="flex items-center justify-center gap-3">
            <Link
              href="/agents/generate"
              className="px-4 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 no-underline"
            >
              ✨ generate from description
            </Link>
            <Link
              href="/getting-started"
              className="px-4 py-2 border border-gray-300 text-gray-700 rounded-md text-sm font-medium hover:bg-gray-50 no-underline"
            >
              Read the guide
            </Link>
          </div>
        </div>
      ) : (
        <div className="rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full text-left text-sm">
            <thead className="bg-gray-50 text-[11px] uppercase tracking-wider text-gray-500">
              <tr>
                <th className="px-4 py-3 font-medium">Name</th>
                <th className="px-4 py-3 font-medium">Role</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Model</th>
                <th className="px-4 py-3 font-medium">Tick</th>
                <th className="px-4 py-3 font-medium">Runs (24h)</th>
                <th className="px-4 py-3 font-medium">Cost (24h)</th>
                <th className="px-4 py-3 font-medium">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr
                  key={r.name}
                  className={
                    "hover:bg-gray-50 transition-colors " +
                    (i !== rows.length - 1 ? "border-b border-gray-100" : "")
                  }
                >
                  <td className="px-4 py-3">
                    <Link
                      href={`/agents/${encodeURIComponent(r.name)}`}
                      className="font-mono text-accent-600 hover:text-accent-700 font-medium"
                    >
                      {r.name}
                    </Link>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-600">
                    {r.role ?? "—"}
                  </td>
                  <td className="px-4 py-3">
                    {r.status === "active" ? (
                      <span className="inline-block px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-100 text-emerald-800">
                        active
                      </span>
                    ) : r.status === "stale" ? (
                      <span className="inline-block px-2 py-0.5 rounded-full text-[10px] font-medium bg-amber-100 text-amber-800">
                        stale
                      </span>
                    ) : r.status === "stopped" ? (
                      <span className="inline-block px-2 py-0.5 rounded-full text-[10px] font-medium bg-gray-100 text-gray-600">
                        stopped
                      </span>
                    ) : (
                      <span className="text-xs text-gray-400">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">
                    {r.pinned_model ? (
                      <span title="pinned via the dashboard">
                        <span className="text-gray-900">{r.pinned_model}</span>
                        {r.pinned_provider && (
                          <span className="text-gray-400">
                            {" "}({r.pinned_provider})
                          </span>
                        )}
                      </span>
                    ) : r.recent_model ? (
                      <span
                        className="text-gray-500"
                        title="auto-detected from recent llm_call_completed event; not pinned"
                      >
                        {r.recent_model}
                      </span>
                    ) : (
                      <span className="text-gray-400">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-700">
                    {fmtInterval(r.tick_interval_s)}
                  </td>
                  <td className="px-4 py-3 text-gray-700 tabular-nums">
                    {r.runs_24h}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-700 tabular-nums">
                    {fmtUsd(r.cost_24h_usd)}
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500">
                    {fmtRelative(r.last_event_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-xs text-gray-400 mt-6">
        &quot;Pinned&quot; model is what you set in each agent&apos;s Model
        section; cron-style bots like Polaris route their LLM calls there
        on the next tick. When unpinned, the column shows the model the SDK
        observed on the most recent <code className="font-mono">llm_call_completed</code>{" "}
        event — purely informational.
      </p>
    </main>
  );
}
