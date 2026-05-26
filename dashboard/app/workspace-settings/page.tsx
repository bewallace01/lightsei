"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  UnauthorizedError,
  WorkspaceMembership,
  deleteMyWorkspace,
  handleAuthError,
  listMyWorkspaces,
  patchMyWorkspace,
} from "../api";
import EndUserInvitesSection from "./EndUserInvitesSection";

export default function WorkspaceSettingsPage() {
  const router = useRouter();
  const [rows, setRows] = useState<WorkspaceMembership[] | null>(null);
  const [active, setActive] = useState<WorkspaceMembership | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showDelete, setShowDelete] = useState(false);

  // Name editor state — local so the input stays controlled while
  // typing. Reset whenever the active workspace flips (e.g. after
  // the user switches via the dropdown).
  const [nameDraft, setNameDraft] = useState("");
  const [savingName, setSavingName] = useState(false);

  async function refresh() {
    try {
      setError(null);
      const out = await listMyWorkspaces();
      setRows(out);
      const a = out.find((w) => w.is_active) ?? null;
      setActive(a);
      if (a) setNameDraft(a.name);
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError((e as Error).message);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onSaveName() {
    if (!active) return;
    const trimmed = nameDraft.trim();
    if (!trimmed || trimmed === active.name) return;
    setSavingName(true);
    try {
      const updated = await patchMyWorkspace(active.id, { name: trimmed });
      setActive(updated);
      setRows((r) =>
        (r ?? []).map((w) => (w.id === updated.id ? updated : w)),
      );
      // No router.refresh() here — the workspace name is read by
      // the Header from localStorage, which is the existing
      // stale-header bug (#218). Renaming feels instant on this
      // page; the chip catches up on next full page load.
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSavingName(false);
    }
  }

  if (rows === null) {
    return (
      <main className="px-8 py-10 max-w-3xl mx-auto text-gray-400 text-sm">
        loading…
      </main>
    );
  }
  if (!active) {
    return (
      <main className="px-8 py-10 max-w-3xl mx-auto">
        <div className="text-sm text-gray-600">
          No active workspace. Open the workspace dropdown to pick one or
          create a new one.
        </div>
      </main>
    );
  }

  const isOwner = active.role === "owner";
  const isOnlyWorkspace = rows.length <= 1;

  return (
    <main className="px-8 py-10 max-w-3xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">
          Workspace settings
        </h1>
        <p className="text-sm text-gray-500 mt-1">
          Configure the workspace you&apos;re currently viewing.
        </p>
      </div>

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {/* ---------- Identity ---------- */}
      <section className="mb-10">
        <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
          Identity
        </h2>
        <div className="rounded-lg border border-gray-200 p-5 space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              Name
            </label>
            <div className="flex gap-2">
              <input
                type="text"
                value={nameDraft}
                onChange={(e) => setNameDraft(e.target.value)}
                disabled={!isOwner || savingName}
                className="flex-1 text-sm rounded-md ring-1 ring-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-600 disabled:bg-gray-50 disabled:text-gray-500"
              />
              {isOwner && (
                <button
                  type="button"
                  onClick={onSaveName}
                  disabled={
                    savingName ||
                    !nameDraft.trim() ||
                    nameDraft.trim() === active.name
                  }
                  className="text-sm px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50"
                >
                  {savingName ? "Saving…" : "Save"}
                </button>
              )}
            </div>
            {!isOwner && (
              <div className="text-xs text-gray-400 mt-1">
                Only the workspace owner can rename.
              </div>
            )}
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              Workspace ID
            </label>
            <input
              type="text"
              readOnly
              value={active.id}
              onFocus={(e) => e.target.select()}
              className="w-full font-mono text-xs rounded-md ring-1 ring-gray-300 px-3 py-2 bg-gray-50 text-gray-700"
            />
            <div className="text-xs text-gray-400 mt-1">
              Useful for support tickets. Read-only.
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              Plan tier
            </label>
            <span
              className={
                "inline-block px-2 py-0.5 rounded-full text-[11px] font-medium " +
                (active.plan_tier === "paid"
                  ? "bg-emerald-100 text-emerald-800"
                  : "bg-gray-100 text-gray-700")
              }
            >
              {active.plan_tier}
            </span>
          </div>
        </div>
      </section>

      {/* ---------- Members ---------- */}
      <section className="mb-10">
        <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
          Members
        </h2>
        <div className="rounded-lg border border-gray-200 p-5">
          <div className="text-sm text-gray-700">
            You ({active.role})
          </div>
          <div className="text-xs text-gray-400 mt-2">
            Inviting teammates ships in a follow-up. Today every workspace
            has exactly one member: the owner.
          </div>
        </div>
      </section>

      {/* ---------- End-user invites (Phase 27.3) ---------- */}
      <EndUserInvitesSection />

      {/* ---------- Danger zone ---------- */}
      <section>
        <h2 className="text-[11px] font-semibold text-red-700 mb-4 uppercase tracking-wider">
          Danger zone
        </h2>
        <div className="rounded-lg border border-red-200 p-5">
          <div className="text-sm font-medium text-gray-900 mb-1">
            Delete this workspace
          </div>
          <div className="text-xs text-gray-500 mb-3">
            Hard-deletes the workspace + every bot, run, event, trigger,
            and secret inside it. Cannot be undone. {isOnlyWorkspace
              ? "Create another workspace first — Lightsei needs you to have at least one."
              : "You'll auto-switch to another workspace you belong to."}
          </div>
          <button
            type="button"
            disabled={!isOwner || isOnlyWorkspace}
            onClick={() => setShowDelete(true)}
            className="text-sm px-3 py-1.5 rounded-md bg-red-600 text-white hover:bg-red-500 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Delete workspace
          </button>
          {!isOwner && (
            <div className="text-xs text-gray-400 mt-2">
              Only the workspace owner can delete.
            </div>
          )}
        </div>
      </section>

      {showDelete && (
        <DeleteConfirmModal
          workspace={active}
          onClose={() => setShowDelete(false)}
          onDeleted={() => {
            // Backend auto-switched the session to another workspace
            // (since we gated this button on !isOnlyWorkspace). Refresh
            // so every component picks up the new active workspace.
            router.refresh();
            // And go to the home page in the new workspace.
            router.push("/");
          }}
        />
      )}
    </main>
  );
}

function DeleteConfirmModal({
  workspace,
  onClose,
  onDeleted,
}: {
  workspace: WorkspaceMembership;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [typed, setTyped] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const armed = typed.trim() === workspace.name;

  async function onConfirm() {
    if (!armed) return;
    setSubmitting(true);
    setError(null);
    try {
      await deleteMyWorkspace(workspace.id);
      onDeleted();
    } catch (e) {
      setError((e as Error).message);
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-white rounded-lg shadow-xl w-full max-w-md p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold text-gray-900">
            Delete workspace
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
        <p className="text-sm text-gray-700 mb-2">
          This will permanently delete{" "}
          <span className="font-mono font-medium">{workspace.name}</span> and
          every bot, run, event, trigger, and secret inside it.
        </p>
        <p className="text-xs text-gray-500 mb-3">
          Type the workspace name to confirm.
        </p>
        <input
          type="text"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder={workspace.name}
          autoFocus
          className="w-full text-sm rounded-md ring-1 ring-gray-300 px-3 py-2 mb-3 focus:outline-none focus:ring-2 focus:ring-red-600"
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
            onClick={onConfirm}
            disabled={!armed || submitting}
            className="text-sm px-3 py-1.5 rounded-md bg-red-600 text-white hover:bg-red-500 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting ? "Deleting…" : "Delete workspace"}
          </button>
        </div>
      </div>
    </div>
  );
}
