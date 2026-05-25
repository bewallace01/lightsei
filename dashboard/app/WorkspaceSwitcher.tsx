"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  createMyWorkspace,
  listMyWorkspaces,
  setStoredWorkspace,
  SessionWorkspace,
  switchMyWorkspace,
  WorkspaceMembership,
} from "./api";

type Props = {
  onClose: () => void;
  // Phase 23.x (#218): optional callback the Header passes in so its
  // workspace chip + dropdown title can update without a page reload.
  // When set, WorkspaceSwitcher invokes it after a successful
  // switch / create with the WorkspaceMembership shape; Header lifts
  // that into its own SessionWorkspace state.
  onWorkspaceChanged?: (next: SessionWorkspace) => void;
};

function _toSessionWorkspace(m: WorkspaceMembership): SessionWorkspace {
  return {
    id: m.id,
    name: m.name,
    plan_tier: m.plan_tier === "paid" ? "paid" : "free",
    created_at: m.created_at,
    // The membership response doesn't carry billing-shaped fields;
    // they refresh on next fetchWorkspace() call (e.g. /account load).
  };
}

/**
 * Phase 23.4: workspace switcher mounted inside the Header dropdown.
 *
 * Fetches the user's workspaces on mount, renders one row per
 * workspace with a checkmark for the active one + a click handler
 * that flips active via POST /me/workspaces/{id}/switch and then
 * calls router.refresh() so every component bound to workspace data
 * pulls fresh state. "+ New workspace" opens an inline modal that
 * names + creates + auto-switches the new workspace.
 */
export default function WorkspaceSwitcher({
  onClose,
  onWorkspaceChanged,
}: Props) {
  const router = useRouter();
  const [rows, setRows] = useState<WorkspaceMembership[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const out = await listMyWorkspaces();
        if (alive) setRows(out);
      } catch (e) {
        if (alive) setError((e as Error).message);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  async function onSwitch(target: WorkspaceMembership) {
    if (target.is_active || busyId) return;
    setBusyId(target.id);
    setError(null);
    try {
      const updated = await switchMyWorkspace(target.id);
      // Phase 23.x (#218): patch session storage + lift Header state
      // BEFORE router.refresh() so the chip + dropdown title catch
      // up without waiting for a remount.
      const ws = _toSessionWorkspace(updated);
      setStoredWorkspace(ws);
      onWorkspaceChanged?.(ws);
      // Mutating the session active pointer = every workspace-scoped
      // fetch needs to re-run. router.refresh() invalidates the
      // App Router's RSC cache; client useEffect-driven fetches
      // re-fire because we close the menu (which unmounts this
      // component) + the parent re-renders.
      onClose();
      router.refresh();
    } catch (e) {
      setError((e as Error).message);
      setBusyId(null);
    }
  }

  return (
    <>
      <div className="border-t border-gray-100">
        <div className="px-3 pt-2 pb-1 text-[11px] uppercase tracking-wider text-gray-400">
          Workspaces
        </div>
        {rows === null ? (
          <div className="px-3 py-2 text-xs text-gray-400 italic">
            loading…
          </div>
        ) : (
          <ul role="menu">
            {rows.map((w) => (
              <li key={w.id}>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => onSwitch(w)}
                  disabled={busyId !== null && busyId !== w.id}
                  className={
                    "w-full text-left px-3 py-2 text-sm flex items-center justify-between " +
                    (w.is_active
                      ? "bg-indigo-50/50 text-indigo-900 cursor-default"
                      : "text-gray-700 hover:bg-gray-50") +
                    (busyId === w.id ? " opacity-60" : "")
                  }
                >
                  <span className="truncate flex-1">{w.name}</span>
                  {w.is_active && (
                    <span
                      className="text-indigo-600 text-xs ml-2"
                      aria-label="active workspace"
                    >
                      ✓
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
        <button
          type="button"
          role="menuitem"
          onClick={() => setShowCreate(true)}
          className="block w-full text-left px-3 py-2 text-sm text-indigo-700 hover:bg-indigo-50/50"
        >
          + New workspace
        </button>
        {error && (
          <div className="px-3 py-2 text-xs text-red-600">{error}</div>
        )}
      </div>

      {showCreate && (
        <CreateWorkspaceModal
          onClose={() => setShowCreate(false)}
          onCreated={(created) => {
            // Phase 23.x (#218): same storage-patch + parent-notify
            // pattern as the switch path so the Header chip catches
            // up immediately.
            const ws = _toSessionWorkspace(created);
            setStoredWorkspace(ws);
            onWorkspaceChanged?.(ws);
            // The backend already flipped active; close the parent
            // dropdown + refresh so every component pulls the new
            // workspace's data.
            setShowCreate(false);
            onClose();
            router.refresh();
          }}
        />
      )}
    </>
  );
}

function CreateWorkspaceModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (created: WorkspaceMembership) => void;
}) {
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit() {
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Name is required.");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      const created = await createMyWorkspace(trimmed);
      onCreated(created);
    } catch (e) {
      setError((e as Error).message);
      setSubmitting(false);
    }
  }

  return (
    <div
      // Modal renders outside the header dropdown's DOM but inside the
      // app's React tree. z-index sits above the dropdown so the
      // backdrop covers it cleanly.
      className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-white rounded-lg shadow-xl w-full max-w-sm p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold text-gray-900">
            New workspace
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 text-lg leading-none"
            aria-label="Close"
          >
            ×
          </button>
        </div>
        <p className="text-xs text-gray-500 mb-3">
          A workspace is a clean container for one project. Bots, runs,
          secrets, and integrations all live inside it. You can switch
          between workspaces from this dropdown.
        </p>
        <label className="block text-xs font-medium text-gray-500 mb-1">
          Name
        </label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="JYNI"
          autoFocus
          onKeyDown={(e) => {
            if (e.key === "Enter") onSubmit();
          }}
          className="w-full text-sm rounded-md ring-1 ring-gray-300 px-3 py-2 mb-3 focus:outline-none focus:ring-2 focus:ring-indigo-600"
        />
        {error && (
          <div className="mb-3 text-sm text-red-600 bg-red-50 ring-1 ring-red-200 rounded px-3 py-2">
            {error}
          </div>
        )}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="text-sm px-3 py-1.5 rounded-md ring-1 ring-gray-300 text-gray-700 hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onSubmit}
            disabled={submitting}
            className="text-sm px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            {submitting ? "Creating…" : "Create + switch"}
          </button>
        </div>
      </div>
    </div>
  );
}
