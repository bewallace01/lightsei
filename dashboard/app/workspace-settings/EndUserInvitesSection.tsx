"use client";

// Phase 27.3: operator surface for vendor invite-code management.
//
// Lives inside /workspace-settings. Three pieces:
//   - Newly-minted codes panel (shown once, copy-to-clipboard).
//   - Outstanding-codes list with per-code revoke.
//   - Mint form: count + TTL days + "Generate codes" button.
//
// The plaintext code is only ever shown in the mint response (same
// once-shown pattern as API keys). The list endpoint returns the
// code value too — these are not secrets in the cryptographic
// sense (they're single-use + short-lived + UUID-shaped), so we
// surface them in the list for copy convenience.

import { useCallback, useEffect, useState } from "react";

import {
  VendorInviteCode,
  fetchVendorInviteCodes,
  mintVendorInviteCodes,
  revokeVendorInviteCode,
} from "../api";

const DEFAULT_COUNT = 1;
const DEFAULT_TTL_DAYS = 30;
const MAX_COUNT = 10; // UI cap below backend's 100 to keep "shown once" panel sane
const MAX_TTL_DAYS = 365;

export default function EndUserInvitesSection() {
  const [codes, setCodes] = useState<VendorInviteCode[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [count, setCount] = useState(DEFAULT_COUNT);
  const [ttlDays, setTtlDays] = useState(DEFAULT_TTL_DAYS);
  const [minting, setMinting] = useState(false);
  // Plaintext codes from the most-recent mint, shown once at the top.
  // Cleared on next mint or on dismiss.
  const [justMinted, setJustMinted] = useState<VendorInviteCode[] | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [revokingCode, setRevokingCode] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const { codes } = await fetchVendorInviteCodes();
      setCodes(codes);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function onMint() {
    if (minting) return;
    setMinting(true);
    setError(null);
    try {
      const { codes: newCodes } = await mintVendorInviteCodes(count, ttlDays);
      setJustMinted(newCodes);
      // Re-fetch the full list so the new codes show up below too.
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setMinting(false);
    }
  }

  async function onRevoke(code: string) {
    if (revokingCode) return;
    setRevokingCode(code);
    setError(null);
    try {
      await revokeVendorInviteCode(code);
      // Drop locally; refresh in background for consistency.
      setCodes((prev) =>
        (prev ?? []).filter((c) => c.code !== code),
      );
      setJustMinted((prev) =>
        prev ? prev.filter((c) => c.code !== code) : prev,
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRevokingCode(null);
    }
  }

  async function copyCode(code: string) {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(code);
      setTimeout(() => setCopied((c) => (c === code ? null : c)), 1500);
    } catch {
      // Clipboard API can fail in non-HTTPS or older browsers; the
      // code is still visible on the page so the user can select +
      // copy manually.
    }
  }

  return (
    <section className="mb-10">
      <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
        End-user invites
      </h2>
      <div className="rounded-lg border border-gray-200 p-5">
        <p className="text-sm text-gray-600 mb-4">
          Generate single-use invite codes for end users to redeem at{" "}
          <span className="font-mono text-xs">/c/auth/magic-link</span>{" "}
          (or via the consumer signup flow). Each code links one end
          user to this workspace as a customer.
        </p>

        {error && (
          <div className="mb-4 p-3 border border-red-200 bg-red-50 text-red-700 text-xs rounded-md">
            {error}
          </div>
        )}

        {justMinted && justMinted.length > 0 && (
          <div className="mb-5 p-4 border border-indigo-200 bg-indigo-50 rounded-md">
            <div className="flex items-baseline justify-between mb-2">
              <div className="text-sm font-medium text-indigo-900">
                {justMinted.length === 1
                  ? "1 code minted — copy it now."
                  : `${justMinted.length} codes minted — copy them now.`}
              </div>
              <button
                type="button"
                onClick={() => setJustMinted(null)}
                className="text-xs text-indigo-700 hover:text-indigo-900"
              >
                Dismiss
              </button>
            </div>
            <div className="text-[11px] text-indigo-800 mb-3">
              The codes are also listed below; you can come back and
              copy them anytime, but this panel surfaces them while
              they&apos;re fresh.
            </div>
            <ul className="space-y-1.5">
              {justMinted.map((c) => (
                <li key={c.code} className="flex items-center gap-2">
                  <span className="flex-1 font-mono text-xs bg-white border border-indigo-200 px-2 py-1 rounded">
                    {c.code}
                  </span>
                  <button
                    type="button"
                    onClick={() => copyCode(c.code)}
                    className="text-xs text-indigo-700 hover:text-indigo-900 px-2 py-1"
                  >
                    {copied === c.code ? "Copied" : "Copy"}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Mint form */}
        <div className="mb-5 rounded-md bg-gray-50 p-4">
          <div className="text-xs font-medium text-gray-700 mb-2">
            Generate codes
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label className="block text-[11px] text-gray-500 mb-1">
                Count
              </label>
              <input
                type="number"
                min={1}
                max={MAX_COUNT}
                value={count}
                onChange={(e) =>
                  setCount(
                    Math.min(MAX_COUNT, Math.max(1, Number(e.target.value) || 1)),
                  )
                }
                className="w-20 text-sm rounded-md ring-1 ring-gray-300 px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-indigo-600"
              />
            </div>
            <div>
              <label className="block text-[11px] text-gray-500 mb-1">
                TTL (days)
              </label>
              <input
                type="number"
                min={1}
                max={MAX_TTL_DAYS}
                value={ttlDays}
                onChange={(e) =>
                  setTtlDays(
                    Math.min(
                      MAX_TTL_DAYS,
                      Math.max(1, Number(e.target.value) || DEFAULT_TTL_DAYS),
                    ),
                  )
                }
                className="w-24 text-sm rounded-md ring-1 ring-gray-300 px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-indigo-600"
              />
            </div>
            <button
              type="button"
              onClick={onMint}
              disabled={minting}
              className="text-sm px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              {minting ? "Generating…" : `Generate ${count > 1 ? `${count} codes` : "code"}`}
            </button>
          </div>
        </div>

        {/* Active codes list */}
        <div>
          <div className="text-xs font-medium text-gray-700 mb-2">
            Outstanding codes
          </div>
          {codes === null ? (
            <div className="text-xs text-gray-400 py-2">loading…</div>
          ) : codes.length === 0 ? (
            <div className="text-xs text-gray-500 py-2">
              No outstanding codes. Generate a few above.
            </div>
          ) : (
            <ul className="space-y-1.5">
              {codes.map((c) => (
                <li
                  key={c.code}
                  className="flex items-center gap-2 text-xs"
                >
                  <span className="flex-1 font-mono bg-white border border-gray-200 px-2 py-1 rounded text-gray-800">
                    {c.code}
                  </span>
                  <span className="text-gray-400" title={c.expires_at}>
                    expires {new Date(c.expires_at).toLocaleDateString()}
                  </span>
                  <button
                    type="button"
                    onClick={() => copyCode(c.code)}
                    className="text-indigo-600 hover:text-indigo-800 px-2"
                  >
                    {copied === c.code ? "Copied" : "Copy"}
                  </button>
                  <button
                    type="button"
                    onClick={() => onRevoke(c.code)}
                    disabled={revokingCode === c.code}
                    className="text-red-600 hover:text-red-800 px-2 disabled:opacity-50"
                  >
                    {revokingCode === c.code ? "…" : "Revoke"}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}
