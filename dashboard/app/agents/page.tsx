"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  AGENT_PROVIDERS,
  Agent,
  AgentProvider,
  ConstellationAgent,
  UnauthorizedError,
  deleteAgent,
  fetchAgents,
  fetchConstellation,
  patchAgent,
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
  description: string | null;
  last_event_at: string | null;
};


export default function AgentsPage() {
  const router = useRouter();
  const [rows, setRows] = useState<AgentRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const onDelete = async (name: string) => {
    if (
      !confirm(
        `Delete agent "${name}"?\n\n` +
        "Past runs / events / commands stay visible on /runs and /dispatch " +
        "as audit trail — only the agent row is removed. If a bot under this " +
        "name emits an event later, the row gets re-created automatically.",
      )
    ) {
      return;
    }
    try {
      await deleteAgent(name);
      await refresh();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

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
          description: a.description,
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
            description: null,
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
      <div className="flex items-start justify-between gap-6 mb-8">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight">Agents</h1>
          <p className="text-sm text-gray-500 mt-1">
            Every bot in this workspace. Click a name for the full detail
            page; edit the model or tick interval directly from the rows
            below.
          </p>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <Link
            href="/agents/generate"
            className="px-4 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 no-underline whitespace-nowrap"
          >
            ✨ generate
          </Link>
          <Link
            href="/agents/new"
            className="px-4 py-2 border border-gray-300 text-gray-700 rounded-md text-sm font-medium hover:bg-gray-50 no-underline whitespace-nowrap"
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
                <th className="px-4 py-3 font-medium text-right"></th>
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
                  <td className="px-4 py-3 max-w-xs">
                    <Link
                      href={`/agents/${encodeURIComponent(r.name)}`}
                      className="font-mono text-accent-600 hover:text-accent-700 font-medium"
                    >
                      {r.name}
                    </Link>
                    {r.description && (
                      <div
                        className="text-xs text-gray-500 mt-0.5 line-clamp-2"
                        title={r.description}
                      >
                        {r.description}
                      </div>
                    )}
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
                    <ModelCell
                      name={r.name}
                      pinnedProvider={r.pinned_provider}
                      pinnedModel={r.pinned_model}
                      recentModel={r.recent_model}
                      onSaved={refresh}
                      onError={(msg) => setError(msg)}
                    />
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-700">
                    <TickCell
                      name={r.name}
                      tickIntervalS={r.tick_interval_s}
                      onSaved={refresh}
                      onError={(msg) => setError(msg)}
                    />
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
                  <td className="px-4 py-3 text-right">
                    <button
                      type="button"
                      onClick={() => onDelete(r.name)}
                      className="text-xs text-gray-400 hover:text-red-600 transition-colors"
                      title={`Remove ${r.name} from the roster (history kept)`}
                    >
                      delete
                    </button>
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

// ---------- Inline model edit cell ---------- //

function ModelCell({
  name,
  pinnedProvider,
  pinnedModel,
  recentModel,
  onSaved,
  onError,
}: {
  name: string;
  pinnedProvider: string | null;
  pinnedModel: string | null;
  recentModel: string | null;
  onSaved: () => void;
  onError: (msg: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [provider, setProvider] = useState<AgentProvider | "">(
    (pinnedProvider as AgentProvider) || "",
  );
  const [model, setModel] = useState(pinnedModel ?? "");
  const [saving, setSaving] = useState(false);

  // Sync from props when they change (e.g. after a successful save the
  // parent refresh updates the pinned_* values).
  useEffect(() => {
    setProvider((pinnedProvider as AgentProvider) || "");
    setModel(pinnedModel ?? "");
  }, [pinnedProvider, pinnedModel]);

  const onSave = async () => {
    setSaving(true);
    try {
      // Both blank → clear pin. Otherwise both required (the inline cell
      // doesn't support partial pins; the agent detail page does).
      if (!provider && !model.trim()) {
        await patchAgent(name, { provider: null, model: null });
      } else if (!provider || !model.trim()) {
        onError("set both provider and model, or clear both");
        setSaving(false);
        return;
      } else {
        await patchAgent(name, {
          provider: provider as AgentProvider,
          model: model.trim(),
        });
      }
      setEditing(false);
      onSaved();
    } catch (e) {
      onError(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(false);
    }
  };

  const onCancel = () => {
    setProvider((pinnedProvider as AgentProvider) || "");
    setModel(pinnedModel ?? "");
    setEditing(false);
  };

  if (editing) {
    return (
      <div className="flex items-center gap-1.5">
        <select
          value={provider}
          onChange={(e) => setProvider(e.target.value as AgentProvider | "")}
          disabled={saving}
          className="px-1.5 py-0.5 text-xs border border-gray-300 rounded font-mono"
        >
          <option value="">— provider —</option>
          {AGENT_PROVIDERS.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
        <input
          type="text"
          value={model}
          onChange={(e) => setModel(e.target.value)}
          disabled={saving}
          placeholder="model id"
          className="w-40 px-1.5 py-0.5 text-xs border border-gray-300 rounded font-mono"
          autoFocus
          onKeyDown={(e) => {
            if (e.key === "Enter") onSave();
            if (e.key === "Escape") onCancel();
          }}
        />
        <button
          type="button"
          onClick={onSave}
          disabled={saving}
          className="px-2 py-0.5 text-[11px] bg-accent-600 text-white rounded hover:bg-accent-700 disabled:opacity-50"
        >
          {saving ? "…" : "save"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={saving}
          className="text-[11px] text-gray-400 hover:text-gray-700"
        >
          ×
        </button>
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      className="text-left group"
      title="click to change pinned model"
    >
      {pinnedModel ? (
        <span>
          <span className="text-gray-900 group-hover:text-accent-600">
            {pinnedModel}
          </span>
          {pinnedProvider && (
            <span className="text-gray-400"> ({pinnedProvider})</span>
          )}
        </span>
      ) : recentModel ? (
        <span
          className="text-gray-500 group-hover:text-accent-600"
          title="auto-detected from recent llm_call_completed event; not pinned. Click to pin."
        >
          {recentModel}
        </span>
      ) : (
        <span className="text-gray-400 group-hover:text-accent-600">— pin?</span>
      )}
    </button>
  );
}

// ---------- Inline tick-interval edit cell ---------- //

// Same six options as the per-agent detail page's ScheduleSelector, in
// the same order, so the two surfaces feel like one knob viewed from two
// places. Hints intentionally short — the detail page has the longer
// guidance.
const TICK_PRESETS: { seconds: number; label: string }[] = [
  { seconds: 60, label: "1m" },
  { seconds: 300, label: "5m" },
  { seconds: 900, label: "15m" },
  { seconds: 3600, label: "1h" },
  { seconds: 14400, label: "4h" },
  { seconds: 86400, label: "daily" },
];

function TickCell({
  name,
  tickIntervalS,
  onSaved,
  onError,
}: {
  name: string;
  tickIntervalS: number | null;
  onSaved: () => void;
  onError: (msg: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [custom, setCustom] = useState<string>("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setCustom(tickIntervalS != null ? String(tickIntervalS) : "");
  }, [tickIntervalS]);

  const apply = async (next: number | null) => {
    setSaving(true);
    try {
      await patchAgent(name, { tick_interval_s: next });
      setEditing(false);
      onSaved();
    } catch (e) {
      onError(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(false);
    }
  };

  const onApplyCustom = () => {
    const n = parseInt(custom, 10);
    if (Number.isNaN(n) || n < 60 || n > 86400) {
      onError("custom interval must be between 60 and 86400 seconds");
      return;
    }
    apply(n);
  };

  const onCancel = () => {
    setCustom(tickIntervalS != null ? String(tickIntervalS) : "");
    setEditing(false);
  };

  if (editing) {
    return (
      <div className="flex flex-wrap items-center gap-1">
        {TICK_PRESETS.map((p) => (
          <button
            key={p.seconds}
            type="button"
            disabled={saving}
            onClick={() => apply(p.seconds)}
            className={
              "px-2 py-0.5 text-[11px] rounded-full border transition-colors " +
              (tickIntervalS === p.seconds
                ? "bg-accent-600 text-white border-accent-600"
                : "bg-white text-gray-700 border-gray-300 hover:border-gray-400")
            }
          >
            {p.label}
          </button>
        ))}
        <input
          type="number"
          min={60}
          max={86400}
          value={custom}
          onChange={(e) => setCustom(e.target.value)}
          disabled={saving}
          placeholder="sec"
          className="w-16 px-1.5 py-0.5 text-[11px] border border-gray-300 rounded font-mono"
          onKeyDown={(e) => {
            if (e.key === "Enter") onApplyCustom();
            if (e.key === "Escape") onCancel();
          }}
        />
        <button
          type="button"
          onClick={onApplyCustom}
          disabled={saving || !custom.trim()}
          className="px-2 py-0.5 text-[11px] bg-accent-600 text-white rounded hover:bg-accent-700 disabled:opacity-50"
        >
          {saving ? "…" : "save"}
        </button>
        <button
          type="button"
          onClick={() => apply(null)}
          disabled={saving || tickIntervalS == null}
          className="px-2 py-0.5 text-[11px] text-gray-500 hover:text-gray-900 disabled:opacity-40"
          title="clear override; bot uses its built-in default"
        >
          clear
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={saving}
          className="text-[11px] text-gray-400 hover:text-gray-700"
        >
          ×
        </button>
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      className="text-left group"
      title="click to change tick interval"
    >
      {tickIntervalS != null ? (
        <span className="text-gray-900 group-hover:text-accent-600">
          {fmtInterval(tickIntervalS)}
        </span>
      ) : (
        <span className="text-gray-400 group-hover:text-accent-600">—</span>
      )}
    </button>
  );
}
