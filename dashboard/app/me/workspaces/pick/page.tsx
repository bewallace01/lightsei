"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  NoActiveWorkspaceError,
  UnauthorizedError,
  WorkspaceMembership,
  createMyWorkspace,
  handleAuthError,
  listMyWorkspaces,
  switchMyWorkspace,
} from "../../../api";

/**
 * Phase 23.6: workspace picker for the "session valid but workspace
 * context bad" state.
 *
 * Triggers (any of):
 * - The user deleted their active workspace from another tab.
 * - Their session was migrated without an active_workspace_id set
 *   and they somehow lost it (defensive surface — the 0040
 *   migration backfilled every existing session).
 * - Future Phase 23B invite-revoke: the user got removed from the
 *   workspace they were viewing.
 *
 * For brand-new signups, the existing flow auto-creates a workspace
 * and lands them on the dashboard — this page is the recovery path
 * when active state goes stale.
 */
export default function PickWorkspacePage() {
  const router = useRouter();
  const [rows, setRows] = useState<WorkspaceMembership[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pickingId, setPickingId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        // listMyWorkspaces doesn't require an active workspace
        // (the backend reads from the user identity on session
        // auth before resolving workspace_id), so it works even
        // when this very page was reached because of a stale
        // active pointer.
        const out = await listMyWorkspaces();
        if (alive) setRows(out);
      } catch (e) {
        if (e instanceof NoActiveWorkspaceError) {
          // Defensive: shouldn't happen because listMyWorkspaces
          // is user-scoped, not workspace-scoped. If it does, we
          // can't recover; treat as a real logout.
          router.replace("/login");
          return;
        }
        if (handleAuthError(e, router)) return;
        if (alive) setError((e as Error).message);
      }
    })();
    return () => {
      alive = false;
    };
  }, [router]);

  async function onPick(target: WorkspaceMembership) {
    if (pickingId) return;
    setPickingId(target.id);
    setError(null);
    try {
      await switchMyWorkspace(target.id);
      // Send them to the dashboard home in the freshly-picked
      // workspace.
      router.replace("/");
    } catch (e) {
      setError((e as Error).message);
      setPickingId(null);
    }
  }

  async function onCreate() {
    const trimmed = newName.trim();
    if (!trimmed || creating) return;
    setCreating(true);
    setError(null);
    try {
      // createMyWorkspace auto-switches the session, so the next
      // page load lands in the new workspace.
      await createMyWorkspace(trimmed);
      router.replace("/");
    } catch (e) {
      setError((e as Error).message);
      setCreating(false);
    }
  }

  return (
    <main className="px-8 py-12 max-w-xl mx-auto">
      <h1 className="text-2xl font-semibold tracking-tight mb-2">
        Pick a workspace to continue
      </h1>
      <p className="text-sm text-gray-500 mb-8">
        Your session is fine — we just need to know which workspace to load.
      </p>

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {rows === null ? (
        <div className="text-gray-400 text-sm">loading…</div>
      ) : rows.length === 0 ? (
        <div className="text-sm text-gray-600 mb-6">
          You aren&apos;t a member of any workspace. Create one below to
          continue.
        </div>
      ) : (
        <ul className="space-y-2 mb-8">
          {rows.map((w) => (
            <li key={w.id}>
              <button
                type="button"
                onClick={() => onPick(w)}
                disabled={pickingId !== null}
                className="w-full text-left rounded-lg border border-gray-200 hover:border-indigo-400 hover:bg-indigo-50/30 px-4 py-3 transition-colors disabled:opacity-50"
              >
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-gray-900">
                    {w.name}
                  </span>
                  <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-gray-50 text-gray-600 ring-1 ring-gray-500/20">
                    {w.role}
                  </span>
                </div>
                <div className="text-xs text-gray-500 mt-1">
                  Plan {w.plan_tier}
                  {pickingId === w.id ? " · switching…" : ""}
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="rounded-lg border border-dashed border-gray-300 p-4">
        <label className="block text-xs font-medium text-gray-500 mb-1">
          Or create a new workspace
        </label>
        <div className="flex gap-2">
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="JYNI"
            onKeyDown={(e) => {
              if (e.key === "Enter") onCreate();
            }}
            className="flex-1 text-sm rounded-md ring-1 ring-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-600"
          />
          <button
            type="button"
            onClick={onCreate}
            disabled={creating || !newName.trim()}
            className="text-sm px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            {creating ? "Creating…" : "Create + open"}
          </button>
        </div>
      </div>
    </main>
  );
}
