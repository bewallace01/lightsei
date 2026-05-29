"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import {
  ChannelType,
  NotificationChannel,
  NotificationDelivery,
  TriggerName,
  UnauthorizedError,
  createNotificationChannel,
  deleteNotificationChannel,
  fetchNotificationChannels,
  fetchNotificationDeliveries,
  handleAuthError,
  patchNotificationChannel,
  testNotificationChannel,
} from "../api";

const CHANNEL_TYPES: { value: ChannelType; label: string; hint: string }[] = [
  {
    value: "slack",
    label: "Slack",
    hint: "Slack → Apps → Incoming Webhooks → Add to a channel.",
  },
  {
    value: "discord",
    label: "Discord",
    hint:
      "Discord channel settings → Integrations → Webhooks → New Webhook → Copy URL.",
  },
  {
    value: "teams",
    label: "Microsoft Teams",
    hint:
      "Teams channel → Workflows → \"Post to a channel when a webhook request is received\" → Copy URL. (Old Office 365 Connector URLs no longer work as of 2025.)",
  },
  {
    value: "mattermost",
    label: "Mattermost",
    hint: "Mattermost → Integrations → Incoming Webhooks → Add.",
  },
  {
    value: "webhook",
    label: "Generic webhook",
    hint:
      "Any URL that accepts POST. Optional shared secret enables HMAC-SHA256 signing via X-Lightsei-Signature.",
  },
];

const TRIGGERS: { value: TriggerName; label: string; description: string }[] = [
  {
    value: "polaris.plan",
    label: "Polaris plan",
    description: "Hourly summary of next moves from the orchestrator.",
  },
  {
    value: "validation.fail",
    label: "Validation failed",
    description: "An event tripped a content rule or schema check.",
  },
  {
    value: "run_failed",
    label: "Run failed",
    description: "A bot crashed mid-run. Get paged on the error.",
  },
];

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function fmtRelative(iso: string): string {
  try {
    const ts = new Date(iso).getTime();
    const now = Date.now();
    const diff = Math.max(0, now - ts);
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

function CHANNEL_TYPE_LABEL(type: ChannelType): string {
  return CHANNEL_TYPES.find((t) => t.value === type)?.label ?? type;
}

function CHANNEL_TYPE_HINT(type: ChannelType): string {
  return CHANNEL_TYPES.find((t) => t.value === type)?.hint ?? "";
}

function StatusPill({ status }: { status: string }) {
  // Sent = green, failed/error = red, anything else = gray.
  const cls =
    status === "sent"
      ? "bg-emerald-50 text-emerald-700 border-emerald-200"
      : status === "failed" || status === "error"
      ? "bg-red-50 text-red-700 border-red-200"
      : status === "skipped" || status === "timeout"
      ? "bg-amber-50 text-amber-800 border-amber-200"
      : "bg-gray-50 text-gray-500 border-gray-200";
  return (
    <span
      className={
        "inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold tracking-wide border " +
        cls
      }
    >
      {status.toUpperCase()}
    </span>
  );
}

function TriggerChip({ trigger }: { trigger: string }) {
  return (
    <span className="inline-block px-1.5 py-0.5 rounded text-[10px] font-mono text-gray-700 bg-gray-100 border border-gray-200">
      {trigger}
    </span>
  );
}

function ChannelTypeIcon({ type }: { type: ChannelType }) {
  // Tiny mono-letter badges. Keeps the page free of brand-asset
  // licensing concerns while still letting users scan rows by type.
  const letter = type[0].toUpperCase();
  const cls =
    type === "slack"
      ? "bg-purple-50 text-purple-700 border-purple-200"
      : type === "discord"
      ? "bg-indigo-50 text-indigo-700 border-indigo-200"
      : type === "teams"
      ? "bg-blue-50 text-blue-700 border-blue-200"
      : type === "mattermost"
      ? "bg-cyan-50 text-cyan-700 border-cyan-200"
      : "bg-gray-100 text-gray-700 border-gray-300";
  return (
    <span
      className={
        "inline-flex items-center justify-center w-7 h-7 rounded-md border font-mono text-xs font-bold " +
        cls
      }
      title={CHANNEL_TYPE_LABEL(type)}
    >
      {letter}
    </span>
  );
}

// ---------------- Add-channel form ----------------

function AddChannelForm({
  onCreated,
}: {
  onCreated: (c: NotificationChannel) => void;
}) {
  const [name, setName] = useState("");
  const [type, setType] = useState<ChannelType>("slack");
  const [targetUrl, setTargetUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [triggers, setTriggers] = useState<TriggerName[]>([
    "polaris.plan",
    "validation.fail",
    "run_failed",
  ]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setName("");
    setTargetUrl("");
    setSecret("");
    setError(null);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!name.trim() || !targetUrl.trim() || triggers.length === 0) {
      setError("name, target URL, and at least one trigger are required");
      return;
    }
    setSubmitting(true);
    try {
      const created = await createNotificationChannel({
        name: name.trim(),
        type,
        target_url: targetUrl.trim(),
        triggers,
        secret_token: type === "webhook" && secret.trim() ? secret.trim() : undefined,
      });
      onCreated(created);
      reset();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const toggleTrigger = (t: TriggerName) => {
    setTriggers((prev) =>
      prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t],
    );
  };

  return (
    <form
      onSubmit={submit}
      className="rounded-lg border border-gray-200 bg-white p-5 space-y-4"
    >
      <div className="text-sm font-medium text-gray-900">Add a channel</div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <label className="block">
          <span className="text-xs font-medium text-gray-600">Name</span>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. team-ops"
            className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none"
          />
        </label>

        <label className="block">
          <span className="text-xs font-medium text-gray-600">Type</span>
          <select
            value={type}
            onChange={(e) => setType(e.target.value as ChannelType)}
            className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none"
          >
            {CHANNEL_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <label className="block">
        <span className="text-xs font-medium text-gray-600">Webhook URL</span>
        <input
          type="url"
          value={targetUrl}
          onChange={(e) => setTargetUrl(e.target.value)}
          placeholder="https://..."
          className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm font-mono focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none"
        />
        <span className="text-[11px] text-gray-500 mt-1 block">
          {CHANNEL_TYPE_HINT(type)}
        </span>
      </label>

      {type === "webhook" && (
        <label className="block">
          <span className="text-xs font-medium text-gray-600">
            Shared secret <span className="font-normal text-gray-400">(optional)</span>
          </span>
          <input
            type="text"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            placeholder="leave blank for unsigned requests"
            className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm font-mono focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none"
          />
          <span className="text-[11px] text-gray-500 mt-1 block">
            When set, requests carry X-Lightsei-Signature: sha256=&lt;hex&gt;
            and X-Lightsei-Timestamp headers. Receivers verify by HMAC-
            SHA256 over &quot;&#123;timestamp&#125;.&quot; + body bytes.
          </span>
        </label>
      )}

      <div>
        <span className="text-xs font-medium text-gray-600 block mb-2">
          Notify on
        </span>
        <div className="space-y-2">
          {TRIGGERS.map((t) => (
            <label
              key={t.value}
              className="flex items-start gap-2 text-sm cursor-pointer"
            >
              <input
                type="checkbox"
                checked={triggers.includes(t.value)}
                onChange={() => toggleTrigger(t.value)}
                className="mt-0.5"
              />
              <span>
                <span className="font-medium text-gray-900">{t.label}</span>{" "}
                <span className="text-gray-500 text-xs">{t.description}</span>
              </span>
            </label>
          ))}
        </div>
      </div>

      {error && (
        <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-md p-2">
          {error}
        </div>
      )}

      <div className="flex justify-end">
        <button
          type="submit"
          disabled={submitting}
          className="inline-flex items-center px-3 py-1.5 rounded-md bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-700 transition-colors disabled:opacity-50"
        >
          {submitting ? "creating…" : "Add channel"}
        </button>
      </div>
    </form>
  );
}

// ---------------- One channel row ----------------

function ChannelRow({
  channel,
  onChanged,
  onDeleted,
}: {
  channel: NotificationChannel;
  onChanged: (c: NotificationChannel) => void;
  onDeleted: (id: string) => void;
}) {
  const [testing, setTesting] = useState(false);
  const [lastTest, setLastTest] = useState<NotificationDelivery | null>(null);
  const [editing, setEditing] = useState(false);
  const [editTriggers, setEditTriggers] = useState<TriggerName[]>(
    channel.triggers,
  );
  const [deliveries, setDeliveries] = useState<NotificationDelivery[]>([]);
  const [showDeliveries, setShowDeliveries] = useState(false);

  // Lazy-load deliveries when the user expands the row. The list is
  // bounded server-side at 200 entries so it's safe to fetch all at
  // once when expanded.
  useEffect(() => {
    if (!showDeliveries) return;
    let alive = true;
    fetchNotificationDeliveries(channel.id, 50)
      .then((d) => {
        if (alive) setDeliveries(d);
      })
      .catch(() => {
        if (alive) setDeliveries([]);
      });
    return () => {
      alive = false;
    };
  }, [showDeliveries, channel.id]);

  const recentSummary = useMemo(() => {
    if (deliveries.length === 0) return null;
    const sent = deliveries.filter((d) => d.status === "sent").length;
    const failed = deliveries.filter(
      (d) => d.status === "failed" || d.status === "error",
    ).length;
    return { sent, failed, total: deliveries.length };
  }, [deliveries]);

  const handleTest = async () => {
    setTesting(true);
    try {
      const delivery = await testNotificationChannel(channel.id);
      setLastTest(delivery);
      // Refresh deliveries list if expanded
      if (showDeliveries) {
        const refreshed = await fetchNotificationDeliveries(channel.id, 50);
        setDeliveries(refreshed);
      }
    } catch {
      // The API never 500s on a misconfigured channel — it returns a
      // failed delivery row. So an error here means something else
      // went wrong (network, auth). Fall through; the user can retry.
    } finally {
      setTesting(false);
    }
  };

  const handleToggleActive = async () => {
    try {
      const updated = await patchNotificationChannel(channel.id, {
        is_active: !channel.is_active,
      });
      onChanged(updated);
    } catch {
      // Show in a toast eventually; for now silent retry-on-click.
    }
  };

  const handleSaveTriggers = async () => {
    try {
      const updated = await patchNotificationChannel(channel.id, {
        triggers: editTriggers,
      });
      onChanged(updated);
      setEditing(false);
    } catch {
      // Same — tolerate.
    }
  };

  const handleDelete = async () => {
    if (!confirm(`Delete channel "${channel.name}"?`)) return;
    try {
      await deleteNotificationChannel(channel.id);
      onDeleted(channel.id);
    } catch {
      // Tolerate.
    }
  };

  return (
    <div
      className={
        "rounded-lg border bg-white p-4 " +
        (channel.is_active ? "border-gray-200" : "border-gray-200 bg-gray-50/60")
      }
    >
      <div className="flex items-start gap-4">
        <ChannelTypeIcon type={channel.type} />
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-2">
            <span className="text-sm font-medium text-gray-900">
              {channel.name}
            </span>
            <span className="text-xs text-gray-400">
              {CHANNEL_TYPE_LABEL(channel.type)}
            </span>
            {!channel.is_active && (
              <span className="text-[10px] uppercase tracking-wide text-amber-700 bg-amber-50 border border-amber-200 px-1.5 py-0.5 rounded">
                muted
              </span>
            )}
            {channel.has_secret_token && (
              <span
                className="text-[10px] uppercase tracking-wide text-indigo-700 bg-indigo-50 border border-indigo-200 px-1.5 py-0.5 rounded"
                title="Requests are HMAC-signed"
              >
                signed
              </span>
            )}
          </div>
          <div className="text-xs font-mono text-gray-500 mt-0.5 truncate">
            {channel.target_url_masked}
          </div>

          <div className="mt-2 flex flex-wrap gap-1">
            {editing ? (
              <div className="flex items-center gap-3">
                {TRIGGERS.map((t) => (
                  <label
                    key={t.value}
                    className="flex items-center gap-1 text-xs cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      checked={editTriggers.includes(t.value)}
                      onChange={() =>
                        setEditTriggers((prev) =>
                          prev.includes(t.value)
                            ? prev.filter((x) => x !== t.value)
                            : [...prev, t.value],
                        )
                      }
                    />
                    <span className="font-mono">{t.value}</span>
                  </label>
                ))}
                <button
                  type="button"
                  onClick={handleSaveTriggers}
                  className="text-xs text-indigo-600 hover:text-indigo-800 transition-colors font-medium"
                >
                  save
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setEditing(false);
                    setEditTriggers(channel.triggers);
                  }}
                  className="text-xs text-gray-500 hover:text-gray-700 transition-colors"
                >
                  cancel
                </button>
              </div>
            ) : (
              <>
                {channel.triggers.length === 0 && (
                  <span className="text-[11px] text-gray-400 italic">
                    no triggers — channel will never fire
                  </span>
                )}
                {channel.triggers.map((t) => (
                  <TriggerChip key={t} trigger={t} />
                ))}
                <button
                  type="button"
                  onClick={() => setEditing(true)}
                  className="text-[11px] text-gray-500 hover:text-gray-700 ml-1 transition-colors"
                >
                  edit
                </button>
              </>
            )}
          </div>
        </div>

        <div className="flex flex-col items-end gap-2 shrink-0">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleTest}
              disabled={testing}
              className="text-xs px-2 py-1 rounded-md border border-gray-300 hover:bg-gray-50 transition-colors disabled:opacity-50"
            >
              {testing
                ? "sending…"
                : lastTest
                ? lastTest.status === "sent"
                  ? "✓ sent"
                  : "✗ failed"
                : "send test"}
            </button>
            <button
              type="button"
              onClick={handleToggleActive}
              className="text-xs px-2 py-1 rounded-md border border-gray-300 hover:bg-gray-50 transition-colors"
            >
              {channel.is_active ? "mute" : "unmute"}
            </button>
            <button
              type="button"
              onClick={handleDelete}
              className="text-xs text-red-600 hover:text-red-800 px-2 py-1 transition-colors"
            >
              delete
            </button>
          </div>
          {lastTest && (
            <div className="text-[11px] text-gray-500 font-mono">
              {lastTest.status === "sent"
                ? `delivered ${fmtRelative(lastTest.sent_at)}`
                : `${(lastTest.response_summary as { error?: string; http_status?: number })
                    ?.error ?? ""} ${
                    (lastTest.response_summary as { http_status?: number })
                      ?.http_status ?? ""
                  }`.trim()}
            </div>
          )}
        </div>
      </div>

      <div className="mt-3 pt-3 border-t border-gray-100">
        <button
          type="button"
          onClick={() => setShowDeliveries((v) => !v)}
          className="text-[11px] uppercase tracking-wider text-gray-500 hover:text-gray-700 transition-colors"
        >
          {showDeliveries ? "hide" : "show"} recent deliveries
        </button>
        {showDeliveries && (
          <div className="mt-3 space-y-1">
            {deliveries.length === 0 ? (
              <div className="text-xs text-gray-400 italic">
                no deliveries yet
              </div>
            ) : (
              <>
                {recentSummary && (
                  <div className="text-[11px] text-gray-600 mb-2">
                    last {recentSummary.total}: {recentSummary.sent} sent,{" "}
                    {recentSummary.failed} failed
                  </div>
                )}
                <div className="rounded-md border border-gray-200 overflow-hidden">
                  <table className="w-full text-xs">
                    <thead className="bg-gray-50 text-[10px] uppercase tracking-wider text-gray-500">
                      <tr>
                        <th className="text-left px-3 py-1.5 font-medium">
                          Sent
                        </th>
                        <th className="text-left px-3 py-1.5 font-medium">
                          Trigger
                        </th>
                        <th className="text-left px-3 py-1.5 font-medium">
                          Status
                        </th>
                        <th className="text-left px-3 py-1.5 font-medium">
                          Detail
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {deliveries.map((d) => {
                        const summary = d.response_summary as {
                          error?: string;
                          http_status?: number;
                          response_preview?: string;
                          message?: string;
                        };
                        let detail = "";
                        if (d.status === "sent" && summary.http_status) {
                          detail = `${summary.http_status}`;
                        } else if (summary.error) {
                          detail = `${summary.error}${
                            summary.http_status
                              ? ` ${summary.http_status}`
                              : ""
                          }`;
                        }
                        return (
                          <tr
                            key={d.id}
                            className="border-t border-gray-100"
                          >
                            <td className="px-3 py-1.5 text-gray-600 whitespace-nowrap">
                              {fmtTime(d.sent_at)}
                            </td>
                            <td className="px-3 py-1.5 font-mono text-gray-700">
                              {d.trigger}
                            </td>
                            <td className="px-3 py-1.5">
                              <StatusPill status={d.status} />
                            </td>
                            <td className="px-3 py-1.5 font-mono text-gray-500">
                              {detail}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------- Page ----------------

export default function NotificationsPage() {
  const router = useRouter();
  const [channels, setChannels] = useState<NotificationChannel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = async () => {
    try {
      const list = await fetchNotificationChannels();
      setChannels(list);
      setError(null);
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    reload();
    // No polling — channel state is operator-driven, not bot-driven.
    // Manual reload via the "Add channel" success path or page nav.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <main className="px-4 py-6 sm:px-8 sm:py-10 max-w-5xl mx-auto">

      <div className="flex items-baseline justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Notifications
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            Pipe Polaris plans, validation failures, and run crashes into
            Slack, Discord, Teams, Mattermost, or any webhook.
          </p>
        </div>
      </div>

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      <div className="space-y-6">
        <AddChannelForm
          onCreated={(c) => setChannels((prev) => [...prev, c])}
        />

        {loading ? (
          <div className="text-gray-400 text-sm">loading…</div>
        ) : channels.length === 0 ? (
          <div className="rounded-lg border border-dashed border-gray-200 p-10 text-center">
            <div className="text-gray-700 font-medium mb-1">
              No channels yet
            </div>
            <p className="text-sm text-gray-500">
              Add one above. Once registered, real events automatically fire
              messages to your team chat.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="text-[11px] uppercase tracking-wider text-gray-500">
              Registered channels
            </div>
            {channels.map((c) => (
              <ChannelRow
                key={c.id}
                channel={c}
                onChanged={(updated) =>
                  setChannels((prev) =>
                    prev.map((p) => (p.id === updated.id ? updated : p)),
                  )
                }
                onDeleted={(id) =>
                  setChannels((prev) => prev.filter((p) => p.id !== id))
                }
              />
            ))}
          </div>
        )}
      </div>
    </main>
  );
}
