"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  Agent,
  AgentInstance,
  AgentManifest,
  cancelCommand,
  Command,
  Deployment,
  enqueueCommand,
  fetchAgent,
  fetchAgentInstances,
  fetchAgentManifest,
  fetchCommands,
  fetchDeployments,
  patchAgent,
  redeployDeployment,
  stopDeployment,
  UnauthorizedError,
} from "../../api";
import Header from "../../Header";

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
      const [cmds, mf, ag, inst, deps] = await Promise.all([
        fetchCommands(agentName),
        fetchAgentManifest(agentName),
        fetchAgent(agentName).catch(() => null),
        fetchAgentInstances(agentName).catch(() => []),
        fetchDeployments(agentName).catch(() => [] as Deployment[]),
      ]);
      setCommands(cmds);
      setManifest(mf);
      setInstances(inst);
      setDeployments(deps);
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
      <Header />

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
