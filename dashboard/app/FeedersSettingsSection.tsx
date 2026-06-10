"use client";

import { useEffect, useState } from "react";
import {
  FeederSetting,
  UnauthorizedError,
  fetchFeeders,
  setFeederEnabled,
} from "./api";

/**
 * "Proactive feeders" settings: a per-workspace on/off for each feeder
 * (the weekly digest, the spend alert). Self-contained — fetches its own
 * data and owns its own state so it can drop into the settings page as a
 * single <section> without threading through the parent.
 *
 * Renders nothing if the workspace is unauthorized (the page handles auth
 * routing); otherwise shows a toggle per feeder.
 */
export default function FeedersSettingsSection() {
  const [feeders, setFeeders] = useState<FeederSetting[] | null>(null);
  const [busyKind, setBusyKind] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hidden, setHidden] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const got = await fetchFeeders();
        if (alive) setFeeders(got);
      } catch (e) {
        if (!alive) return;
        if (e instanceof UnauthorizedError) setHidden(true);
        else setError(String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  async function onToggle(kind: string, next: boolean) {
    setBusyKind(kind);
    setError(null);
    try {
      const updated = await setFeederEnabled(kind, next);
      setFeeders(updated);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyKind(null);
    }
  }

  if (hidden) return null;

  return (
    <section className="mb-10">
      <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
        Proactive feeders
      </h2>
      <div className="rounded-lg border border-gray-200 p-5">
        <p className="text-xs text-gray-500 mb-4">
          Feeders let your AI Business Team act on its own, without being
          asked. Turn any off if you would rather it stay quiet.
        </p>

        {error && (
          <div className="mb-3 text-xs text-red-700">{error}</div>
        )}

        {feeders === null ? (
          <div className="text-sm text-gray-400">loading…</div>
        ) : (
          <ul className="divide-y divide-gray-100">
            {feeders.map((f) => (
              <li
                key={f.kind}
                className="py-3 flex items-start justify-between gap-4 first:pt-0 last:pb-0"
              >
                <div className="min-w-0">
                  <div className="text-sm font-medium text-gray-900">
                    {f.name}
                  </div>
                  <div className="text-xs text-gray-500 mt-0.5">
                    {f.description}
                  </div>
                </div>
                <button
                  role="switch"
                  aria-checked={f.enabled}
                  aria-label={`Turn ${f.name} ${f.enabled ? "off" : "on"}`}
                  disabled={busyKind === f.kind}
                  onClick={() => onToggle(f.kind, !f.enabled)}
                  className={
                    "relative shrink-0 inline-flex h-6 w-11 items-center rounded-full transition-colors disabled:opacity-50 " +
                    (f.enabled ? "bg-accent-600" : "bg-gray-300")
                  }
                >
                  <span
                    className={
                      "inline-block h-4 w-4 transform rounded-full bg-white transition-transform " +
                      (f.enabled ? "translate-x-6" : "translate-x-1")
                    }
                  />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
