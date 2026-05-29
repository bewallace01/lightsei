"use client";

// Phase 19.7: dashboard surface for the Slack chat integration.
//
// The operator-facing view of Phase 19's machinery: connect/disconnect
// Slack via OAuth, opt channels in, set per-channel sensitivity zones.
// The orchestrator (19.4) reads the channel rows on every @-mention.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import {
  SENSITIVITY_LEVELS,
  SensitivityLevel,
  SlackChannelSummary,
  SlackWorkspaceSummary,
  UnauthorizedError,
  fetchSlackChannels,
  fetchSlackWorkspaces,
  handleAuthError,
  patchSlackChannel,
  revokeSlackWorkspace,
  startSlackOAuth,
} from "../../api";
import HelpTip from "../../HelpTip";
import { SensitivityChip } from "../../sensitivity";


function fmtRelative(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}


export default function SlackIntegrationPage(): JSX.Element {
  const router = useRouter();
  const [workspaces, setWorkspaces] = useState<SlackWorkspaceSummary[] | null>(null);
  const [channels, setChannels] = useState<SlackChannelSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"connect" | string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const loadAll = async () => {
    try {
      const [wsList, chList] = await Promise.all([
        fetchSlackWorkspaces(),
        fetchSlackChannels(),
      ]);
      setWorkspaces(wsList);
      setChannels(chList);
      setError(null);
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(String(e));
    }
  };

  useEffect(() => {
    loadAll();
    // Handle the OAuth callback's redirect-back: it lands us here with
    // ?installed=true. Show a green banner; clean the query so a hard
    // reload doesn't re-flash it.
    if (typeof window === "undefined") return;
    const sp = new URLSearchParams(window.location.search);
    if (sp.get("installed") === "true") {
      setFlash("Slack workspace connected. Channels will appear here as Lightsei sees them.");
      window.history.replaceState({}, "", "/integrations/slack");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onConnect = async () => {
    setBusy("connect");
    setError(null);
    try {
      const { authorization_url } = await startSlackOAuth();
      window.location.href = authorization_url;
    } catch (e) {
      setError(String(e));
      setBusy(null);
    }
  };

  const onRevoke = async (slackTeamId: string) => {
    if (!confirm(
      `Disconnect this Slack workspace? Lightsei will stop responding to mentions ` +
      `there until it's reinstalled.`,
    )) {
      return;
    }
    setBusy(`revoke:${slackTeamId}`);
    try {
      await revokeSlackWorkspace(slackTeamId);
      await loadAll();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const onTogglePatch = async (
    slackTeamId: string,
    channelId: string,
    patch: { sensitivity_level?: SensitivityLevel; opted_in?: boolean },
  ) => {
    setBusy(`patch:${slackTeamId}:${channelId}`);
    try {
      const updated = await patchSlackChannel(slackTeamId, channelId, patch);
      setChannels((cur) =>
        cur.map((c) =>
          c.slack_team_id === updated.slack_team_id && c.channel_id === updated.channel_id
            ? updated
            : c,
        ),
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  // Group channels by slack_team_id so the dashboard renders them
  // under their parent workspace block. Map preserves insertion order.
  const channelsByTeam = useMemo(() => {
    const m = new Map<string, SlackChannelSummary[]>();
    for (const ch of channels) {
      const arr = m.get(ch.slack_team_id) ?? [];
      arr.push(ch);
      m.set(ch.slack_team_id, arr);
    }
    return m;
  }, [channels]);

  return (
    <main className="px-4 py-6 sm:px-8 sm:py-10 max-w-4xl mx-auto">
      <div className="mb-2">
        <Link href="/account" className="text-sm text-gray-500 hover:text-gray-900">
          ← integrations
        </Link>
      </div>
      <h1 className="text-2xl font-semibold tracking-tight">Slack</h1>
      <p className="text-sm text-gray-500 mt-1 mb-8 max-w-2xl">
        Connect Lightsei to your Slack workspace so the team can @-mention
        a bot from a channel. Each channel gets a{" "}
        <HelpTip term="sensitivity_zone" />
        — the chat orchestrator only routes to bots in the same zone.
      </p>

      {flash && (
        <div className="mb-6 p-3 border border-green-200 bg-green-50 text-green-800 text-sm rounded-md">
          {flash}
        </div>
      )}
      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {workspaces === null ? (
        <div className="text-gray-400 text-sm">loading…</div>
      ) : workspaces.length === 0 ? (
        <div className="border border-dashed border-gray-200 rounded-lg p-10 text-center">
          <div className="text-gray-700 font-medium mb-2">
            No Slack workspace connected
          </div>
          <p className="text-sm text-gray-500 mb-4 max-w-xl mx-auto">
            Connect Slack to let your team @-mention Lightsei from any
            channel. The framework enforces per-channel trust zones so PII
            bots can&apos;t be reached from public channels by mistake.
          </p>
          <button
            type="button"
            onClick={onConnect}
            disabled={busy === "connect"}
            className="px-4 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 disabled:opacity-50 transition-colors"
          >
            {busy === "connect" ? "opening…" : "Connect Slack"}
          </button>
        </div>
      ) : (
        <div className="space-y-8">
          {workspaces.map((ws) => {
            const teamChannels = channelsByTeam.get(ws.slack_team_id) ?? [];
            const revoking = busy === `revoke:${ws.slack_team_id}`;
            return (
              <section
                key={ws.slack_team_id}
                className="rounded-lg border border-gray-200 overflow-hidden"
              >
                <header className="flex items-start justify-between gap-3 px-5 py-4 border-b border-gray-100 bg-gray-50/40">
                  <div>
                    <h2 className="text-lg font-semibold text-gray-900">
                      {ws.team_name}
                    </h2>
                    <p className="text-xs text-gray-500 mt-0.5 font-mono">
                      {ws.slack_team_id} · bot user{" "}
                      <span className="text-gray-700">{ws.bot_user_id}</span>
                    </p>
                    <p className="text-xs text-gray-500 mt-0.5">
                      installed {fmtRelative(ws.installed_at)}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => onRevoke(ws.slack_team_id)}
                    disabled={revoking}
                    className="text-xs text-red-600 hover:text-red-700 disabled:opacity-50"
                  >
                    {revoking ? "disconnecting…" : "disconnect"}
                  </button>
                </header>
                <div className="px-5 py-4">
                  <div className="flex items-baseline justify-between mb-3">
                    <h3 className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">
                      Channels ({teamChannels.length})
                    </h3>
                    <span className="text-[11px] text-gray-400">
                      New channels appear here the first time @Lightsei is mentioned in them.
                    </span>
                  </div>
                  {teamChannels.length === 0 ? (
                    <p className="text-sm text-gray-500 italic">
                      No channels yet. @-mention Lightsei in a channel to surface it here.
                    </p>
                  ) : (
                    <ul className="divide-y divide-gray-100">
                      {teamChannels.map((ch) => {
                        const patching = busy === `patch:${ch.slack_team_id}:${ch.channel_id}`;
                        return (
                          <li
                            key={`${ch.slack_team_id}:${ch.channel_id}`}
                            className="py-3 flex items-center justify-between gap-4"
                          >
                            <div className="min-w-0">
                              <div className="flex items-center gap-2">
                                <span className="font-mono text-sm text-gray-900 truncate">
                                  #{ch.channel_name}
                                </span>
                                <SensitivityChip level={ch.sensitivity_level} />
                                {!ch.opted_in && (
                                  <span className="text-[11px] uppercase tracking-wider text-gray-500 font-medium">
                                    silent
                                  </span>
                                )}
                              </div>
                              <div className="text-[11px] text-gray-400 font-mono mt-0.5">
                                {ch.channel_id}
                              </div>
                            </div>
                            <div className="flex items-center gap-3 shrink-0">
                              <label className="flex items-center gap-1 text-xs text-gray-600">
                                <select
                                  value={ch.sensitivity_level}
                                  onChange={(e) =>
                                    onTogglePatch(ch.slack_team_id, ch.channel_id, {
                                      sensitivity_level: e.target.value as SensitivityLevel,
                                    })
                                  }
                                  disabled={patching}
                                  className="border border-gray-300 rounded-md px-2 py-1 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-accent-500/30"
                                >
                                  {SENSITIVITY_LEVELS.map((lvl) => (
                                    <option key={lvl} value={lvl}>
                                      {lvl}
                                    </option>
                                  ))}
                                </select>
                              </label>
                              <label className="flex items-center gap-1.5 text-xs text-gray-700 cursor-pointer">
                                <input
                                  type="checkbox"
                                  checked={ch.opted_in}
                                  onChange={(e) =>
                                    onTogglePatch(ch.slack_team_id, ch.channel_id, {
                                      opted_in: e.target.checked,
                                    })
                                  }
                                  disabled={patching}
                                  className="rounded"
                                />
                                opted in
                              </label>
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>
              </section>
            );
          })}

          <div className="text-sm text-gray-500">
            Want to connect a second Slack workspace?{" "}
            <button
              type="button"
              onClick={onConnect}
              disabled={busy !== null}
              className="text-accent-600 hover:text-accent-700 disabled:opacity-50"
            >
              {busy === "connect" ? "opening…" : "Connect another"}
            </button>
            .
          </div>
        </div>
      )}

    </main>
  );
}
