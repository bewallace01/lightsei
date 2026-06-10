"use client";

import { useEffect, useState } from "react";
import {
  FeederDigestStatus,
  UnauthorizedError,
  fetchFeederDigestStatus,
  runFeederDigest,
} from "./api";

const POLL_MS = 30000;

function relTime(iso: string): string {
  try {
    const then = new Date(iso).getTime();
    const mins = Math.round((Date.now() - then) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.round(hrs / 24);
    return `${days}d ago`;
  } catch {
    return iso;
  }
}

/**
 * The home-page "weekly business digest" card. This is the visible face
 * of the feeder: the BI assistant writes a plain-English summary of the
 * week on a cadence (no one has to ask), and this card surfaces it plus a
 * "generate now" button for an on-demand pull.
 *
 * Renders null while loading, when unauthorized, and when the workspace
 * has never produced a digest AND has no BI assistant to make one — a
 * fresh workspace shouldn't see an empty promise. Once a digest has ever
 * been queued, the card stays so the owner can pull a new one.
 */
export default function WeeklyDigestPanel({
  compact = false,
}: {
  compact?: boolean;
}) {
  const [data, setData] = useState<FeederDigestStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [unauthorized, setUnauthorized] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const got = await fetchFeederDigestStatus();
      setData(got);
    } catch (e) {
      if (e instanceof UnauthorizedError) setUnauthorized(true);
      // Other errors are dropped: this is enrichment, not a core panel.
    } finally {
      setLoaded(true);
    }
  };

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (!alive) return;
      await refresh();
    };
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const onGenerate = async () => {
    setBusy(true);
    setNote(null);
    try {
      const res = await runFeederDigest();
      setNote(
        res.note ??
          "Queued. Your Business Intelligence assistant is writing the summary.",
      );
      await refresh();
    } catch (e) {
      setNote(`Could not queue a digest: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  if (unauthorized) return null;
  if (!loaded || data === null) return null;

  const summary = data.latest_summary;
  const hasHistory = data.last_digest !== null || summary !== null;
  // Nothing produced and nothing ever queued: stay quiet on a fresh
  // workspace rather than advertise a feature with no content behind it.
  if (!hasHistory) return null;

  return (
    <section className={compact ? "mb-10" : "mb-12"}>
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <h2
            className={
              "tracking-tight " +
              (compact ? "text-lg font-semibold" : "text-xl font-semibold")
            }
          >
            📊 Weekly business digest
          </h2>
          <p className="text-xs text-gray-500 mt-1">
            Your Business Intelligence assistant summarizes the last{" "}
            {data.period_days} days, on its own.
            {data.last_digest && (
              <>
                {" "}
                Last run {relTime(data.last_digest.created_at)} (
                {data.last_digest.status}).
              </>
            )}
          </p>
        </div>
        <button
          onClick={onGenerate}
          disabled={busy}
          className="text-xs px-3 py-1.5 rounded-md border border-accent-600 text-accent-600 font-medium hover:bg-accent-50 disabled:opacity-50 whitespace-nowrap"
        >
          {busy ? "Queuing…" : "Generate now"}
        </button>
      </div>

      {note && (
        <p className="text-xs text-gray-500 mb-2 italic">{note}</p>
      )}

      {summary && summary.text ? (
        <div className="rounded-md border border-gray-200 bg-white px-4 py-3">
          <p className="text-sm text-gray-900 leading-relaxed whitespace-pre-wrap">
            {summary.text}
          </p>
          <p className="text-[11px] text-gray-400 mt-2">
            Written {relTime(summary.produced_at)}
          </p>
        </div>
      ) : (
        <div className="rounded-md border border-dashed border-gray-200 px-4 py-3 text-sm text-gray-500">
          No summary yet. The assistant writes one after its next run.
        </div>
      )}
    </section>
  );
}
