"use client";

// Phase 20.8: connector index page.
//
// Card grid showing every connector in the registry (Slack from
// Phase 19 + Gmail / Calendar / Drive from Phase 20). Each card
// surfaces the connector's declared zones, install state, and the
// connect/disconnect action.
//
// Slack lives at /integrations/slack with its own UI (channel
// opt-in + per-channel sensitivity). The three Google connectors
// have minimal detail pages at /integrations/{type} — connect /
// disconnect + capability hint, no per-channel-equivalent surface.

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import {
  ConnectorSummary,
  SensitivityLevel,
  SlackWorkspaceSummary,
  UnauthorizedError,
  disconnectConnector,
  fetchConnectors,
  fetchSlackWorkspaces,
  handleAuthError,
  revokeSlackWorkspace,
  startConnectorOAuth,
  startSlackOAuth,
} from "../api";
import { SensitivityChip } from "../sensitivity";


type ConnectorCard = {
  key: string;
  href: string;
  displayLabel: string;
  oauthProvider: string;
  declaredZones: SensitivityLevel[];
  summary: string;
  installed: boolean;
  installLabel: string | null;
  installedAt: string | null;
};


function fmtRelative(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString();
  } catch {
    return iso;
  }
}


export default function IntegrationsIndexPage(): JSX.Element {
  // Suspense wrapper required by Next.js for any tree that calls
  // useSearchParams() at the page level.
  return (
    <Suspense fallback={null}>
      <IntegrationsIndexInner />
    </Suspense>
  );
}


function IntegrationsIndexInner(): JSX.Element {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [connectors, setConnectors] = useState<ConnectorSummary[] | null>(null);
  const [slackWorkspaces, setSlackWorkspaces] = useState<SlackWorkspaceSummary[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const loadAll = async () => {
    try {
      const [cs, ws] = await Promise.all([
        fetchConnectors(),
        fetchSlackWorkspaces(),
      ]);
      setConnectors(cs);
      setSlackWorkspaces(ws);
      setError(null);
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    loadAll();
  }, []);

  // Show a flash on ?installed=<type> redirect from the OAuth callback.
  useEffect(() => {
    const installed = searchParams?.get("installed");
    if (installed) {
      setFlash(`Connected ${installed}.`);
    }
  }, [searchParams]);

  const handleConnectGoogle = async (connectorType: string) => {
    setBusy(connectorType);
    setError(null);
    try {
      const { authorization_url } = await startConnectorOAuth(connectorType, {
        redirectAfter: `${window.location.origin}/integrations?installed=${connectorType}`,
      });
      window.location.href = authorization_url;
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setBusy(null);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const handleConnectSlack = async () => {
    setBusy("slack");
    setError(null);
    try {
      const { authorization_url } = await startSlackOAuth();
      window.location.href = authorization_url;
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setBusy(null);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const handleDisconnect = async (connectorType: string) => {
    if (!confirm(`Disconnect ${connectorType}? Bots calling this connector will fail until reconnected.`)) {
      return;
    }
    setBusy(connectorType);
    setError(null);
    try {
      await disconnectConnector(connectorType);
      setFlash(`Disconnected ${connectorType}.`);
      await loadAll();
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const handleDisconnectSlack = async (slackTeamId: string, teamName: string) => {
    if (!confirm(`Disconnect Slack workspace ${teamName}?`)) return;
    setBusy("slack");
    setError(null);
    try {
      await revokeSlackWorkspace(slackTeamId);
      setFlash(`Disconnected Slack workspace ${teamName}.`);
      await loadAll();
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  // Build a unified list of cards. Slack is treated as a virtual
  // connector card so the grid is uniform; clicking it routes to
  // the dedicated /integrations/slack page for channel config.
  const cards: ConnectorCard[] = [];
  const activeSlack = slackWorkspaces.find((w) => !w.revoked_at);
  cards.push({
    key: "slack",
    href: "/integrations/slack",
    displayLabel: "Slack",
    oauthProvider: "slack",
    declaredZones: ["public", "internal", "sensitive", "pii"],
    summary:
      "Chat surface: bots reply to @-mentions, route messages by channel sensitivity, post digests.",
    installed: !!activeSlack,
    installLabel: activeSlack ? activeSlack.team_name : null,
    installedAt: activeSlack ? activeSlack.installed_at : null,
  });
  for (const c of connectors || []) {
    cards.push({
      key: c.type,
      href: `/integrations/${c.type.replace("_", "-")}`,
      displayLabel: c.display_label,
      oauthProvider: c.oauth_provider,
      declaredZones: c.declared_zones,
      summary: c.summary,
      installed: !!c.install,
      installLabel: c.install?.external_account_email || null,
      installedAt: c.install?.installed_at || null,
    });
  }

  return (
    <main className="max-w-5xl mx-auto px-6 py-8 text-sm text-zinc-200">
      <header className="mb-6 flex items-baseline justify-between">
        <div>
          <h1 className="text-xl font-semibold">Integrations</h1>
          <p className="text-zinc-400 mt-1">
            Connect external services your bots can call. Each connector
            declares which trust zones can use it; bots outside those
            zones are refused at the API gate.
          </p>
        </div>
      </header>

      {flash && (
        <div className="mb-4 rounded border border-emerald-700/60 bg-emerald-900/30 px-3 py-2 text-emerald-200">
          {flash}
        </div>
      )}
      {error && (
        <div className="mb-4 rounded border border-rose-700/60 bg-rose-900/30 px-3 py-2 text-rose-200">
          {error}
        </div>
      )}

      {connectors === null ? (
        <p className="text-zinc-500">Loading…</p>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {cards.map((card) => (
            <div
              key={card.key}
              className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-4 flex flex-col gap-3"
            >
              <div className="flex items-start justify-between">
                <Link href={card.href} className="group">
                  <h2 className="font-semibold text-zinc-100 group-hover:text-indigo-300">
                    {card.displayLabel}
                  </h2>
                  <p className="text-xs text-zinc-500 mt-0.5">
                    OAuth: {card.oauthProvider}
                  </p>
                </Link>
                {card.installed ? (
                  <span className="rounded-full bg-emerald-900/50 text-emerald-300 text-xs px-2 py-0.5 border border-emerald-700/60">
                    Connected
                  </span>
                ) : (
                  <span className="rounded-full bg-zinc-800 text-zinc-400 text-xs px-2 py-0.5 border border-zinc-700">
                    Not connected
                  </span>
                )}
              </div>

              <p className="text-zinc-400 text-xs leading-relaxed">
                {card.summary}
              </p>

              <div className="flex flex-wrap gap-1.5">
                <span className="text-xs text-zinc-500 mr-1">Zones:</span>
                {card.declaredZones.map((z) => (
                  <SensitivityChip key={z} level={z} size="sm" />
                ))}
              </div>

              {card.installed && (
                <div className="text-xs text-zinc-500 border-t border-zinc-800 pt-2">
                  {card.installLabel ? (
                    <span>
                      Connected as{" "}
                      <span className="text-zinc-300">{card.installLabel}</span>
                    </span>
                  ) : (
                    <span>Connected</span>
                  )}
                  {card.installedAt && (
                    <span> · since {fmtRelative(card.installedAt)}</span>
                  )}
                </div>
              )}

              <div className="flex items-center gap-2 mt-auto pt-1">
                {card.installed ? (
                  <>
                    <Link
                      href={card.href}
                      className="rounded border border-zinc-700 px-3 py-1.5 text-xs hover:bg-zinc-800"
                    >
                      Configure →
                    </Link>
                    <button
                      type="button"
                      onClick={() =>
                        card.key === "slack"
                          ? activeSlack &&
                            handleDisconnectSlack(
                              activeSlack.slack_team_id,
                              activeSlack.team_name || activeSlack.slack_team_id,
                            )
                          : handleDisconnect(card.key)
                      }
                      disabled={busy === card.key}
                      className="rounded border border-rose-800/70 text-rose-300 px-3 py-1.5 text-xs hover:bg-rose-900/30 disabled:opacity-50"
                    >
                      {busy === card.key ? "Working…" : "Disconnect"}
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    onClick={() =>
                      card.key === "slack"
                        ? handleConnectSlack()
                        : handleConnectGoogle(card.key)
                    }
                    disabled={busy === card.key}
                    className="rounded bg-indigo-600 hover:bg-indigo-500 px-3 py-1.5 text-xs text-white disabled:opacity-50"
                  >
                    {busy === card.key ? "Redirecting…" : "Connect"}
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
