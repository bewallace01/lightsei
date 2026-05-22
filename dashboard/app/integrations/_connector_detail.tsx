"use client";

// Phase 20.8: shared per-Google-connector detail page.
//
// Gmail, Calendar, and Drive all share the same install/disconnect
// surface — only the connector_type + the capability hint differ.
// One component, three thin wrappers under
// /integrations/{gmail,google-calendar,google-drive}.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  ConnectorSummary,
  UnauthorizedError,
  disconnectConnector,
  fetchConnectors,
  startConnectorOAuth,
} from "../api";
import { SensitivityChip } from "../sensitivity";


function fmtRelative(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}


type Props = {
  connectorType: string;          // e.g. "gmail"
  capabilityHint: string;         // e.g. "connector:gmail"
  exampleCode: string;            // small code snippet for bot authors
};


export default function ConnectorDetailPage(
  { connectorType, capabilityHint, exampleCode }: Props,
): JSX.Element {
  const router = useRouter();
  const [connector, setConnector] = useState<ConnectorSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const list = await fetchConnectors();
      const found = list.find((c) => c.type === connectorType) || null;
      setConnector(found);
      setError(null);
    } catch (e) {
      if (e instanceof UnauthorizedError) {
        router.replace("/login");
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [connectorType]);

  const installed = !!connector?.install;

  const handleConnect = async () => {
    setBusy(true);
    setError(null);
    try {
      const { authorization_url } = await startConnectorOAuth(connectorType, {
        redirectAfter: `${window.location.origin}/integrations/${connectorType.replace("_", "-")}?installed=1`,
      });
      window.location.href = authorization_url;
    } catch (e) {
      if (e instanceof UnauthorizedError) {
        router.replace("/login");
        return;
      }
      setBusy(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const handleDisconnect = async () => {
    if (!confirm(`Disconnect ${connector?.display_label || connectorType}?`)) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await disconnectConnector(connectorType);
      setFlash("Disconnected.");
      await load();
    } catch (e) {
      if (e instanceof UnauthorizedError) {
        router.replace("/login");
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <main className="max-w-3xl mx-auto px-6 py-8 text-sm text-zinc-300">
        <p className="text-zinc-500">Loading…</p>
      </main>
    );
  }

  if (!connector) {
    return (
      <main className="max-w-3xl mx-auto px-6 py-8 text-sm text-zinc-300">
        <p className="text-rose-300">
          Unknown connector {connectorType!}. Go back to{" "}
          <Link href="/integrations" className="text-indigo-400 hover:underline">
            integrations
          </Link>
          .
        </p>
      </main>
    );
  }

  return (
    <main className="max-w-3xl mx-auto px-6 py-8 text-sm text-zinc-200">
      <header className="mb-5">
        <div className="text-xs text-zinc-500 mb-2">
          <Link href="/integrations" className="hover:text-zinc-300">
            ← Integrations
          </Link>
        </div>
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-xl font-semibold">{connector.display_label}</h1>
            <p className="text-zinc-400 mt-1 max-w-xl">{connector.summary}</p>
          </div>
          {installed ? (
            <span className="rounded-full bg-emerald-900/50 text-emerald-300 text-xs px-2 py-0.5 border border-emerald-700/60">
              Connected
            </span>
          ) : (
            <span className="rounded-full bg-zinc-800 text-zinc-400 text-xs px-2 py-0.5 border border-zinc-700">
              Not connected
            </span>
          )}
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

      <section className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-4 mb-4">
        <h2 className="text-sm font-semibold text-zinc-200 mb-2">
          Trust-zone access
        </h2>
        <p className="text-zinc-400 text-xs mb-3">
          Only bots in these zones can call this connector. A bot
          outside the allow-list is refused at the API gate even if
          it holds the <code className="text-zinc-300">{capabilityHint}</code>{" "}
          capability.
        </p>
        <div className="flex flex-wrap gap-1.5">
          {connector.declared_zones.map((z) => (
            <SensitivityChip key={z} level={z} size="md" />
          ))}
        </div>
      </section>

      {installed && connector.install && (
        <section className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-4 mb-4">
          <h2 className="text-sm font-semibold text-zinc-200 mb-2">
            Install
          </h2>
          <dl className="text-xs grid grid-cols-[140px,1fr] gap-y-1.5 text-zinc-400">
            <dt>Account</dt>
            <dd className="text-zinc-300">
              {connector.install.external_account_email || "—"}
            </dd>
            <dt>Connected</dt>
            <dd className="text-zinc-300">
              {fmtRelative(connector.install.installed_at)}
            </dd>
            <dt>Granted scopes</dt>
            <dd className="text-zinc-300 break-all">
              {connector.install.scopes.length > 0
                ? connector.install.scopes.join(", ")
                : "—"}
            </dd>
          </dl>
        </section>
      )}

      <section className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-4 mb-4">
        <h2 className="text-sm font-semibold text-zinc-200 mb-2">
          Bot usage
        </h2>
        <p className="text-zinc-400 text-xs mb-3">
          Grant <code className="text-zinc-300">{capabilityHint}</code>{" "}
          to the bots that should use this connector
          (PATCH <code className="text-zinc-300">/agents/{"{name}"}/capabilities</code>{" "}
          or via the agent detail page). The bot then calls:
        </p>
        <pre className="rounded bg-black/60 border border-zinc-800 p-3 text-xs text-zinc-300 overflow-x-auto">
{exampleCode}
        </pre>
      </section>

      <div className="flex items-center gap-2">
        {installed ? (
          <button
            type="button"
            onClick={handleDisconnect}
            disabled={busy}
            className="rounded border border-rose-800/70 text-rose-300 px-3 py-1.5 text-xs hover:bg-rose-900/30 disabled:opacity-50"
          >
            {busy ? "Working…" : "Disconnect"}
          </button>
        ) : (
          <button
            type="button"
            onClick={handleConnect}
            disabled={busy}
            className="rounded bg-indigo-600 hover:bg-indigo-500 px-3 py-1.5 text-xs text-white disabled:opacity-50"
          >
            {busy ? "Redirecting…" : `Connect ${connector.display_label}`}
          </button>
        )}
      </div>
    </main>
  );
}
