"use client";

// Phase 26.x close-out: operator UI for claiming the workspace's
// consumer-chat URL handle (vendor_slug). Backend endpoint shipped
// in Phase 26.1, but the dashboard form was never built, so
// operators had no way to set this without a direct DB UPDATE.
//
// Owner-only. Slug rules mirror backend/models.is_valid_vendor_slug:
// 3-32 chars, lowercase letters / digits / dashes, no leading or
// trailing dash. Once claimed, the chat lives at /c/{slug}.

import { useEffect, useState } from "react";

import { WorkspaceMembership, claimVendorSlug } from "../api";

// Mirror of backend is_valid_vendor_slug for client-side feedback.
// Backend re-validates; this is just to gate the Save button + show
// a hint while the operator types.
const SLUG_REGEX = /^[a-z0-9](?:[a-z0-9-]{1,30}[a-z0-9])?$/;

function isValidSlug(slug: string): boolean {
  return SLUG_REGEX.test(slug);
}

export default function VendorSlugSection({
  active,
  isOwner,
  onClaimed,
}: {
  active: WorkspaceMembership;
  isOwner: boolean;
  onClaimed: () => void | Promise<void>;
}) {
  const [draft, setDraft] = useState<string>(active.vendor_slug ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [justSaved, setJustSaved] = useState(false);

  // Reset the local draft whenever the active workspace flips (e.g.
  // operator switched via the dropdown).
  useEffect(() => {
    setDraft(active.vendor_slug ?? "");
    setError(null);
    setJustSaved(false);
  }, [active.id, active.vendor_slug]);

  const trimmed = draft.trim();
  const looksValid = isValidSlug(trimmed);
  const isUnchanged = trimmed === (active.vendor_slug ?? "");
  const canSave =
    isOwner && !saving && trimmed.length > 0 && looksValid && !isUnchanged;

  async function onSave() {
    if (!canSave) return;
    setSaving(true);
    setError(null);
    setJustSaved(false);
    try {
      await claimVendorSlug(trimmed);
      setJustSaved(true);
      await onClaimed();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  const chatUrl =
    typeof window !== "undefined" && active.vendor_slug
      ? `${window.location.origin}/c/${active.vendor_slug}`
      : null;

  return (
    <section className="mb-10">
      <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
        Consumer chat URL
      </h2>
      <div className="rounded-lg border border-gray-200 p-5 space-y-4">
        <div className="text-xs text-gray-500">
          Claim the URL your end users will visit to chat with this
          workspace&apos;s bots. Once set, the chat lives at{" "}
          <span className="font-mono text-gray-700">
            /c/your-slug
          </span>
          . Lowercase letters, digits, and dashes only (3 to 32 chars).
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">
            Slug
          </label>
          <div className="flex gap-2">
            <input
              type="text"
              value={draft}
              onChange={(e) => {
                // Force lowercase as the user types so the hint
                // doesn't fight upper-case input.
                setDraft(e.target.value.toLowerCase());
                setJustSaved(false);
              }}
              disabled={!isOwner || saving}
              placeholder="acme-support"
              className="flex-1 font-mono text-sm rounded-md ring-1 ring-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-600 disabled:bg-gray-50 disabled:text-gray-500"
            />
            {isOwner && (
              <button
                type="button"
                onClick={onSave}
                disabled={!canSave}
                className="text-sm px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                {saving ? "Saving…" : active.vendor_slug ? "Update" : "Claim"}
              </button>
            )}
          </div>

          {trimmed.length > 0 && !looksValid && (
            <div className="text-xs text-amber-700 mt-1">
              Slug must be 3 to 32 chars, lowercase letters / digits /
              dashes, no leading or trailing dash.
            </div>
          )}
          {!isOwner && (
            <div className="text-xs text-gray-400 mt-1">
              Only the workspace owner can claim the slug.
            </div>
          )}
          {error && (
            <div className="text-xs text-red-600 mt-2">{error}</div>
          )}
          {justSaved && !error && (
            <div className="text-xs text-emerald-700 mt-2">Saved.</div>
          )}
        </div>

        {active.vendor_slug && (
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">
              Live URL
            </label>
            <input
              type="text"
              readOnly
              value={chatUrl ?? `/c/${active.vendor_slug}`}
              onFocus={(e) => e.target.select()}
              className="w-full font-mono text-xs rounded-md ring-1 ring-gray-300 px-3 py-2 bg-gray-50 text-gray-700"
            />
            <div className="text-xs text-gray-400 mt-1">
              Share this with end users. They&apos;ll land on the chat
              page after signing in.
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
