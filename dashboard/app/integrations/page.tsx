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
    <main className="max-w-5xl mx-auto px-4 py-6 sm:px-6 sm:py-8 text-sm text-gray-900">
      <header className="mb-6 flex items-baseline justify-between">
        <div>
          <h1 className="text-xl font-semibold">Integrations</h1>
          <p className="text-gray-600 mt-1">
            Connect external services your bots can call. Each connector
            declares which trust zones can use it; bots outside those
            zones are refused at the API gate.
          </p>
        </div>
      </header>

      {flash && (
        <div className="mb-4 rounded border border-green-200 bg-green-50 px-3 py-2 text-green-800">
          {flash}
        </div>
      )}
      {error && (
        <div className="mb-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-red-700">
          {error}
        </div>
      )}

      {connectors === null ? (
        <p className="text-gray-500">Loading…</p>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {cards.map((card) => (
            <div
              key={card.key}
              className="rounded-lg border border-gray-200 bg-white p-4 flex flex-col gap-3"
            >
              <div className="flex items-start justify-between">
                <Link href={card.href} className="group">
                  <h2 className="font-semibold text-gray-900 group-hover:text-accent-600">
                    {card.displayLabel}
                  </h2>
                  <p className="text-xs text-gray-500 mt-0.5">
                    OAuth: {card.oauthProvider}
                  </p>
                </Link>
                {card.installed ? (
                  <span className="rounded-full bg-green-100 text-green-800 text-xs px-2 py-0.5 border border-green-200">
                    Connected
                  </span>
                ) : (
                  <span className="rounded-full bg-gray-100 text-gray-600 text-xs px-2 py-0.5 border border-gray-200">
                    Not connected
                  </span>
                )}
              </div>

              <p className="text-gray-600 text-xs leading-relaxed">
                {card.summary}
              </p>

              <div className="flex flex-wrap gap-1.5 items-center">
                <span className="text-xs text-gray-500 mr-1">Zones:</span>
                {card.declaredZones.map((z) => (
                  <SensitivityChip key={z} level={z} size="sm" />
                ))}
              </div>

              {card.installed && (
                <div className="text-xs text-gray-500 border-t border-gray-100 pt-2">
                  {card.installLabel ? (
                    <span>
                      Connected as{" "}
                      <span className="text-gray-800">{card.installLabel}</span>
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
                      className="rounded border border-gray-200 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50"
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
                      className="rounded border border-red-200 text-red-700 px-3 py-1.5 text-xs hover:bg-red-50 disabled:opacity-50"
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
                    className="rounded bg-accent-600 hover:bg-accent-700 px-3 py-1.5 text-xs text-white disabled:opacity-50"
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
