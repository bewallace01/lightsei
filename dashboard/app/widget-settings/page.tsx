"use client";

// Phase 21.7: operator-facing widget settings page.
//
// Three editable surfaces:
//
//   1. Customer-facing bot picker — dropdown of deployed agents.
//      Picking auto-grants widget:respond + widget:escalate to
//      the bot (backend enforces) so the operator doesn't have to
//      think about capabilities.
//   2. Allowed origins — one HTTPS origin per line. Backend
//      validates per-entry; per-entry errors surface inline.
//   3. The widget snippet, pre-filled with the workspace's
//      widget_public_id. Copyable to clipboard.
//
// Plus a "Test it now" link that opens the workspace's own
// widget at /widget/{public_id} so the operator can play with
// the bot before pasting the snippet on a customer site.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import {
  UnauthorizedError,
  WidgetSettings,
  fetchWidgetSettings,
  handleAuthError,
  patchWidgetSettings,
} from "../api";
import { SensitivityChip } from "../sensitivity";


export default function WidgetSettingsPage(): JSX.Element {
  const router = useRouter();
  const [settings, setSettings] = useState<WidgetSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [savingBot, setSavingBot] = useState(false);
  const [savingOrigins, setSavingOrigins] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  // Origin editor local state — comma-separated isn't quite right
  // because URLs have no commas but newlines are the more natural
  // shape (one per line).
  const [originsText, setOriginsText] = useState("");
  const [originErrors, setOriginErrors] = useState<
    Array<{ index: number; value: string; error: string }>
  >([]);

  const [copied, setCopied] = useState(false);

  const load = async () => {
    try {
      setLoading(true);
      const s = await fetchWidgetSettings();
      setSettings(s);
      setOriginsText((s.allowed_widget_origins || []).join("\n"));
      setError(null);
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const flashTimeout = (msg: string) => {
    setFlash(msg);
    setTimeout(() => setFlash(null), 3000);
  };

  const onPickBot = async (name: string) => {
    setSavingBot(true);
    setError(null);
    try {
      const next = await patchWidgetSettings({
        customer_facing_agent_name: name || null,
      });
      setSettings(next);
      setOriginsText((next.allowed_widget_origins || []).join("\n"));
      flashTimeout(
        name
          ? `Bot set to '${name}'. Capabilities auto-granted.`
          : "Cleared the customer-facing bot.",
      );
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSavingBot(false);
    }
  };

  const onSaveOrigins = async () => {
    setSavingOrigins(true);
    setError(null);
    setOriginErrors([]);
    const lines = originsText
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => l.length > 0);
    try {
      const next = await patchWidgetSettings({
        allowed_widget_origins: lines,
      });
      setSettings(next);
      setOriginsText((next.allowed_widget_origins || []).join("\n"));
      flashTimeout("Origins saved.");
    } catch (e) {
      if (handleAuthError(e, router)) return;
      // 422 with per-entry error list surfaces as inline annotations.
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes("invalid_widget_origins")) {
        // The backend's HTTPException detail comes through as a
        // JSON-encoded string; parse defensively.
        try {
          const start = msg.indexOf("{");
          if (start >= 0) {
            const parsed = JSON.parse(msg.slice(start));
            if (parsed?.errors) setOriginErrors(parsed.errors);
          }
        } catch {
          // fall through to generic message
        }
      }
      setError(msg);
    } finally {
      setSavingOrigins(false);
    }
  };

  const snippet = useMemo(() => {
    if (!settings) return "";
    const apiOrigin =
      typeof window !== "undefined" ? window.location.origin : "https://app.lightsei.com";
    return `<script src="${apiOrigin}/widget.js"
        data-workspace="${settings.widget_public_id}"
        async></script>`;
  }, [settings]);

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(snippet);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Browsers may refuse clipboard access without focus; show a flash.
      flashTimeout("Couldn't copy — select + ⌘C manually.");
    }
  };

  if (loading) {
    return (
      <main className="max-w-3xl mx-auto px-6 py-8 text-sm text-gray-700">
        <p className="text-gray-500">Loading…</p>
      </main>
    );
  }

  if (!settings) {
    return (
      <main className="max-w-3xl mx-auto px-6 py-8 text-sm text-gray-700">
        <p className="text-red-700">{error || "Failed to load widget settings."}</p>
      </main>
    );
  }

  const previewUrl =
    typeof window !== "undefined"
      ? `${window.location.origin}/widget/${settings.widget_public_id}`
      : `/widget/${settings.widget_public_id}`;

  return (
    <main className="max-w-3xl mx-auto px-6 py-8 text-sm text-gray-900">
      <header className="mb-5">
        <div className="text-xs text-gray-500 mb-2">
          <Link href="/integrations" className="hover:text-gray-900">
            ← Integrations
          </Link>
        </div>
        <h1 className="text-xl font-semibold">Widget settings</h1>
        <p className="text-gray-600 mt-1 max-w-xl">
          Configure the embeddable chat widget your end users see on
          your product. Pick which bot answers them, control which
          sites can embed the widget, and copy the snippet.
        </p>
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

      {/* Bot picker */}
      <section className="rounded-lg border border-gray-200 bg-white p-4 mb-4">
        <h2 className="text-sm font-semibold text-gray-900 mb-2">
          Customer-facing bot
        </h2>
        <p className="text-gray-600 text-xs mb-3">
          This bot answers every widget conversation in this workspace.
          Picking it auto-grants <code className="text-gray-900 font-mono">widget:respond</code>{" "}
          + <code className="text-gray-900 font-mono">widget:escalate</code> if missing.
        </p>
        {settings.available_agents.length === 0 ? (
          <p className="text-gray-500 text-xs italic">
            No agents deployed in this workspace yet. Deploy a bot from{" "}
            <Link href="/agents" className="text-accent-600 hover:underline">
              /agents
            </Link>{" "}
            first.
          </p>
        ) : (
          <select
            value={settings.customer_facing_agent_name || ""}
            onChange={(e) => void onPickBot(e.target.value)}
            disabled={savingBot}
            className="w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-xs text-gray-900 disabled:opacity-50"
          >
            <option value="">— Not configured —</option>
            {settings.available_agents.map((a) => (
              <option key={a.name} value={a.name}>
                {a.name} · {a.sensitivity_level}
                {a.has_widget_capabilities ? " · ready" : ""}
              </option>
            ))}
          </select>
        )}
        {settings.customer_facing_agent_name && (
          <div className="mt-3 text-xs text-gray-600 flex items-center gap-2">
            <span>Selected zone:</span>
            <SensitivityChip
              level={
                (settings.available_agents.find(
                  (a) => a.name === settings.customer_facing_agent_name,
                )?.sensitivity_level || "public") as any
              }
              size="sm"
            />
          </div>
        )}
      </section>

      {/* Origin allowlist */}
      <section className="rounded-lg border border-gray-200 bg-white p-4 mb-4">
        <h2 className="text-sm font-semibold text-gray-900 mb-2">
          Allowed origins
        </h2>
        <p className="text-gray-600 text-xs mb-3">
          One HTTPS origin per line (e.g. <code className="font-mono">https://halo.dev</code>).
          The widget endpoint refuses POSTs from any origin not on this list,
          so without entries here the widget can't be embedded anywhere.
          For local dev, <code className="font-mono">http://localhost:PORT</code> is also accepted.
        </p>
        <textarea
          value={originsText}
          onChange={(e) => setOriginsText(e.target.value)}
          rows={4}
          spellCheck={false}
          className="w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-xs font-mono text-gray-900"
          placeholder={"https://your-product.com\nhttps://www.your-product.com"}
          disabled={savingOrigins}
        />
        {originErrors.length > 0 && (
          <ul className="mt-2 text-xs text-red-700 space-y-1">
            {originErrors.map((e) => (
              <li key={`${e.index}-${e.value}`}>
                <code className="font-mono">{e.value || "(empty)"}</code>: {e.error}
              </li>
            ))}
          </ul>
        )}
        <div className="mt-3 flex items-center gap-2">
          <button
            type="button"
            onClick={() => void onSaveOrigins()}
            disabled={savingOrigins}
            className="rounded bg-accent-600 hover:bg-accent-700 px-3 py-1.5 text-xs text-white disabled:opacity-50"
          >
            {savingOrigins ? "Saving…" : "Save origins"}
          </button>
          <span className="text-xs text-gray-500">
            {settings.allowed_widget_origins.length} active
          </span>
        </div>
      </section>

      {/* Snippet */}
      <section className="rounded-lg border border-gray-200 bg-white p-4 mb-4">
        <h2 className="text-sm font-semibold text-gray-900 mb-2">
          Embed snippet
        </h2>
        <p className="text-gray-600 text-xs mb-3">
          Paste this once on every page where you want the widget to
          appear. The script loads asynchronously and injects a
          fixed-position chat bubble in the bottom-right.
        </p>
        <pre className="rounded bg-gray-50 border border-gray-200 p-3 text-xs text-gray-900 overflow-x-auto whitespace-pre font-mono">
{snippet}
        </pre>
        <div className="mt-3 flex items-center gap-2">
          <button
            type="button"
            onClick={() => void onCopy()}
            className="rounded border border-gray-200 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50"
          >
            {copied ? "Copied!" : "Copy snippet"}
          </button>
          <a
            href={previewUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded border border-gray-200 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50"
          >
            Test it now →
          </a>
        </div>
        <p className="mt-2 text-xs text-gray-500">
          The "Test it now" link opens the widget at{" "}
          <code className="text-gray-900 font-mono">/widget/{settings.widget_public_id}</code>.
          Make sure your current dashboard origin is in the allowed-origins
          list above first; otherwise the preview will surface a 403.
        </p>
      </section>
    </main>
  );
}
