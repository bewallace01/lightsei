"use client";

import { useEffect, useState } from "react";

import {
  API_URL,
  Trigger,
  TriggerWithToken,
  createAgentTrigger,
  deleteTrigger,
  listAgentTriggers,
  patchTrigger,
  previewSchedule,
} from "../../api";

type Props = {
  agentName: string;
};

const PRESETS: { key: string; label: string; cron: string }[] = [
  { key: "daily", label: "Every day at 9am", cron: "0 9 * * *" },
  { key: "weekdays", label: "Weekdays at 9am", cron: "0 9 * * 1-5" },
  { key: "weekly", label: "Mondays at 9am", cron: "0 9 * * 1" },
  { key: "hourly", label: "Every hour", cron: "0 * * * *" },
];

function fmtRelative(iso: string | null): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  const now = Date.now();
  const diff = t - now;
  const abs = Math.abs(diff);
  const mins = Math.round(abs / 60_000);
  const hours = Math.round(abs / 3_600_000);
  const days = Math.round(abs / 86_400_000);
  let body: string;
  if (abs < 60_000) body = "moments";
  else if (mins < 60) body = `${mins}m`;
  else if (hours < 48) body = `${hours}h`;
  else body = `${days}d`;
  return diff >= 0 ? `in ${body}` : `${body} ago`;
}

function fmtFull(iso: string): string {
  return new Date(iso).toLocaleString();
}

function statusPillClass(status: string | null): string {
  switch (status) {
    case "succeeded":
      return "bg-green-50 text-green-700 ring-green-600/20";
    case "failed":
      return "bg-red-50 text-red-700 ring-red-600/20";
    case "dispatched":
    case "queued":
      return "bg-blue-50 text-blue-700 ring-blue-600/20";
    case "agent_missing":
      return "bg-amber-50 text-amber-700 ring-amber-600/20";
    default:
      return "bg-gray-50 text-gray-600 ring-gray-500/20";
  }
}

function scheduleLabel(schedule: string): string {
  const match = PRESETS.find((p) => p.cron === schedule);
  return match ? match.label : schedule;
}

export default function TriggersPanel({ agentName }: Props) {
  const [triggers, setTriggers] = useState<Trigger[] | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      setError(null);
      const rows = await listAgentTriggers(agentName);
      setTriggers(rows);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentName]);

  async function onToggle(t: Trigger) {
    try {
      const updated = await patchTrigger(t.id, { enabled: !t.enabled });
      setTriggers((rows) =>
        (rows ?? []).map((r) => (r.id === t.id ? updated : r)),
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function onDelete(t: Trigger) {
    if (
      !window.confirm(
        `Delete trigger "${t.name}"? Past runs stay in /runs; the bot keeps the badge via a snapshot.`,
      )
    ) {
      return;
    }
    try {
      await deleteTrigger(t.id);
      setTriggers((rows) => (rows ?? []).filter((r) => r.id !== t.id));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <section className="mb-10">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider">
          Triggers
        </h2>
        <button
          type="button"
          onClick={() => setShowModal(true)}
          className="text-sm px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-500"
        >
          + New trigger
        </button>
      </div>

      {error && (
        <div className="mb-3 text-sm text-red-600 bg-red-50 ring-1 ring-red-200 rounded px-3 py-2">
          {error}
        </div>
      )}

      {triggers === null ? (
        <div className="text-sm text-gray-400 italic">loading…</div>
      ) : triggers.length === 0 ? (
        <div className="text-sm text-gray-500">
          No triggers yet. A trigger fires this bot on a schedule (cron) or
          via a webhook POST.
        </div>
      ) : (
        <ul className="divide-y divide-gray-200 ring-1 ring-gray-200 rounded-md">
          {triggers.map((t) => (
            <li key={t.id} className="px-4 py-3 flex items-center gap-4">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-900 truncate">
                    {t.name}
                  </span>
                  <span
                    className={
                      "text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ring-1 " +
                      (t.kind === "cron"
                        ? "bg-violet-50 text-violet-700 ring-violet-600/20"
                        : "bg-sky-50 text-sky-700 ring-sky-600/20")
                    }
                  >
                    {t.kind}
                  </span>
                  {t.last_run_status && (
                    <span
                      className={
                        "text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ring-1 " +
                        statusPillClass(t.last_run_status)
                      }
                      title={`last_run_status=${t.last_run_status}`}
                    >
                      {t.last_run_status}
                    </span>
                  )}
                </div>
                <div className="text-xs text-gray-500 mt-0.5">
                  {t.kind === "cron" && t.schedule ? (
                    <>
                      <span className="font-mono mr-2">{t.schedule}</span>
                      <span>· {scheduleLabel(t.schedule)}</span>
                    </>
                  ) : (
                    <span>
                      POST {API_URL}/triggers/&lt;token&gt;/fire
                    </span>
                  )}
                </div>
                <div className="text-xs text-gray-400 mt-0.5">
                  {t.kind === "cron" && t.next_run_at ? (
                    <span title={fmtFull(t.next_run_at)}>
                      next: {fmtRelative(t.next_run_at)}
                    </span>
                  ) : (
                    <span>
                      last fired: {t.last_run_at ? fmtRelative(t.last_run_at) : "never"}
                    </span>
                  )}
                </div>
              </div>
              <button
                type="button"
                onClick={() => onToggle(t)}
                className={
                  "text-xs px-2 py-1 rounded ring-1 " +
                  (t.enabled
                    ? "ring-gray-300 text-gray-700 hover:bg-gray-50"
                    : "ring-amber-300 text-amber-700 bg-amber-50 hover:bg-amber-100")
                }
              >
                {t.enabled ? "Disable" : "Enable"}
              </button>
              <button
                type="button"
                onClick={() => onDelete(t)}
                className="text-xs px-2 py-1 rounded ring-1 ring-red-300 text-red-700 hover:bg-red-50"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}

      {showModal && (
        <NewTriggerModal
          agentName={agentName}
          onClose={() => setShowModal(false)}
          onCreated={(t) => {
            setTriggers((rows) => [t, ...(rows ?? [])]);
          }}
        />
      )}
    </section>
  );
}

// ---------- New-trigger modal ---------- //

type ModalProps = {
  agentName: string;
  onClose: () => void;
  onCreated: (t: TriggerWithToken) => void;
};

function NewTriggerModal({ agentName, onClose, onCreated }: ModalProps) {
  const [tab, setTab] = useState<"cron" | "webhook">("cron");
  // Cron tab state.
  const [presetKey, setPresetKey] = useState<string>("weekdays");
  const [customCron, setCustomCron] = useState<string>("");
  const [name, setName] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // After webhook create: the plaintext token shown once.
  const [createdToken, setCreatedToken] =
    useState<TriggerWithToken | null>(null);

  // Cron preview.
  const [preview, setPreview] = useState<string[] | null>(null);
  const effectiveCron =
    presetKey === "custom"
      ? customCron.trim()
      : (PRESETS.find((p) => p.key === presetKey)?.cron ?? "");

  useEffect(() => {
    if (tab !== "cron" || !effectiveCron) {
      setPreview(null);
      return;
    }
    let cancelled = false;
    const id = window.setTimeout(async () => {
      try {
        const out = await previewSchedule(effectiveCron, 3);
        if (!cancelled) setPreview(out);
      } catch {
        if (!cancelled) setPreview(null);
      }
    }, 250); // small debounce while the operator types
    return () => {
      cancelled = true;
      window.clearTimeout(id);
    };
  }, [tab, effectiveCron]);

  async function onSubmit() {
    setError(null);
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Name is required.");
      return;
    }
    setSubmitting(true);
    try {
      let body;
      if (tab === "cron") {
        if (presetKey === "custom") {
          if (!customCron.trim()) {
            throw new Error("Custom cron expression is required.");
          }
          body = {
            kind: "cron" as const,
            name: trimmed,
            schedule: customCron.trim(),
          };
        } else {
          body = {
            kind: "cron" as const,
            name: trimmed,
            preset: presetKey,
          };
        }
      } else {
        body = { kind: "webhook" as const, name: trimmed };
      }
      const created = await createAgentTrigger(agentName, body);
      onCreated(created);
      if (created.kind === "webhook" && created.webhook_token) {
        // Hold the modal open so the operator can copy the plaintext.
        setCreatedToken(created);
      } else {
        onClose();
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  // After-create webhook view.
  if (createdToken && createdToken.webhook_token) {
    const fireUrl = `${API_URL}/triggers/${createdToken.webhook_token}/fire`;
    const curl = `curl -X POST ${fireUrl} \\\n  -H "content-type: application/json" \\\n  -d '{"channel": "#sales"}'`;
    return (
      <ModalShell onClose={onClose} title="Webhook token">
        <div className="text-sm text-gray-600 mb-3">
          This token is shown <strong>once</strong>. Copy it now — there is
          no recovery path. To rotate, delete this trigger + create a new
          one.
        </div>
        <label className="block text-xs font-medium text-gray-500 mb-1">
          Token
        </label>
        <div className="font-mono text-xs break-all bg-gray-50 ring-1 ring-gray-200 rounded p-2 mb-3 select-all">
          {createdToken.webhook_token}
        </div>
        <label className="block text-xs font-medium text-gray-500 mb-1">
          Try it
        </label>
        <pre className="font-mono text-[11px] bg-gray-900 text-gray-100 rounded p-2 mb-4 overflow-x-auto">
          {curl}
        </pre>
        <div className="flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="text-sm px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-500"
          >
            I&apos;ve copied it
          </button>
        </div>
      </ModalShell>
    );
  }

  return (
    <ModalShell onClose={onClose} title="New trigger">
      <div className="flex gap-2 mb-4 border-b border-gray-200">
        <button
          type="button"
          onClick={() => setTab("cron")}
          className={
            "text-sm px-3 py-1.5 -mb-px border-b-2 " +
            (tab === "cron"
              ? "border-indigo-600 text-indigo-700"
              : "border-transparent text-gray-600 hover:text-gray-900")
          }
        >
          Cron
        </button>
        <button
          type="button"
          onClick={() => setTab("webhook")}
          className={
            "text-sm px-3 py-1.5 -mb-px border-b-2 " +
            (tab === "webhook"
              ? "border-indigo-600 text-indigo-700"
              : "border-transparent text-gray-600 hover:text-gray-900")
          }
        >
          Webhook
        </button>
      </div>

      <label className="block text-xs font-medium text-gray-500 mb-1">
        Name
      </label>
      <input
        type="text"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="morning digest"
        className="w-full text-sm rounded-md ring-1 ring-gray-300 px-3 py-2 mb-4 focus:outline-none focus:ring-2 focus:ring-indigo-600"
      />

      {tab === "cron" ? (
        <>
          <label className="block text-xs font-medium text-gray-500 mb-1">
            Schedule
          </label>
          <div className="grid grid-cols-2 gap-2 mb-3">
            {PRESETS.map((p) => (
              <button
                key={p.key}
                type="button"
                onClick={() => setPresetKey(p.key)}
                className={
                  "text-left text-sm px-3 py-2 rounded-md ring-1 " +
                  (presetKey === p.key
                    ? "ring-indigo-600 bg-indigo-50 text-indigo-900"
                    : "ring-gray-300 hover:bg-gray-50")
                }
              >
                <div className="font-medium">{p.label}</div>
                <div className="font-mono text-[11px] text-gray-500">
                  {p.cron}
                </div>
              </button>
            ))}
            <button
              type="button"
              onClick={() => setPresetKey("custom")}
              className={
                "text-left text-sm px-3 py-2 rounded-md ring-1 col-span-2 " +
                (presetKey === "custom"
                  ? "ring-indigo-600 bg-indigo-50 text-indigo-900"
                  : "ring-gray-300 hover:bg-gray-50")
              }
            >
              <div className="font-medium">Custom</div>
              <div className="text-[11px] text-gray-500">
                Paste a standard 5-field cron expression.
              </div>
            </button>
          </div>
          {presetKey === "custom" && (
            <input
              type="text"
              value={customCron}
              onChange={(e) => setCustomCron(e.target.value)}
              placeholder="*/15 * * * *"
              className="w-full font-mono text-sm rounded-md ring-1 ring-gray-300 px-3 py-2 mb-3 focus:outline-none focus:ring-2 focus:ring-indigo-600"
            />
          )}
          {preview && preview.length > 0 && (
            <div className="text-xs text-gray-600 bg-gray-50 ring-1 ring-gray-200 rounded p-2 mb-4">
              Next fires:
              <ul className="list-disc list-inside mt-1 space-y-0.5">
                {preview.map((iso) => (
                  <li key={iso} className="font-mono text-[11px]">
                    {new Date(iso).toLocaleString()}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      ) : (
        <div className="text-sm text-gray-600 mb-4">
          On create, a one-time token is shown. POST to{" "}
          <span className="font-mono text-xs">
            {API_URL}/triggers/&lt;token&gt;/fire
          </span>{" "}
          to fire the bot. The request body becomes{" "}
          <span className="font-mono text-xs">
            lightsei.trigger.webhook_payload
          </span>
          .
        </div>
      )}

      {error && (
        <div className="mb-3 text-sm text-red-600 bg-red-50 ring-1 ring-red-200 rounded px-3 py-2">
          {error}
        </div>
      )}

      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onClose}
          className="text-sm px-3 py-1.5 rounded-md ring-1 ring-gray-300 text-gray-700 hover:bg-gray-50"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={onSubmit}
          disabled={submitting}
          className="text-sm px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          {submitting ? "Creating…" : "Create trigger"}
        </button>
      </div>
    </ModalShell>
  );
}

function ModalShell({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-md p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 text-lg leading-none"
            aria-label="Close"
          >
            ×
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
