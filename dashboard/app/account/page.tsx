"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  ApiKeySummary,
  createApiKey,
  fetchApiKeys,
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
  setSession,
  UnauthorizedError,
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
  const [error, setError] = useState<string | null>(null);

  const loadAll = async () => {
    try {
      const [ws, ks, ss] = await Promise.all([
        fetchWorkspace(),
        fetchApiKeys(),
        fetchSessions(),
      ]);
      setWorkspace(ws);
      setName(ws.name);
      setKeys(ks);
      setSessions(ss);
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

  return (
    <main className="p-8 max-w-4xl mx-auto">
      <Header />

      {error && (
        <div className="mb-4 p-3 border border-red-300 bg-red-50 text-red-700 text-sm rounded">
          {error}
        </div>
      )}

      {/* --- Workspace --- */}
      <section className="mb-10">
        <h2 className="text-lg font-semibold mb-3">Workspace</h2>
        <form onSubmit={onRename} className="flex items-end gap-3">
          <div className="flex-1">
            <label className="block text-sm text-gray-600 mb-1">Name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full border border-gray-300 rounded px-3 py-2 text-sm"
            />
          </div>
          <button
            type="submit"
            disabled={renaming || !workspace || name === workspace.name || !name.trim()}
            className="px-4 py-2 bg-blue-600 text-white rounded text-sm font-medium disabled:opacity-50"
          >
            {renaming ? "saving..." : "save"}
          </button>
        </form>
        {savedAt && Date.now() - savedAt < 4000 && (
          <div className="text-xs text-green-700 mt-2">saved.</div>
        )}
        <div className="text-xs text-gray-500 mt-2">
          signed in as <span className="font-mono">{user?.email ?? "—"}</span>
        </div>
      </section>

      {/* --- API keys --- */}
      <section className="mb-10">
        <h2 className="text-lg font-semibold mb-3">API keys</h2>

        {newKey && (
          <div className="mb-4 p-4 border border-amber-300 bg-amber-50 rounded">
            <div className="text-xs uppercase font-semibold text-amber-800 mb-1">
              new key — copy now, shown once
            </div>
            <code className="block font-mono text-sm break-all text-amber-900 mb-2">
              {newKey.plaintext}
            </code>
            <div className="flex items-center gap-3 text-sm">
              <button
                type="button"
                className="text-blue-600 underline"
                onClick={() => navigator.clipboard.writeText(newKey.plaintext)}
              >
                copy
              </button>
              <button
                type="button"
                className="text-gray-600 underline"
                onClick={() => setNewKey(null)}
              >
                dismiss
              </button>
            </div>
          </div>
        )}

        <form onSubmit={onCreateKey} className="flex items-end gap-3 mb-4">
          <div className="flex-1">
            <label className="block text-sm text-gray-600 mb-1">New key name</label>
            <input
              value={keyName}
              onChange={(e) => setKeyName(e.target.value)}
              placeholder="production"
              className="w-full border border-gray-300 rounded px-3 py-2 text-sm"
            />
          </div>
          <button
            type="submit"
            disabled={!keyName.trim()}
            className="px-4 py-2 bg-blue-600 text-white rounded text-sm font-medium disabled:opacity-50"
          >
            create
          </button>
        </form>

        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-gray-200 text-gray-600">
              <th className="py-2 pr-4 font-medium">Name</th>
              <th className="py-2 pr-4 font-medium">Prefix</th>
              <th className="py-2 pr-4 font-medium">Created</th>
              <th className="py-2 pr-4 font-medium">Last used</th>
              <th className="py-2 pr-4 font-medium">Status</th>
              <th className="py-2 pr-4 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {keys.map((k) => (
              <tr key={k.id} className="border-b border-gray-100">
                <td className="py-2 pr-4">{k.name}</td>
                <td className="py-2 pr-4 font-mono text-xs">{k.prefix}…</td>
                <td className="py-2 pr-4 font-mono text-xs">{fmt(k.created_at)}</td>
                <td className="py-2 pr-4 font-mono text-xs">{fmt(k.last_used_at)}</td>
                <td className="py-2 pr-4">
                  {k.revoked_at ? (
                    <span className="text-red-700">revoked</span>
                  ) : (
                    <span className="text-gray-700">active</span>
                  )}
                </td>
                <td className="py-2 pr-4">
                  {!k.revoked_at && (
                    <button
                      type="button"
                      onClick={() => onRevokeKey(k.id)}
                      className="text-red-600 underline text-xs"
                    >
                      revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {keys.length === 0 && (
              <tr>
                <td className="py-2 text-gray-500 italic" colSpan={6}>
                  no keys yet
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {/* --- Sessions --- */}
      <section>
        <h2 className="text-lg font-semibold mb-3">Browser sessions</h2>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-gray-200 text-gray-600">
              <th className="py-2 pr-4 font-medium">Created</th>
              <th className="py-2 pr-4 font-medium">Expires</th>
              <th className="py-2 pr-4 font-medium">Status</th>
              <th className="py-2 pr-4 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((s) => (
              <tr key={s.id} className="border-b border-gray-100">
                <td className="py-2 pr-4 font-mono text-xs">{fmt(s.created_at)}</td>
                <td className="py-2 pr-4 font-mono text-xs">{fmt(s.expires_at)}</td>
                <td className="py-2 pr-4">
                  {s.revoked_at ? (
                    <span className="text-red-700">revoked</span>
                  ) : s.current ? (
                    <span className="text-green-700">current</span>
                  ) : (
                    <span className="text-gray-700">active</span>
                  )}
                </td>
                <td className="py-2 pr-4">
                  {!s.revoked_at && !s.current && (
                    <button
                      type="button"
                      onClick={() => onRevokeSession(s.id)}
                      className="text-red-600 underline text-xs"
                    >
                      revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {sessions.length === 0 && (
              <tr>
                <td className="py-2 text-gray-500 italic" colSpan={4}>
                  no active sessions
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </main>
  );
}
