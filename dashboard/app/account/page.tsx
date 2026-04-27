"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  ApiKeySummary,
  createApiKey,
  deleteSecret,
  fetchApiKeys,
  fetchSecrets,
  fetchSessions,
  fetchWorkspace,
  getStoredUser,
  getStoredWorkspace,
  renameWorkspace,
  revokeApiKey,
  revokeSession,
  SessionSummary,
  SessionUser,
  SessionWorkspace,
  setSecret,
  setSession,
  UnauthorizedError,
  WorkspaceSecretMeta,
} from "../api";
import Header from "../Header";

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

  const loadAll = async () => {
    try {
      const [ws, ks, ss, sc] = await Promise.all([
        fetchWorkspace(),
        fetchApiKeys(),
        fetchSessions(),
        fetchSecrets().catch(() => [] as WorkspaceSecretMeta[]),
      ]);
      setWorkspace(ws);
      setName(ws.name);
      setKeys(ks);
      setSessions(ss);
      setSecrets(sc);
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
    setUser(getStoredUser());
    setWorkspace(getStoredWorkspace());
    loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
      <Header />

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

        <div className="rounded-lg border border-gray-200 overflow-hidden">
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

        <form onSubmit={onSaveSecret} className="grid grid-cols-12 gap-3 mb-5">
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

        <div className="rounded-lg border border-gray-200 overflow-hidden">
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
        <div className="rounded-lg border border-gray-200 overflow-hidden">
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
