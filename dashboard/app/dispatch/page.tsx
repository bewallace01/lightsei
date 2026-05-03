"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  AutoApprovalRule,
  DispatchChainDetail,
  DispatchChainStatus,
  DispatchChainSummary,
  DispatchCommand,
  DispatchEvent,
  UnauthorizedError,
  approveCommand,
  deleteAutoApprovalRule,
  fetchAutoApprovalRules,
  fetchDispatchChain,
  fetchDispatchChains,
  rejectCommand,
  upsertAutoApprovalRule,
} from "../api";

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return iso;
  }
}

function fmtRelative(iso: string): string {
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
    return "";
  }
}

const STATUS_STYLES: Record<DispatchChainStatus, string> = {
  pending: "bg-gray-100 text-gray-700",
  pending_approval: "bg-amber-100 text-amber-800",
  running: "bg-blue-100 text-blue-800",
  done: "bg-emerald-100 text-emerald-800",
  failed: "bg-red-100 text-red-800",
  expired: "bg-gray-100 text-gray-500",
  rejected: "bg-rose-100 text-rose-800",
};

function StatusPill({ status }: { status: DispatchChainStatus }) {
  return (
    <span
      className={
        "inline-block px-2 py-0.5 rounded-full text-[11px] font-medium " +
        (STATUS_STYLES[status] ?? "bg-gray-100 text-gray-700")
      }
    >
      {status.replace("_", " ")}
    </span>
  );
}

// ---------- top-level page ---------- //

export default function DispatchPage() {
  const router = useRouter();
  const [chains, setChains] = useState<DispatchChainSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showRules, setShowRules] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await fetchDispatchChains();
      setChains(data);
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
  }, [router]);

  useEffect(() => {
    let alive = true;
    refresh();
    const id = setInterval(() => {
      if (alive) refresh();
    }, 3000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [refresh]);

  return (
    <main className="px-8 py-10 max-w-6xl mx-auto">
      <div className="flex items-baseline justify-between mb-8">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Dispatch</h1>
          <p className="text-sm text-gray-500 mt-1">
            One row per cause-and-effect chain. Click to expand the timeline.
          </p>
        </div>
        <div className="flex items-center gap-4">
          <span className="text-xs text-gray-400">refreshes every 3s</span>
          <button
            type="button"
            onClick={() => setShowRules((v) => !v)}
            className="text-sm text-accent-600 hover:text-accent-700"
          >
            {showRules ? "hide rules" : "auto-approval rules"}
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {showRules && <AutoApprovalRulesPanel onChange={refresh} />}

      {loading ? (
        <div className="text-gray-400 text-sm">loading…</div>
      ) : chains.length === 0 ? (
        <div className="border border-dashed border-gray-200 rounded-lg p-10 text-center">
          <div className="text-gray-700 font-medium mb-1">No dispatch chains yet</div>
          <p className="text-sm text-gray-500">
            Push a commit, send a command from the dashboard, or wait for
            Polaris's next tick. Anything that fans out into agent-to-agent
            commands shows up here.
          </p>
        </div>
      ) : (
        <div className="rounded-lg border border-gray-200 overflow-hidden">
          {chains.map((c, i) => (
            <ChainRow
              key={c.chain_id}
              chain={c}
              isLast={i === chains.length - 1}
              expanded={expanded === c.chain_id}
              onToggle={() =>
                setExpanded((cur) => (cur === c.chain_id ? null : c.chain_id))
              }
              onAction={refresh}
            />
          ))}
        </div>
      )}
    </main>
  );
}

// ---------- one chain row + expanded timeline ---------- //

function ChainRow({
  chain,
  isLast,
  expanded,
  onToggle,
  onAction,
}: {
  chain: DispatchChainSummary;
  isLast: boolean;
  expanded: boolean;
  onToggle: () => void;
  onAction: () => void;
}) {
  return (
    <div className={isLast ? "" : "border-b border-gray-100"}>
      <button
        type="button"
        onClick={onToggle}
        className="w-full text-left px-4 py-3 hover:bg-gray-50 transition-colors flex items-center gap-4"
      >
        <span className="text-gray-400 text-xs w-3">
          {expanded ? "▾" : "▸"}
        </span>
        <span className="flex-1 min-w-0">
          <span className="font-mono text-xs text-gray-500">
            {chain.root_agent} · {chain.root_kind}
          </span>
          <span className="ml-3 text-xs text-gray-400">
            {chain.command_count} cmd{chain.command_count === 1 ? "" : "s"}
            {chain.max_depth > 0 ? ` · depth ${chain.max_depth}` : ""}
          </span>
        </span>
        {chain.pending_approval_count > 0 && (
          <span className="inline-block px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-100 text-amber-800">
            {chain.pending_approval_count} pending
          </span>
        )}
        <StatusPill status={chain.status} />
        <span className="text-xs text-gray-400 w-20 text-right tabular-nums">
          {fmtRelative(chain.last_activity_at)}
        </span>
      </button>
      {expanded && <ChainTimeline chainId={chain.chain_id} onAction={onAction} />}
    </div>
  );
}

function ChainTimeline({
  chainId,
  onAction,
}: {
  chainId: string;
  onAction: () => void;
}) {
  const [detail, setDetail] = useState<DispatchChainDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const d = await fetchDispatchChain(chainId);
      setDetail(d);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [chainId]);

  useEffect(() => {
    refresh();
    // The parent polls the chain list every 3s; this expanded detail
    // tracks the same cadence so a freshly approved command flips state
    // promptly while the row is open.
    const id = setInterval(refresh, 3000);
    return () => clearInterval(id);
  }, [refresh]);

  const onApprove = async (cmdId: string) => {
    setBusy(cmdId);
    try {
      await approveCommand(cmdId);
      await refresh();
      onAction();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const onReject = async (cmdId: string) => {
    setBusy(cmdId);
    try {
      await rejectCommand(cmdId);
      await refresh();
      onAction();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  if (error) {
    return (
      <div className="px-12 py-3 text-sm text-red-700 bg-red-50 border-t border-red-100">
        {error}
      </div>
    );
  }
  if (!detail) {
    return (
      <div className="px-12 py-3 text-xs text-gray-400">loading timeline…</div>
    );
  }

  // Group events under their command for inline rendering. Events
  // without a command_id won't appear here (the backend already filters
  // them), but be defensive.
  const eventsByCmd = new Map<string, DispatchEvent[]>();
  for (const ev of detail.events) {
    if (!ev.command_id) continue;
    const arr = eventsByCmd.get(ev.command_id) ?? [];
    arr.push(ev);
    eventsByCmd.set(ev.command_id, arr);
  }

  return (
    <div className="bg-gray-50 border-t border-gray-100 px-4 py-3 space-y-2">
      <div className="text-[11px] text-gray-400 font-mono">
        chain {detail.chain_id}
      </div>
      {detail.commands.map((cmd) => (
        <CommandRow
          key={cmd.id}
          cmd={cmd}
          events={eventsByCmd.get(cmd.id) ?? []}
          busy={busy === cmd.id}
          onApprove={() => onApprove(cmd.id)}
          onReject={() => onReject(cmd.id)}
        />
      ))}
    </div>
  );
}

function CommandRow({
  cmd,
  events,
  busy,
  onApprove,
  onReject,
}: {
  cmd: DispatchCommand;
  events: DispatchEvent[];
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
}) {
  const indent = Math.min(cmd.dispatch_depth, 6) * 24;
  const isPending = cmd.approval_state === "pending";
  return (
    <div className="bg-white rounded border border-gray-200 px-3 py-2">
      <div
        className="flex items-baseline gap-3"
        style={{ paddingLeft: `${indent}px` }}
      >
        <span className="text-[10px] text-gray-400 tabular-nums w-16">
          {fmtTime(cmd.created_at)}
        </span>
        <span className="font-mono text-xs text-gray-700">
          {cmd.source_agent ? `${cmd.source_agent} → ` : ""}
          <span className="font-medium">{cmd.agent_name}</span>
          <span className="text-gray-500"> · {cmd.kind}</span>
        </span>
        <span className="flex-1" />
        <ApprovalChip state={cmd.approval_state} />
        <CommandStatusChip status={cmd.status} />
        {isPending && (
          <span className="flex items-center gap-1">
            <button
              type="button"
              disabled={busy}
              onClick={onApprove}
              className="px-2 py-0.5 text-[11px] font-medium rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
            >
              approve
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={onReject}
              className="px-2 py-0.5 text-[11px] font-medium rounded border border-gray-300 text-gray-700 hover:bg-gray-100 disabled:opacity-50"
            >
              reject
            </button>
          </span>
        )}
      </div>
      <details
        className="mt-1 text-xs"
        style={{ paddingLeft: `${indent + 80}px` }}
      >
        <summary className="cursor-pointer text-gray-500 hover:text-gray-700 select-none">
          payload
          {cmd.error ? " + error" : ""}
          {cmd.result ? " + result" : ""}
          {events.length > 0
            ? ` + ${events.length} event${events.length === 1 ? "" : "s"}`
            : ""}
        </summary>
        {cmd.error && (
          <div className="mt-1">
            <div className="text-[10px] text-red-600 font-mono">error</div>
            <pre className="font-mono text-[11px] bg-red-50 border border-red-100 p-2 rounded text-red-800 overflow-x-auto whitespace-pre-wrap">
              {cmd.error}
            </pre>
          </div>
        )}
        {cmd.result && (
          <div className="mt-1">
            <div className="text-[10px] text-gray-500 font-mono">result</div>
            <pre className="font-mono text-[11px] bg-gray-50 p-2 rounded text-gray-700 overflow-x-auto">
              {JSON.stringify(cmd.result, null, 2)}
            </pre>
          </div>
        )}
        <div className="mt-1">
          <div className="text-[10px] text-gray-500 font-mono">payload</div>
          <pre className="font-mono text-[11px] bg-gray-50 p-2 rounded text-gray-700 overflow-x-auto">
            {JSON.stringify(cmd.payload, null, 2)}
          </pre>
        </div>
        {events.map((ev) => (
          <div key={ev.id} className="mt-1">
            <div className="text-[10px] text-gray-500 font-mono">
              {fmtTime(ev.timestamp)} · {ev.kind}
            </div>
            <pre className="font-mono text-[11px] bg-gray-50 p-2 rounded text-gray-700 overflow-x-auto">
              {JSON.stringify(ev.payload, null, 2)}
            </pre>
          </div>
        ))}
      </details>
    </div>
  );
}

function ApprovalChip({ state }: { state: string }) {
  if (state === "auto_approved") {
    return (
      <span className="inline-block px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-50 text-emerald-700 border border-emerald-100">
        auto
      </span>
    );
  }
  if (state === "approved") {
    return (
      <span className="inline-block px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-100 text-emerald-800">
        approved
      </span>
    );
  }
  if (state === "pending") {
    return (
      <span className="inline-block px-2 py-0.5 rounded-full text-[10px] font-medium bg-amber-100 text-amber-800">
        pending
      </span>
    );
  }
  if (state === "rejected") {
    return (
      <span className="inline-block px-2 py-0.5 rounded-full text-[10px] font-medium bg-rose-100 text-rose-800">
        rejected
      </span>
    );
  }
  return (
    <span className="inline-block px-2 py-0.5 rounded-full text-[10px] font-medium bg-gray-100 text-gray-600">
      {state}
    </span>
  );
}

function CommandStatusChip({ status }: { status: string }) {
  const palette: Record<string, string> = {
    pending: "bg-gray-100 text-gray-600",
    claimed: "bg-blue-50 text-blue-700",
    running: "bg-blue-100 text-blue-800",
    done: "bg-emerald-100 text-emerald-800",
    failed: "bg-red-100 text-red-800",
    expired: "bg-gray-100 text-gray-500",
  };
  return (
    <span
      className={
        "inline-block px-2 py-0.5 rounded-full text-[10px] font-medium " +
        (palette[status] ?? "bg-gray-100 text-gray-600")
      }
    >
      {status}
    </span>
  );
}

// ---------- auto-approval rules side panel ---------- //

function AutoApprovalRulesPanel({ onChange }: { onChange: () => void }) {
  const [rules, setRules] = useState<AutoApprovalRule[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState({
    source_agent: "",
    target_agent: "",
    command_kind: "",
    mode: "auto_approve" as "auto_approve" | "require_human",
  });

  const refresh = useCallback(async () => {
    try {
      setRules(await fetchAutoApprovalRules());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onAdd = async () => {
    try {
      await upsertAutoApprovalRule(draft);
      setDraft({
        source_agent: "",
        target_agent: "",
        command_kind: "",
        mode: "auto_approve",
      });
      await refresh();
      onChange();
    } catch (e) {
      setError(String(e));
    }
  };

  const onDelete = async (rule: AutoApprovalRule) => {
    try {
      await deleteAutoApprovalRule({
        source_agent: rule.source_agent,
        target_agent: rule.target_agent,
        command_kind: rule.command_kind,
      });
      await refresh();
      onChange();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="mb-6 rounded-lg border border-gray-200 bg-white p-4">
      <div className="flex items-baseline justify-between mb-3">
        <h2 className="text-sm font-semibold text-gray-800">
          Auto-approval rules
        </h2>
        <p className="text-xs text-gray-500">
          (source, target, kind) tuples that skip the human-in-the-loop gate.
        </p>
      </div>
      {error && (
        <div className="mb-3 p-2 text-xs rounded border border-red-200 bg-red-50 text-red-700">
          {error}
        </div>
      )}

      <div className="grid grid-cols-5 gap-2 mb-2 text-xs">
        <input
          placeholder="source agent (* for any)"
          value={draft.source_agent}
          onChange={(e) =>
            setDraft({ ...draft, source_agent: e.target.value })
          }
          className="px-2 py-1 border border-gray-300 rounded"
        />
        <input
          placeholder="target agent"
          value={draft.target_agent}
          onChange={(e) =>
            setDraft({ ...draft, target_agent: e.target.value })
          }
          className="px-2 py-1 border border-gray-300 rounded"
        />
        <input
          placeholder="command kind (* for any)"
          value={draft.command_kind}
          onChange={(e) =>
            setDraft({ ...draft, command_kind: e.target.value })
          }
          className="px-2 py-1 border border-gray-300 rounded"
        />
        <select
          value={draft.mode}
          onChange={(e) =>
            setDraft({
              ...draft,
              mode: e.target.value as "auto_approve" | "require_human",
            })
          }
          className="px-2 py-1 border border-gray-300 rounded"
        >
          <option value="auto_approve">auto_approve</option>
          <option value="require_human">require_human</option>
        </select>
        <button
          type="button"
          onClick={onAdd}
          disabled={
            !draft.source_agent ||
            !draft.target_agent ||
            !draft.command_kind
          }
          className="px-3 py-1 bg-accent-600 text-white rounded hover:bg-accent-700 disabled:opacity-50"
        >
          add / update
        </button>
      </div>

      {rules.length === 0 ? (
        <div className="text-xs text-gray-500 mt-2">
          No rules yet. Every agent-to-agent dispatch lands at `pending` until
          someone clicks approve.
        </div>
      ) : (
        <table className="w-full text-xs mt-2">
          <thead className="text-[11px] text-gray-500 uppercase tracking-wider">
            <tr>
              <th className="text-left py-1">source</th>
              <th className="text-left py-1">target</th>
              <th className="text-left py-1">kind</th>
              <th className="text-left py-1">mode</th>
              <th className="text-right py-1"></th>
            </tr>
          </thead>
          <tbody>
            {rules.map((r, i) => (
              <tr
                key={`${r.source_agent}|${r.target_agent}|${r.command_kind}`}
                className={i !== rules.length - 1 ? "border-b border-gray-100" : ""}
              >
                <td className="font-mono py-1">{r.source_agent}</td>
                <td className="font-mono py-1">{r.target_agent}</td>
                <td className="font-mono py-1">{r.command_kind}</td>
                <td className="py-1">
                  <span
                    className={
                      "inline-block px-2 py-0.5 rounded-full text-[10px] font-medium " +
                      (r.mode === "auto_approve"
                        ? "bg-emerald-100 text-emerald-800"
                        : "bg-amber-100 text-amber-800")
                    }
                  >
                    {r.mode}
                  </span>
                </td>
                <td className="text-right py-1">
                  <button
                    type="button"
                    onClick={() => onDelete(r)}
                    className="text-gray-400 hover:text-red-600 text-[11px]"
                  >
                    delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
