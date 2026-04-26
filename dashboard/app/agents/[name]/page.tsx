"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  Agent,
  AgentManifest,
  cancelCommand,
  Command,
  enqueueCommand,
  fetchAgent,
  fetchAgentManifest,
  fetchCommands,
  patchAgent,
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
      const [cmds, mf, ag] = await Promise.all([
        fetchCommands(agentName),
        fetchAgentManifest(agentName),
        fetchAgent(agentName).catch(() => null),
      ]);
      setCommands(cmds);
      setManifest(mf);
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
          const lastSeen = manifest?.last_seen_at
            ? new Date(manifest.last_seen_at).getTime()
            : null;
          if (lastSeen === null) return null;
          // Heuristic: a bot "checked in" via manifest at init OR via a poll.
          // Without per-poll heartbeats we just show when it last checked in.
          const ageSec = (Date.now() - lastSeen) / 1000;
          const live = ageSec < 60;
          return (
            <span
              className={
                "inline-block px-2 py-0.5 rounded-full text-[11px] font-medium " +
                (live
                  ? "bg-green-100 text-green-800"
                  : "bg-gray-100 text-gray-700")
              }
              title={`last seen ${manifest!.last_seen_at}`}
            >
              {live ? "live" : "idle"}
            </span>
          );
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

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
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
