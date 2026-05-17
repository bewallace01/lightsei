"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  AGENT_PROVIDERS,
  Agent,
  AgentInstance,
  AgentManifest,
  AgentProvider,
  AgentQuality,
  cancelCommand,
  Command,
  Deployment,
  enqueueCommand,
  fetchAgent,
  fetchAgentInstances,
  fetchAgentManifest,
  fetchAgentQuality,
  fetchCommands,
  fetchDeployments,
  patchAgent,
  redeployDeployment,
  stopDeployment,
  UnauthorizedError,
} from "../../api";

function fmt(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function statusBadge(status: Command["status"]): string {
  switch (status) {
    case "pending":
      return "bg-amber-100 text-amber-800";
    case "claimed":
      return "bg-accent-100 text-accent-800";
    case "completed":
      return "bg-green-100 text-green-800";
    case "failed":
      return "bg-red-100 text-red-800";
    case "cancelled":
      return "bg-gray-100 text-gray-700";
  }
}

export default function AgentPage({ params }: { params: { name: string } }) {
  const agentName = decodeURIComponent(params.name);
  const router = useRouter();
  const [commands, setCommands] = useState<Command[]>([]);
  const [manifest, setManifest] = useState<AgentManifest | null>(null);
  const [agent, setAgent] = useState<Agent | null>(null);
  const [instances, setInstances] = useState<AgentInstance[]>([]);
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [quality, setQuality] = useState<AgentQuality | null>(null);
  const [systemPromptDraft, setSystemPromptDraft] = useState("");
  const [systemPromptSaving, setSystemPromptSaving] = useState(false);
  const [systemPromptSavedAt, setSystemPromptSavedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [kind, setKind] = useState("ping");
  const [kindCustom, setKindCustom] = useState(false);
  const [payloadText, setPayloadText] = useState("{}");
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      const [cmds, mf, ag, inst, deps, q] = await Promise.all([
        fetchCommands(agentName),
        fetchAgentManifest(agentName),
        fetchAgent(agentName).catch(() => null),
        fetchAgentInstances(agentName).catch(() => []),
        fetchDeployments(agentName).catch(() => [] as Deployment[]),
        fetchAgentQuality(agentName).catch(() => null as AgentQuality | null),
      ]);
      setCommands(cmds);
      setManifest(mf);
      setInstances(inst);
      setDeployments(deps);
      setQuality(q);
      if (ag) {
        setAgent(ag);
        // Only sync the draft if the user hasn't typed since last load.
        setSystemPromptDraft((cur) =>
          cur === (agent?.system_prompt ?? "") || cur === ""
            ? ag.system_prompt ?? ""
            : cur,
        );
      }
      setError(null);
    } catch (e) {
      if (e instanceof UnauthorizedError) {
        router.replace("/login");
        return;
      }
      setError(String(e));
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 2000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentName]);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      let payload: Record<string, unknown> = {};
      const trimmed = payloadText.trim();
      if (trimmed) {
        try {
          const parsed = JSON.parse(trimmed);
          if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
            throw new Error("payload must be a JSON object");
          }
          payload = parsed as Record<string, unknown>;
        } catch (err) {
          setError(`invalid JSON payload: ${(err as Error).message}`);
          setBusy(false);
          return;
        }
      }
      await enqueueCommand(agentName, kind.trim(), payload);
      setKind("ping");
      setPayloadText("{}");
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onCancel = async (id: string) => {
    if (!confirm("Cancel this pending command?")) return;
    try {
      await cancelCommand(id);
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const pending = commands.filter((c) => c.status === "pending");
  const recent = commands.filter((c) => c.status !== "pending");

  return (
    <main className="px-8 py-10 max-w-5xl mx-auto">

      <Link
        href="/"
        className="text-sm text-gray-500 hover:text-accent-600 transition-colors inline-block mb-4"
      >
        ← runs
      </Link>

      <div className="flex items-baseline gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">
          <span className="font-mono">{agentName}</span>
        </h1>
        {(() => {
          const activeCount = instances.filter((i) => i.status === "active").length;
          if (activeCount > 0) {
            return (
              <span
                className="inline-block px-2 py-0.5 rounded-full text-[11px] font-medium bg-green-100 text-green-800"
                title={`${activeCount} active instance(s)`}
              >
                {activeCount === 1 ? "live" : `live · ${activeCount}`}
              </span>
            );
          }
          if (instances.length > 0) {
            return (
              <span
                className="inline-block px-2 py-0.5 rounded-full text-[11px] font-medium bg-gray-100 text-gray-700"
                title="all known instances are stale"
              >
                idle
              </span>
            );
          }
          return null;
        })()}
      </div>
      <p className="text-sm text-gray-500 mt-1 mb-3">
        Send commands to this agent. The bot polls every few seconds and runs
        a registered <code className="font-mono">@lightsei.on_command</code>{" "}
        handler.
      </p>
      <div className="mb-8">
        <Link
          href={`/agents/${encodeURIComponent(agentName)}/chat`}
          className="inline-flex items-center gap-1.5 text-sm text-accent-700 hover:text-accent-800"
        >
          open chat
          <span className="text-xs">→</span>
        </Link>
      </div>

      {instances.length > 0 && (
        <section className="mb-10">
          <h2 className="text-[11px] font-semibold text-gray-500 mb-3 uppercase tracking-wider">
            Instances
          </h2>
          <div className="border border-gray-200 rounded-lg divide-y divide-gray-100">
            {instances.map((i) => (
              <div
                key={i.id}
                className="px-4 py-2.5 flex items-center justify-between text-sm"
              >
                <div className="flex items-center gap-3 min-w-0">
                  <span
                    className={
                      "inline-block px-2 py-0.5 rounded-full text-[10px] font-medium uppercase tracking-wider " +
                      (i.status === "active"
                        ? "bg-green-100 text-green-800"
                        : "bg-gray-100 text-gray-600")
                    }
                  >
                    {i.status}
                  </span>
                  <span className="font-mono text-gray-800 truncate">
                    {i.hostname || "unknown-host"}
                    {i.pid !== null && (
                      <span className="text-gray-400">
                        {" "}· pid {i.pid}
                      </span>
                    )}
                  </span>
                  {i.sdk_version && (
                    <span className="text-[10px] text-gray-400 font-mono">
                      sdk {i.sdk_version}
                    </span>
                  )}
                </div>
                <div className="text-[11px] text-gray-400 font-mono">
                  last seen {fmt(i.last_heartbeat_at)}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {deployments.length > 0 && (
        <section className="mb-10">
          <div className="flex items-baseline justify-between mb-3">
            <h2 className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider">
              Deployments
            </h2>
            <span className="text-[11px] text-gray-400 font-mono">
              upload from CLI: <code>lightsei deploy ./{agentName}</code>
            </span>
          </div>
          <div className="border border-gray-200 rounded-lg divide-y divide-gray-100">
            {deployments.slice(0, 10).map((d) => (
              <div
                key={d.id}
                className="px-4 py-2.5 flex items-center justify-between text-sm gap-3"
              >
                <div className="flex items-center gap-3 min-w-0">
                  {(() => {
                    const cls =
                      d.status === "running"
                        ? "bg-green-100 text-green-800"
                        : d.status === "failed"
                          ? "bg-red-100 text-red-800"
                          : d.status === "stopped"
                            ? "bg-gray-100 text-gray-700"
                            : d.status === "building"
                              ? "bg-amber-100 text-amber-800"
                              : "bg-blue-100 text-blue-800";
                    return (
                      <span
                        className={
                          "inline-block px-2 py-0.5 rounded-full text-[10px] font-medium uppercase tracking-wider " +
                          cls
                        }
                      >
                        {d.status}
                      </span>
                    );
                  })()}
                  <Link
                    href={`/deployments/${d.id}`}
                    className="font-mono text-gray-700 hover:text-accent-700 truncate"
                  >
                    {d.id.slice(0, 8)}…
                  </Link>
                  {d.source === "github_push" ? (
                    <span
                      className="inline-flex items-center gap-1 text-[11px] text-gray-500 font-mono"
                      title={
                        d.source_commit_sha
                          ? `Pushed via GitHub at ${d.source_commit_sha}`
                          : "Pushed via GitHub"
                      }
                    >
                      <span aria-hidden="true">↳</span>
                      <span>github</span>
                      {d.source_commit_sha && (
                        <span className="text-gray-400">
                          {d.source_commit_sha.slice(0, 7)}
                        </span>
                      )}
                    </span>
                  ) : (
                    <span
                      className="text-[11px] text-gray-400 font-mono"
                      title="Uploaded via lightsei CLI"
                    >
                      cli
                    </span>
                  )}
                  {d.error && (
                    <span
                      className="text-[11px] text-red-700 truncate max-w-md"
                      title={d.error}
                    >
                      {d.error}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-3 text-[11px] text-gray-400 font-mono">
                  <span>created {fmt(d.created_at)}</span>
                  {(d.status === "running" ||
                    d.status === "building" ||
                    d.status === "queued") && (
                    <button
                      type="button"
                      onClick={async () => {
                        if (!confirm("Stop this deployment?")) return;
                        try {
                          await stopDeployment(d.id);
                          await load();
                        } catch (e) {
                          setError(String(e));
                        }
                      }}
                      className="text-red-600 hover:text-red-700 font-medium"
                    >
                      stop
                    </button>
                  )}
                  {(d.status === "stopped" ||
                    d.status === "failed") && (
                    <button
                      type="button"
                      onClick={async () => {
                        if (!confirm("Redeploy from this bundle?")) return;
                        try {
                          await redeployDeployment(d.id);
                          await load();
                        } catch (e) {
                          setError(String(e));
                        }
                      }}
                      className="text-accent-700 hover:text-accent-800 font-medium"
                    >
                      redeploy
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Phase 14.4: judge-LLM quality verdicts. Renders the verdict
          breakdown for the last 7 days + the most-recent bads with
          their judge reasons so the user can see _why_ a bot is
          flagged without an extra fetch. Hidden entirely on agents
          with no eval data yet (fresh deploys before the cron has
          sampled anything). */}
      {quality && quality.total_evaluations > 0 && (
        <section className="mb-10">
          <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
            Quality (last {quality.days}d, judge: {
              quality.recent_bads[0]?.judge_model ?? "claude-sonnet-4-6"
            })
          </h2>
          <div className="rounded-lg border border-gray-200 p-4 bg-white">
            <div className="flex items-center gap-3 flex-wrap mb-3">
              <span className="inline-block px-2.5 py-1 rounded-full bg-green-100 text-green-800 text-xs font-medium">
                {quality.verdict_counts.good} good
              </span>
              <span className="inline-block px-2.5 py-1 rounded-full bg-amber-100 text-amber-800 text-xs font-medium">
                {quality.verdict_counts.borderline} borderline
              </span>
              <span className="inline-block px-2.5 py-1 rounded-full bg-red-100 text-red-800 text-xs font-medium">
                {quality.verdict_counts.bad} bad
              </span>
              <span className="text-xs text-gray-500 ml-2">
                {quality.total_evaluations} total
              </span>
              {quality.trend.direction !== "unknown" && (
                <span
                  className={
                    "text-xs ml-2 " +
                    (quality.trend.direction === "up"
                      ? "text-green-700"
                      : quality.trend.direction === "down"
                        ? "text-red-700"
                        : "text-gray-500")
                  }
                  title="Good-rate change vs the prior 7d window"
                >
                  {quality.trend.direction === "up" && "↑ "}
                  {quality.trend.direction === "down" && "↓ "}
                  {quality.trend.direction === "flat" && "→ "}
                  {quality.trend.delta_pp >= 0 ? "+" : ""}
                  {quality.trend.delta_pp}pp vs prior {quality.days}d
                </span>
              )}
            </div>
            {quality.recent_bads.length > 0 ? (
              <div className="mt-3 border-t border-gray-100 pt-3">
                <h3 className="text-xs font-semibold text-gray-700 mb-2">
                  Recent bads
                </h3>
                <ul className="space-y-2">
                  {quality.recent_bads.map((b) => (
                    <li
                      key={b.run_id}
                      className="rounded border border-red-100 bg-red-50/50 px-3 py-2"
                    >
                      <div className="flex items-center justify-between text-xs text-red-900 mb-1">
                        <span>
                          <span className="font-mono">
                            run {b.run_id.slice(0, 8)}…
                          </span>
                          {" · "}
                          {fmt(b.run_started_at ?? b.created_at)}
                        </span>
                        <span className="text-red-700">
                          confidence {Math.round(b.confidence * 100)}%
                        </span>
                      </div>
                      <ul className="text-xs text-red-900 list-disc pl-5">
                        {b.reasons.map((r, i) => (
                          <li key={i}>{r}</li>
                        ))}
                      </ul>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="text-xs text-gray-500 mt-2">
                No bad verdicts in the last {quality.days} days. Nothing
                to investigate.
              </p>
            )}
          </div>
        </section>
      )}

      <section className="mb-10">
        <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
          System prompt
        </h2>
        <p className="text-xs text-gray-500 mb-3">
          Prepended to every chat thread for this agent. Bot doesn&apos;t need
          to do anything — Lightsei delivers it as a <code className="font-mono">system</code>{" "}
          message. Leave blank to disable.
        </p>
        <textarea
          value={systemPromptDraft}
          onChange={(e) => setSystemPromptDraft(e.target.value)}
          rows={4}
          placeholder="You are a helpful assistant…"
          className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm font-sans focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500"
        />
        <div className="flex items-center gap-3 mt-2">
          <button
            type="button"
            disabled={
              systemPromptSaving ||
              systemPromptDraft === (agent?.system_prompt ?? "")
            }
            onClick={async () => {
              setSystemPromptSaving(true);
              try {
                const next = systemPromptDraft.trim() ? systemPromptDraft : null;
                const updated = await patchAgent(agentName, { system_prompt: next });
                setAgent(updated);
                setSystemPromptDraft(updated.system_prompt ?? "");
                setSystemPromptSavedAt(Date.now());
              } catch (e) {
                setError(String(e));
              } finally {
                setSystemPromptSaving(false);
              }
            }}
            className="px-4 py-2 bg-accent-600 hover:bg-accent-700 text-white rounded-md text-sm font-medium disabled:opacity-50 transition-colors"
          >
            {systemPromptSaving ? "saving…" : "save"}
          </button>
          {systemPromptSavedAt && Date.now() - systemPromptSavedAt < 4000 && (
            <span className="text-xs text-green-700">saved.</span>
          )}
          {agent?.system_prompt && (
            <span className="text-xs text-gray-400">
              {agent.system_prompt.length} chars stored
            </span>
          )}
        </div>
      </section>

      <DescriptionSection
        agent={agent}
        onSaved={(updated) => setAgent(updated)}
        onError={(msg) => setError(msg)}
      />

      <ModelSelector
        agent={agent}
        onSaved={(updated) => setAgent(updated)}
        onError={(msg) => setError(msg)}
      />

      <ScheduleSelector
        agent={agent}
        onSaved={(updated) => setAgent(updated)}
        onError={(msg) => setError(msg)}
      />

      <section className="mb-10">
        <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
          Send command
        </h2>
        {(() => {
          const handlers = manifest?.command_handlers ?? [];
          const selected = handlers.find((h) => h.kind === kind);
          return (
            <form onSubmit={onSubmit} className="space-y-3">
              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-1">
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">
                    Kind
                  </label>
                  {kindCustom || handlers.length === 0 ? (
                    <input
                      value={kind}
                      onChange={(e) => setKind(e.target.value)}
                      required
                      placeholder="ping"
                      className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500"
                    />
                  ) : (
                    <select
                      value={kind}
                      onChange={(e) => {
                        const v = e.target.value;
                        if (v === "__custom__") {
                          setKindCustom(true);
                          setKind("");
                        } else {
                          setKind(v);
                        }
                      }}
                      className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500 bg-white"
                    >
                      {handlers.map((h) => (
                        <option key={h.kind} value={h.kind}>
                          {h.kind}
                        </option>
                      ))}
                      <option value="__custom__">other (custom)…</option>
                    </select>
                  )}
                </div>
                <div className="col-span-2">
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">
                    Payload (JSON object)
                  </label>
                  <input
                    value={payloadText}
                    onChange={(e) => setPayloadText(e.target.value)}
                    placeholder="{}"
                    className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500"
                  />
                </div>
              </div>
              {selected?.description && (
                <div className="text-xs text-gray-500">
                  {selected.description}
                </div>
              )}
              {handlers.length === 0 && (
                <div className="text-xs text-gray-500">
                  No registered handlers for this agent yet. Anything you send
                  queues until a bot with a matching{" "}
                  <code className="font-mono">@lightsei.on_command</code>{" "}
                  starts up.
                </div>
              )}
              <div className="flex items-center gap-3">
                <button
                  type="submit"
                  disabled={busy || !kind.trim()}
                  className="px-4 py-2 bg-accent-600 hover:bg-accent-700 text-white rounded-md text-sm font-medium disabled:opacity-50 transition-colors"
                >
                  {busy ? "sending…" : "send"}
                </button>
                {kindCustom && handlers.length > 0 && (
                  <button
                    type="button"
                    onClick={() => {
                      setKindCustom(false);
                      setKind(handlers[0].kind);
                    }}
                    className="text-xs text-gray-500 hover:text-accent-600"
                  >
                    ← pick from registered handlers
                  </button>
                )}
              </div>
            </form>
          );
        })()}
      </section>

      <section className="mb-10">
        <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
          Pending ({pending.length})
        </h2>
        {pending.length === 0 ? (
          <div className="text-sm text-gray-400 italic">no pending commands</div>
        ) : (
          <CommandTable commands={pending} onCancel={onCancel} canCancel />
        )}
      </section>

      <section>
        <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
          Recent
        </h2>
        {recent.length === 0 ? (
          <div className="text-sm text-gray-400 italic">no recent commands</div>
        ) : (
          <CommandTable commands={recent} onCancel={() => {}} canCancel={false} />
        )}
      </section>
    </main>
  );
}

function CommandTable({
  commands,
  onCancel,
  canCancel,
}: {
  commands: Command[];
  onCancel: (id: string) => void;
  canCancel: boolean;
}) {
  return (
    <div className="rounded-lg border border-gray-200 overflow-hidden">
      <table className="w-full text-left text-sm">
        <thead className="bg-gray-50 text-[11px] uppercase tracking-wider text-gray-500">
          <tr>
            <th className="px-4 py-3 font-medium">Kind</th>
            <th className="px-4 py-3 font-medium">Status</th>
            <th className="px-4 py-3 font-medium">Created</th>
            <th className="px-4 py-3 font-medium">Result</th>
            {canCancel && <th className="px-4 py-3 font-medium"></th>}
          </tr>
        </thead>
        <tbody>
          {commands.map((c, i) => (
            <tr
              key={c.id}
              className={i !== commands.length - 1 ? "border-b border-gray-100 align-top" : "align-top"}
            >
              <td className="px-4 py-3 font-mono text-xs text-gray-800">
                {c.kind}
                {Object.keys(c.payload).length > 0 && (
                  <pre className="mt-1 text-[10px] text-gray-500 whitespace-pre-wrap break-words">
                    {JSON.stringify(c.payload)}
                  </pre>
                )}
              </td>
              <td className="px-4 py-3">
                <span
                  className={
                    "inline-block px-2 py-0.5 rounded-full text-[11px] font-medium " +
                    statusBadge(c.status)
                  }
                >
                  {c.status}
                </span>
              </td>
              <td className="px-4 py-3 font-mono text-xs text-gray-600">
                {fmt(c.created_at)}
              </td>
              <td className="px-4 py-3 font-mono text-xs text-gray-700">
                {c.error ? (
                  <span className="text-red-700">{c.error}</span>
                ) : c.result ? (
                  <pre className="whitespace-pre-wrap break-words">
                    {JSON.stringify(c.result, null, 2)}
                  </pre>
                ) : (
                  "—"
                )}
              </td>
              {canCancel && (
                <td className="px-4 py-3 text-right">
                  <button
                    type="button"
                    onClick={() => onCancel(c.id)}
                    className="text-red-600 hover:text-red-700 text-xs font-medium"
                  >
                    cancel
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------- Phase 12.3: provider + model picker ---------- //

function ModelSelector({
  agent,
  onSaved,
  onError,
}: {
  agent: Agent | null;
  onSaved: (updated: Agent) => void;
  onError: (msg: string) => void;
}) {
  const [provider, setProvider] = useState<AgentProvider | "">("");
  const [model, setModel] = useState("");
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  // Mirror server state into the form whenever the agent prop changes,
  // so an external refresh (heartbeat poll) doesn't fight typed input.
  useEffect(() => {
    setProvider(agent?.provider ?? "");
    setModel(agent?.model ?? "");
  }, [agent?.provider, agent?.model]);

  if (!agent) return null;

  const dirty =
    (provider || null) !== (agent.provider ?? null) ||
    (model.trim() || null) !== (agent.model ?? null);

  const onSave = async () => {
    setSaving(true);
    try {
      const updated = await patchAgent(agent.name, {
        provider: provider ? (provider as AgentProvider) : null,
        model: model.trim() ? model.trim() : null,
      });
      onSaved(updated);
      setSavedAt(Date.now());
    } catch (e) {
      onError(String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="mb-10">
      <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
        Model
      </h2>
      <p className="text-xs text-gray-500 mb-3">
        Pin this agent&apos;s LLM provider + model id. Leave both blank to
        let the SDK auto-record whichever model the bot calls. Provider
        is validated server-side; model id is free-form (it&apos;s just a
        string the SDK + cost panel match against).
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <label className="block">
          <span className="text-xs font-medium text-gray-600">Provider</span>
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value as AgentProvider | "")}
            className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none"
          >
            <option value="">— inherit —</option>
            {AGENT_PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <label className="block sm:col-span-2">
          <span className="text-xs font-medium text-gray-600">Model id</span>
          <input
            type="text"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="e.g. gemini-1.5-flash, claude-haiku-4-5, gpt-4o-mini"
            className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm font-mono focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none"
          />
        </label>
      </div>
      <div className="flex items-center gap-3 mt-3">
        <button
          type="button"
          disabled={saving || !dirty}
          onClick={onSave}
          className="px-4 py-2 bg-accent-600 hover:bg-accent-700 text-white rounded-md text-sm font-medium disabled:opacity-50 transition-colors"
        >
          {saving ? "saving…" : "save"}
        </button>
        {savedAt && Date.now() - savedAt < 4000 && (
          <span className="text-xs text-green-700">saved.</span>
        )}
        {agent.provider && agent.model ? (
          <span className="text-xs text-gray-400">
            currently pinned to <span className="font-mono">{agent.provider}</span> ·{" "}
            <span className="font-mono">{agent.model}</span>
          </span>
        ) : agent.provider || agent.model ? (
          <span className="text-xs text-amber-600">
            partial pin — set both fields or clear both
          </span>
        ) : (
          <span className="text-xs text-gray-400">
            no pin set; using whatever the SDK reports
          </span>
        )}
      </div>
    </section>
  );
}

// ---------- Schedule selector (per-agent tick interval) ---------- //

const TICK_PRESETS: { seconds: number; label: string; hint: string }[] = [
  { seconds: 60, label: "every 1 min", hint: "tight feedback for active dev; can burn budget fast on LLM-calling bots" },
  { seconds: 300, label: "every 5 min", hint: "frequent; reasonable for low-cost agents (notifiers, watchers)" },
  { seconds: 900, label: "every 15 min", hint: "balanced; the sweet spot for most schedule-driven LLM agents" },
  { seconds: 3600, label: "every hour", hint: "default; recommended for production planners like Polaris" },
  { seconds: 14400, label: "every 4 hours", hint: "low frequency; documentation maintainers, summarizers" },
  { seconds: 86400, label: "daily", hint: "minimal cost; cron-style daily digests" },
];

function fmtInterval(s: number): string {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${(s / 3600).toFixed(s % 3600 === 0 ? 0 : 1)}h`;
  return `${(s / 86400).toFixed(s % 86400 === 0 ? 0 : 1)}d`;
}

function ScheduleSelector({
  agent,
  onSaved,
  onError,
}: {
  agent: Agent | null;
  onSaved: (updated: Agent) => void;
  onError: (msg: string) => void;
}) {
  const [custom, setCustom] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    setCustom(agent?.tick_interval_s != null ? String(agent.tick_interval_s) : "");
  }, [agent?.tick_interval_s]);

  if (!agent) return null;

  const current = agent.tick_interval_s;

  const apply = async (next: number | null) => {
    setSaving(true);
    try {
      const updated = await patchAgent(agent.name, { tick_interval_s: next });
      onSaved(updated);
      setSavedAt(Date.now());
    } catch (e) {
      onError(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(false);
    }
  };

  const onPreset = (s: number) => apply(s);
  const onClear = () => apply(null);
  const onApplyCustom = () => {
    const n = parseInt(custom, 10);
    if (Number.isNaN(n) || n < 60 || n > 86400) {
      onError("custom interval must be between 60 and 86400 seconds");
      return;
    }
    apply(n);
  };

  return (
    <section className="mb-10">
      <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
        Schedule
      </h2>
      <p className="text-xs text-gray-500 mb-3">
        How often this bot ticks. Cron-style bots (Polaris, future planners) read
        this at the start of each sleep cycle, so a change here takes effect on
        the very next tick — no redeploy. Reactive bots (Atlas, Hermes — they
        claim commands instead of ticking) ignore the setting.
      </p>
      <div className="flex flex-wrap gap-2 mb-3">
        {TICK_PRESETS.map((p) => (
          <button
            key={p.seconds}
            type="button"
            disabled={saving}
            onClick={() => onPreset(p.seconds)}
            title={p.hint}
            className={
              "px-3 py-1.5 text-xs rounded-full border transition-colors " +
              (current === p.seconds
                ? "bg-accent-600 text-white border-accent-600"
                : "bg-white text-gray-700 border-gray-300 hover:border-gray-400")
            }
          >
            {p.label}
          </button>
        ))}
        <button
          type="button"
          disabled={saving || current == null}
          onClick={onClear}
          className="px-3 py-1.5 text-xs rounded-full border border-gray-300 text-gray-600 hover:border-gray-400 disabled:opacity-50"
        >
          use bot default
        </button>
      </div>
      <div className="flex items-baseline gap-3">
        <label className="text-xs text-gray-600 flex items-center gap-2">
          custom (seconds):
          <input
            type="number"
            min={60}
            max={86400}
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
            placeholder="60–86400"
            disabled={saving}
            className="w-28 px-2 py-1 border border-gray-300 rounded text-sm font-mono focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none disabled:opacity-50"
          />
          <button
            type="button"
            onClick={onApplyCustom}
            disabled={saving || !custom.trim()}
            className="px-3 py-1 text-xs bg-accent-600 text-white rounded hover:bg-accent-700 disabled:opacity-50"
          >
            apply
          </button>
        </label>
        {savedAt && Date.now() - savedAt < 4000 && (
          <span className="text-xs text-green-700">saved.</span>
        )}
        <span className="text-xs text-gray-400 ml-auto">
          {current != null
            ? `currently set to ${fmtInterval(current)}`
            : "no override; using bot env default"}
        </span>
      </div>
    </section>
  );
}

// ---------- Description editor ---------- //

function DescriptionSection({
  agent,
  onSaved,
  onError,
}: {
  agent: Agent | null;
  onSaved: (updated: Agent) => void;
  onError: (msg: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    setDraft(agent?.description ?? "");
  }, [agent?.description]);

  if (!agent) return null;

  const dirty = (draft.trim() || null) !== (agent.description ?? null);

  const onSave = async () => {
    setSaving(true);
    try {
      const updated = await patchAgent(agent.name, {
        description: draft.trim() ? draft.trim() : null,
      });
      onSaved(updated);
      setSavedAt(Date.now());
    } catch (e) {
      onError(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="mb-10">
      <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
        Description
      </h2>
      <p className="text-xs text-gray-500 mb-3">
        One-line summary shown on the /agents roster. Generated bots get
        this auto-populated from the LLM&apos;s rationale; for hand-
        deployed bots, write your own. Markdown not rendered.
      </p>
      <textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        rows={2}
        placeholder="e.g. Scans every push for hardcoded secrets and dispatches alerts via hermes."
        disabled={saving}
        className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500 disabled:opacity-50"
      />
      <div className="flex items-center gap-3 mt-2">
        <button
          type="button"
          onClick={onSave}
          disabled={saving || !dirty}
          className="px-4 py-1.5 bg-accent-600 hover:bg-accent-700 text-white rounded-md text-sm font-medium disabled:opacity-50 transition-colors"
        >
          {saving ? "saving…" : "save"}
        </button>
        {savedAt && Date.now() - savedAt < 4000 && (
          <span className="text-xs text-green-700">saved.</span>
        )}
      </div>
    </section>
  );
}
