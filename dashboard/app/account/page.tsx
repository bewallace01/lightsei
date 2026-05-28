"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiKeySummary,
  BillingNotConfiguredError,
  SessionSummary,
  SessionUser,
  SessionWorkspace,
  UnauthorizedError,
  WorkspaceMembership,
  WorkspaceSecretMeta,
  createApiKey,
  createBillingCheckout,
  createBillingPortal,
  deleteSecret,
  fetchApiKeys,
  fetchSecrets,
  fetchSessions,
  fetchWorkspace,
  getStoredUser,
  getStoredWorkspace,
  handleAuthError,
  listMyWorkspaces,
  renameWorkspace,
  revokeApiKey,
  revokeSession,
  setSecret,
  setSession,
  switchMyWorkspace,
} from "../api";
import {
  SUGGESTED_SECRET_ORDER,
  guidanceFor,
} from "../secret_guidance";

function fmt(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function AccountPage() {
  const router = useRouter();
  const [user, setUser] = useState<SessionUser | null>(null);
  const [workspace, setWorkspace] = useState<SessionWorkspace | null>(null);
  const [name, setName] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const [keys, setKeys] = useState<ApiKeySummary[]>([]);
  const [keyName, setKeyName] = useState("");
  const [newKey, setNewKey] = useState<{ id: string; plaintext: string } | null>(
    null,
  );

  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [secrets, setSecrets] = useState<WorkspaceSecretMeta[]>([]);
  const [secretName, setSecretName] = useState("");
  const [secretValue, setSecretValue] = useState("");
  const [savingSecret, setSavingSecret] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Phase 23.5: workspace memberships for the Workspaces section.
  const [memberships, setMemberships] = useState<WorkspaceMembership[]>([]);
  const [switchingTo, setSwitchingTo] = useState<string | null>(null);

  // Phase 17.7: billing state.
  const [billingBusy, setBillingBusy] = useState<"checkout" | "portal" | null>(null);
  const [billingError, setBillingError] = useState<string | null>(null);
  // 'success' | 'cancelled' | 'paying' when we're polling for the
  // webhook to flip plan_tier=paid after a Checkout return; cleared
  // once we see the flip or the poll timeout fires.
  const [billingFlash, setBillingFlash] = useState<
    null | "success" | "cancelled" | "paying"
  >(null);
  const pollTimeoutRef = useRef<number | null>(null);

  const loadAll = async () => {
    try {
      const [ws, ks, ss, sc, wsList] = await Promise.all([
        fetchWorkspace(),
        fetchApiKeys(),
        fetchSessions(),
        fetchSecrets().catch(() => [] as WorkspaceSecretMeta[]),
        listMyWorkspaces().catch(() => [] as WorkspaceMembership[]),
      ]);
      setWorkspace(ws);
      setName(ws.name);
      setKeys(ks);
      setSessions(ss);
      setSecrets(sc);
      setMemberships(wsList);
      setError(null);
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(String(e));
    }
  };

  async function onSwitchWorkspace(workspaceId: string) {
    if (switchingTo) return;
    setSwitchingTo(workspaceId);
    try {
      await switchMyWorkspace(workspaceId);
      // Full refresh: every workspace-scoped block on this page (keys,
      // secrets, billing) is keyed to the active workspace and needs
      // to re-fetch.
      router.refresh();
      await loadAll();
    } catch (e) {
      setError(String(e));
    } finally {
      setSwitchingTo(null);
    }
  }

  // Poll fetchWorkspace until plan_tier flips to 'paid' (webhook lands
  // within ~1s of Checkout success). Stops on flip, on timeout (45s),
  // or when the component unmounts.
  const pollForPaidFlip = useCallback(() => {
    const startedAt = Date.now();
    const tick = async () => {
      try {
        const ws = await fetchWorkspace();
        setWorkspace(ws);
        if (ws.plan_tier === "paid") {
          setBillingFlash("success");
          return;
        }
      } catch {
        // Ignore transient errors during polling; we'll retry.
      }
      if (Date.now() - startedAt > 45_000) {
        // Webhook didn't land in time. Leave the user on the "we're
        // confirming your payment" state with a manual refresh hint;
        // the next page load will resolve.
        return;
      }
      pollTimeoutRef.current = window.setTimeout(tick, 1500);
    };
    tick();
  }, []);

  useEffect(() => {
    setUser(getStoredUser());
    setWorkspace(getStoredWorkspace());
    loadAll();

    // Handle the Checkout / Portal redirect-back query params. We use
    // window.location instead of useSearchParams to avoid wrapping the
    // whole AccountPage in Suspense.
    const sp = new URLSearchParams(window.location.search);
    const upgrade = sp.get("upgrade");
    if (upgrade === "success") {
      setBillingFlash("paying");
      pollForPaidFlip();
      // Clean the query param so a hard reload doesn't re-trigger.
      window.history.replaceState({}, "", "/account");
    } else if (upgrade === "cancelled") {
      setBillingFlash("cancelled");
      window.history.replaceState({}, "", "/account");
    }

    return () => {
      if (pollTimeoutRef.current !== null) {
        clearTimeout(pollTimeoutRef.current);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onUpgrade = async () => {
    setBillingError(null);
    setBillingBusy("checkout");
    try {
      const { checkout_url } = await createBillingCheckout();
      window.location.href = checkout_url;
    } catch (e) {
      if (e instanceof BillingNotConfiguredError) {
        setBillingError(
          "Billing isn't configured on this Lightsei deployment yet. " +
            "Ask the admin to follow STRIPE_SETUP.md.",
        );
      } else {
        setBillingError((e as Error).message);
      }
      setBillingBusy(null);
    }
  };

  const onManageSubscription = async () => {
    setBillingError(null);
    setBillingBusy("portal");
    try {
      const { portal_url } = await createBillingPortal();
      window.location.href = portal_url;
    } catch (e) {
      if (e instanceof BillingNotConfiguredError) {
        setBillingError(
          "Billing isn't configured on this Lightsei deployment yet.",
        );
      } else {
        setBillingError((e as Error).message);
      }
      setBillingBusy(null);
    }
  };

  const onRename = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!workspace || !name.trim() || name === workspace.name) return;
    setRenaming(true);
    try {
      const updated = await renameWorkspace(name.trim());
      setWorkspace(updated);
      setName(updated.name);
      setSavedAt(Date.now());
      // refresh stored workspace so the header picks up the new name
      const u = getStoredUser();
      if (u) setSession(localStorage.getItem("lightsei.session_token") || "", u, updated);
    } catch (e) {
      setError(String(e));
    } finally {
      setRenaming(false);
    }
  };

  const onCreateKey = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!keyName.trim()) return;
    try {
      const created = await createApiKey(keyName.trim());
      setNewKey({ id: created.id, plaintext: created.plaintext });
      setKeyName("");
      await loadAll();
    } catch (e) {
      setError(String(e));
    }
  };

  const onRevokeKey = async (id: string) => {
    if (!confirm("Revoke this key? Anything using it stops working immediately.")) return;
    try {
      await revokeApiKey(id);
      await loadAll();
    } catch (e) {
      setError(String(e));
    }
  };

  const onRevokeSession = async (id: string) => {
    if (!confirm("Revoke this session?")) return;
    try {
      await revokeSession(id);
      await loadAll();
    } catch (e) {
      setError(String(e));
    }
  };

  const onSaveSecret = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!secretName.trim() || !secretValue) return;
    setSavingSecret(true);
    try {
      await setSecret(secretName.trim(), secretValue);
      setSecretName("");
      setSecretValue("");
      await loadAll();
    } catch (e) {
      setError(String(e));
    } finally {
      setSavingSecret(false);
    }
  };

  const onDeleteSecret = async (name: string) => {
    if (!confirm(`Delete secret ${name}? Anything reading it stops working.`)) return;
    try {
      await deleteSecret(name);
      await loadAll();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <main className="px-8 py-10 max-w-4xl mx-auto">

      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">Account</h1>
        <p className="text-sm text-gray-500 mt-1">
          Manage your workspace, API keys, and active sessions.
        </p>
      </div>

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {/* --- Phase 23.5: Workspaces --- */}
      <section className="mb-12">
        <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
          Workspaces
        </h2>
        {memberships.length === 0 ? (
          <div className="text-sm text-gray-400 italic">loading…</div>
        ) : (
          <ul className="rounded-lg border border-gray-200 divide-y divide-gray-100">
            {memberships.map((m) => (
              <li
                key={m.id}
                className="px-4 py-3 flex items-center gap-4 text-sm"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-gray-900 truncate">
                      {m.name}
                    </span>
                    {m.is_active && (
                      <span
                        className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700 ring-1 ring-indigo-600/20"
                        aria-label="active workspace"
                      >
                        active
                      </span>
                    )}
                    <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-gray-50 text-gray-600 ring-1 ring-gray-500/20">
                      {m.role}
                    </span>
                  </div>
                  <div className="text-xs text-gray-500 mt-0.5">
                    Joined {fmt(m.joined_at)} · plan {m.plan_tier}
                  </div>
                </div>
                {!m.is_active && (
                  <button
                    type="button"
                    onClick={() => onSwitchWorkspace(m.id)}
                    disabled={switchingTo !== null}
                    className="text-xs px-2 py-1 rounded ring-1 ring-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                  >
                    {switchingTo === m.id ? "Switching…" : "Switch"}
                  </button>
                )}
                {m.is_active && (
                  <Link
                    href="/workspace-settings"
                    className="text-xs px-2 py-1 rounded ring-1 ring-gray-300 text-gray-700 hover:bg-gray-50 no-underline"
                  >
                    Open settings
                  </Link>
                )}
              </li>
            ))}
          </ul>
        )}
        <div className="text-xs text-gray-400 mt-2">
          Need another workspace? Use the dropdown in the top-right to
          create one.
        </div>
      </section>

      {/* --- Billing --- */}
      <section className="mb-12">
        <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
          Billing
        </h2>

        {billingFlash === "paying" && (
          <div className="mb-4 p-3 border border-blue-200 bg-blue-50 text-blue-800 text-sm rounded-md">
            Confirming your payment with Stripe. This usually takes a couple of
            seconds; the page will update automatically.
          </div>
        )}
        {billingFlash === "success" && (
          <div className="mb-4 p-3 border border-green-200 bg-green-50 text-green-800 text-sm rounded-md">
            You&apos;re on the paid plan. Thanks!
          </div>
        )}
        {billingFlash === "cancelled" && (
          <div className="mb-4 p-3 border border-amber-200 bg-amber-50 text-amber-800 text-sm rounded-md">
            Upgrade cancelled, no charge made. You can try again any time.
          </div>
        )}
        {billingError && (
          <div className="mb-4 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
            {billingError}
          </div>
        )}

        {workspace?.plan_tier === "paid" ? (
          <div className="rounded-lg border border-gray-200 p-5 flex items-start justify-between gap-4">
            <div>
              <div className="flex items-center gap-2 mb-1.5">
                <span className="inline-block px-2 py-0.5 rounded-full bg-green-100 text-green-800 text-[11px] font-medium">
                  Paid
                </span>
                <span className="text-sm text-gray-700">
                  Active subscription · $50/mo
                </span>
              </div>
              <p className="text-xs text-gray-500">
                Manage your card, view invoices, or cancel via the Stripe
                customer portal.
              </p>
            </div>
            <button
              type="button"
              onClick={onManageSubscription}
              disabled={billingBusy !== null}
              className="shrink-0 px-4 py-2 border border-gray-300 hover:bg-gray-50 text-gray-800 rounded-md text-sm font-medium disabled:opacity-50 transition-colors"
            >
              {billingBusy === "portal" ? "opening…" : "manage subscription"}
            </button>
          </div>
        ) : (
          <div className="rounded-lg border border-gray-200 p-5 flex items-start justify-between gap-4">
            <div>
              <div className="flex items-center gap-2 mb-1.5">
                <span className="inline-block px-2 py-0.5 rounded-full bg-gray-100 text-gray-700 text-[11px] font-medium">
                  Free
                </span>
                <span className="text-sm text-gray-700">
                  {workspace
                    ? `$${(workspace.free_credits_remaining_usd ?? 0).toFixed(2)} of free credits remaining`
                    : "loading…"}
                </span>
              </div>
              <p className="text-xs text-gray-500">
                Upgrade to keep your bots running once your free credits are
                used up. $50/mo, cancel anytime.
              </p>
            </div>
            <button
              type="button"
              onClick={onUpgrade}
              disabled={billingBusy !== null}
              className="shrink-0 px-4 py-2 bg-accent-600 hover:bg-accent-700 text-white rounded-md text-sm font-medium disabled:opacity-50 transition-colors"
            >
              {billingBusy === "checkout" ? "opening…" : "upgrade to $50/mo"}
            </button>
          </div>
        )}
      </section>

      {/* --- Workspace --- */}
      <section className="mb-12">
        <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
          Workspace
        </h2>
        <form onSubmit={onRename} className="flex items-end gap-3">
          <div className="flex-1">
            <label className="block text-sm font-medium text-gray-700 mb-1.5">
              Name
            </label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500 transition-shadow"
            />
          </div>
          <button
            type="submit"
            disabled={renaming || !workspace || name === workspace.name || !name.trim()}
            className="px-4 py-2 bg-accent-600 hover:bg-accent-700 text-white rounded-md text-sm font-medium disabled:opacity-50 transition-colors"
          >
            {renaming ? "saving…" : "save"}
          </button>
        </form>
        {savedAt && Date.now() - savedAt < 4000 && (
          <div className="text-xs text-green-700 mt-2">saved.</div>
        )}
        <div className="text-xs text-gray-500 mt-3">
          signed in as <span className="font-mono">{user?.email ?? "—"}</span>
        </div>
      </section>

      {/* --- API keys --- */}
      <section className="mb-12">
        <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
          API keys
        </h2>

        {newKey && (
          <div className="mb-5 p-4 border border-amber-300 bg-amber-50 rounded-lg">
            <div className="text-[10px] uppercase tracking-wider font-semibold text-amber-800 mb-1.5">
              new key — copy now, shown once
            </div>
            <code className="block font-mono text-sm break-all text-amber-900 mb-2.5">
              {newKey.plaintext}
            </code>
            <div className="flex items-center gap-4 text-sm">
              <button
                type="button"
                className="text-accent-700 hover:text-accent-800 font-medium"
                onClick={() => navigator.clipboard.writeText(newKey.plaintext)}
              >
                copy
              </button>
              <button
                type="button"
                className="text-gray-600 hover:text-gray-800"
                onClick={() => setNewKey(null)}
              >
                dismiss
              </button>
            </div>
          </div>
        )}

        <form onSubmit={onCreateKey} className="flex items-end gap-3 mb-5">
          <div className="flex-1">
            <label className="block text-sm font-medium text-gray-700 mb-1.5">
              New key name
            </label>
            <input
              value={keyName}
              onChange={(e) => setKeyName(e.target.value)}
              placeholder="production"
              className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500 transition-shadow"
            />
          </div>
          <button
            type="submit"
            disabled={!keyName.trim()}
            className="px-4 py-2 bg-accent-600 hover:bg-accent-700 text-white rounded-md text-sm font-medium disabled:opacity-50 transition-colors"
          >
            create
          </button>
        </form>

        <div className="rounded-lg border border-gray-200 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-[11px] uppercase tracking-wider text-gray-500">
            <tr>
              <th className="px-4 py-3 font-medium">Name</th>
              <th className="px-4 py-3 font-medium">Prefix</th>
              <th className="px-4 py-3 font-medium">Created</th>
              <th className="px-4 py-3 font-medium">Last used</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {keys.map((k, i) => (
              <tr
                key={k.id}
                className={
                  i !== keys.length - 1 ? "border-b border-gray-100" : ""
                }
              >
                <td className="px-4 py-3 text-gray-800">{k.name}</td>
                <td className="px-4 py-3 font-mono text-xs text-gray-600">
                  {k.prefix}…
                </td>
                <td className="px-4 py-3 font-mono text-xs text-gray-600">
                  {fmt(k.created_at)}
                </td>
                <td className="px-4 py-3 font-mono text-xs text-gray-600">
                  {fmt(k.last_used_at)}
                </td>
                <td className="px-4 py-3">
                  {k.revoked_at ? (
                    <span className="inline-block px-2 py-0.5 rounded-full bg-red-100 text-red-800 text-[11px] font-medium">
                      revoked
                    </span>
                  ) : (
                    <span className="inline-block px-2 py-0.5 rounded-full bg-gray-100 text-gray-700 text-[11px] font-medium">
                      active
                    </span>
                  )}
                </td>
                <td className="px-4 py-3 text-right">
                  {!k.revoked_at && (
                    <button
                      type="button"
                      onClick={() => onRevokeKey(k.id)}
                      className="text-red-600 hover:text-red-700 text-xs font-medium"
                    >
                      revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {keys.length === 0 && (
              <tr>
                <td className="px-4 py-4 text-gray-400 italic text-sm" colSpan={6}>
                  no keys yet
                </td>
              </tr>
            )}
          </tbody>
        </table>
        </div>
      </section>

      {/* --- Secrets --- */}
      <section className="mb-12">
        <h2 className="text-[11px] font-semibold text-gray-500 mb-2 uppercase tracking-wider">
          Workspace secrets
        </h2>
        <p className="text-xs text-gray-500 mb-4">
          Encrypted KV store for API keys and other config your bot needs.
          Read from your code with{" "}
          <code className="font-mono">lightsei.get_secret(&quot;NAME&quot;)</code>.
        </p>

        {/* Suggested secrets — common ones bots in this workspace are
            likely to want. Expand a row to see what the secret is for +
            where to get it; click "use this name" to prefill the form
            below with the exact name. */}
        <div className="mb-5 rounded-lg border border-gray-200 bg-gray-50/60 p-4">
          <div className="flex items-baseline justify-between mb-3">
            <h3 className="text-sm font-semibold text-gray-700">
              Suggested secrets
            </h3>
            <span className="text-[11px] text-gray-500">
              {(() => {
                const haveSet = new Set(secrets.map((s) => s.name));
                const setCount = SUGGESTED_SECRET_ORDER.filter((n) =>
                  haveSet.has(n),
                ).length;
                return `${setCount} of ${SUGGESTED_SECRET_ORDER.length} set`;
              })()}
            </span>
          </div>
          <p className="text-xs text-gray-500 mb-3">
            Common API keys + webhooks bots reach for. Expand any row to
            see where to get the value.
          </p>
          <ul className="space-y-2">
            {SUGGESTED_SECRET_ORDER.map((name) => {
              const isSet = secrets.some((s) => s.name === name);
              const g = guidanceFor(name);
              const isExternal = /^https?:\/\//.test(g.url);
              return (
                <li key={name}>
                  <details className="group rounded border border-gray-200 bg-white open:shadow-sm">
                    <summary className="cursor-pointer list-none px-3 py-2 text-sm flex items-center gap-3">
                      <code className="font-mono text-gray-800 flex-1">{name}</code>
                      {isSet ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-green-100 text-green-800 text-[11px] font-medium">
                          ✓ set
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 text-[11px] font-medium">
                          not set
                        </span>
                      )}
                      <span className="text-xs text-gray-500 group-open:hidden">
                        details →
                      </span>
                      <span className="text-xs text-gray-500 hidden group-open:inline">
                        hide
                      </span>
                    </summary>
                    <div className="px-3 pb-3 pt-1 text-xs text-gray-700 space-y-2 border-t border-gray-100">
                      <p>{g.what}</p>
                      <p>{g.where}</p>
                      <div className="flex items-center gap-4 pt-1">
                        {isExternal ? (
                          <a
                            href={g.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-accent-600 hover:text-accent-700 font-medium"
                          >
                            open the page →
                          </a>
                        ) : null}
                        {!isSet ? (
                          <button
                            type="button"
                            onClick={() => {
                              setSecretName(name);
                              // Scroll the form into view so the
                              // password field is the next thing the
                              // user sees after clicking.
                              document
                                .getElementById("secret-form")
                                ?.scrollIntoView({
                                  behavior: "smooth",
                                  block: "center",
                                });
                            }}
                            className="text-accent-600 hover:text-accent-700 font-medium"
                          >
                            use this name in the form below →
                          </button>
                        ) : null}
                      </div>
                    </div>
                  </details>
                </li>
              );
            })}
          </ul>
        </div>

        <form id="secret-form" onSubmit={onSaveSecret} className="grid grid-cols-12 gap-3 mb-5">
          <div className="col-span-4">
            <label className="block text-sm font-medium text-gray-700 mb-1.5">
              Name
            </label>
            <input
              value={secretName}
              onChange={(e) => setSecretName(e.target.value)}
              placeholder="OPENAI_API_KEY"
              className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500"
            />
          </div>
          <div className="col-span-6">
            <label className="block text-sm font-medium text-gray-700 mb-1.5">
              Value
            </label>
            <input
              type="password"
              value={secretValue}
              onChange={(e) => setSecretValue(e.target.value)}
              placeholder="sk-…"
              className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500"
            />
          </div>
          <div className="col-span-2 flex items-end">
            <button
              type="submit"
              disabled={savingSecret || !secretName.trim() || !secretValue}
              className="w-full px-4 py-2 bg-accent-600 hover:bg-accent-700 text-white rounded-md text-sm font-medium disabled:opacity-50 transition-colors"
            >
              {savingSecret ? "saving…" : "save"}
            </button>
          </div>
        </form>

        <div className="rounded-lg border border-gray-200 overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="bg-gray-50 text-[11px] uppercase tracking-wider text-gray-500">
              <tr>
                <th className="px-4 py-3 font-medium">Name</th>
                <th className="px-4 py-3 font-medium">Created</th>
                <th className="px-4 py-3 font-medium">Updated</th>
                <th className="px-4 py-3 font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {secrets.map((s, i) => (
                <tr
                  key={s.name}
                  className={
                    i !== secrets.length - 1 ? "border-b border-gray-100" : ""
                  }
                >
                  <td className="px-4 py-3 font-mono text-gray-800">{s.name}</td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-600">
                    {fmt(s.created_at)}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-600">
                    {fmt(s.updated_at)}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      type="button"
                      onClick={() => onDeleteSecret(s.name)}
                      className="text-red-600 hover:text-red-700 text-xs font-medium"
                    >
                      delete
                    </button>
                  </td>
                </tr>
              ))}
              {secrets.length === 0 && (
                <tr>
                  <td className="px-4 py-4 text-gray-400 italic text-sm" colSpan={4}>
                    no secrets yet
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* --- Sessions --- */}
      <section>
        <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
          Browser sessions
        </h2>
        <div className="rounded-lg border border-gray-200 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-[11px] uppercase tracking-wider text-gray-500">
            <tr>
              <th className="px-4 py-3 font-medium">Created</th>
              <th className="px-4 py-3 font-medium">Expires</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((s, i) => (
              <tr
                key={s.id}
                className={
                  i !== sessions.length - 1 ? "border-b border-gray-100" : ""
                }
              >
                <td className="px-4 py-3 font-mono text-xs text-gray-600">
                  {fmt(s.created_at)}
                </td>
                <td className="px-4 py-3 font-mono text-xs text-gray-600">
                  {fmt(s.expires_at)}
                </td>
                <td className="px-4 py-3">
                  {s.revoked_at ? (
                    <span className="inline-block px-2 py-0.5 rounded-full bg-red-100 text-red-800 text-[11px] font-medium">
                      revoked
                    </span>
                  ) : s.current ? (
                    <span className="inline-block px-2 py-0.5 rounded-full bg-green-100 text-green-800 text-[11px] font-medium">
                      current
                    </span>
                  ) : (
                    <span className="inline-block px-2 py-0.5 rounded-full bg-gray-100 text-gray-700 text-[11px] font-medium">
                      active
                    </span>
                  )}
                </td>
                <td className="px-4 py-3 text-right">
                  {!s.revoked_at && !s.current && (
                    <button
                      type="button"
                      onClick={() => onRevokeSession(s.id)}
                      className="text-red-600 hover:text-red-700 text-xs font-medium"
                    >
                      revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {sessions.length === 0 && (
              <tr>
                <td className="px-4 py-4 text-gray-400 italic text-sm" colSpan={4}>
                  no active sessions
                </td>
              </tr>
            )}
          </tbody>
        </table>
        </div>
      </section>
    </main>
  );
}
