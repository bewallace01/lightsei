"use client";

/**
 * Phase 11B.4: the cost panel.
 *
 * Lives below the shared sky on the home page. Reads the cost
 * telemetry endpoint shipped in 11B.1 (no extra backend work for
 * this widget) and renders:
 *
 *   - Top row: workspace MTD spend (big tabular numerals), projected
 *     EOM in muted gray, monthly budget bar underneath when one is
 *     set.
 *   - Per-agent breakdown table: small star icon (same hash → palette
 *     tint as the constellation map, so an Atlas line in the cost
 *     panel reads as the same agent that's a violet sparkle in the
 *     map above) + agent name + model + run count + MTD cost +
 *     projected EOM cost. Sortable by MTD cost desc by default.
 *   - Per-model summary row at the bottom — sets up the Phase 12
 *     demo where swapping providers shows the cost shift in this row.
 *
 * Polls every 30s (cost moves slowly enough that 5s polling like the
 * hero/map is wasted). Pause-on-hidden so background tabs don't burn
 * the API.
 *
 * Empty state: when MTD = 0 the panel shows "No spend yet. Polaris's
 * first tick will land here." — same vibe as the constellation map's
 * "Sky empty" state, mirrored phrasing.
 */

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  fetchWorkspaceCost,
  UnauthorizedError,
  WorkspaceCost,
  WorkspaceCostByAgent,
} from "./api";
import { sparklePath, tintForAgent } from "./stars";

function fmtUsd(n: number): string {
  if (!Number.isFinite(n)) return "—";
  // Show fractional cents under $1 so a tiny first run doesn't read
  // as $0.00. Switch to two-decimal at $1+ where the extra precision
  // would just be noise.
  if (n < 1) return `$${n.toFixed(4)}`;
  if (n < 100) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
}

function fmtPct(n: number | null): string {
  if (n === null || !Number.isFinite(n)) return "—";
  return `${Math.round(n)}%`;
}

function StarBullet({ name, size = 8 }: { name: string; size?: number }) {
  // Inline SVG sparkle in the agent's palette color. Used as a
  // bullet next to the agent name in the per-agent breakdown so
  // the cost row visually ties to the constellation map row.
  return (
    <svg
      width={size * 2 + 4}
      height={size * 2 + 4}
      viewBox={`0 0 ${size * 2 + 4} ${size * 2 + 4}`}
      aria-hidden="true"
      className="inline-block shrink-0"
    >
      <path
        d={sparklePath(size + 2, size + 2, size, 0.3)}
        fill={tintForAgent(name)}
      />
    </svg>
  );
}

export default function CostPanel() {
  const [data, setData] = useState<WorkspaceCost | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sortAgentsBy, setSortAgentsBy] =
    useState<"mtd_usd" | "run_count" | "agent_name">("mtd_usd");

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      if (document.visibilityState === "hidden") {
        if (alive) timer = setTimeout(tick, 30000);
        return;
      }
      try {
        const fresh = await fetchWorkspaceCost();
        if (!alive) return;
        setData(fresh);
        setError(null);
      } catch (e) {
        if (!alive) return;
        if (e instanceof UnauthorizedError) {
          // Parent page handles the redirect.
          return;
        }
        setError(String(e));
      } finally {
        if (alive) timer = setTimeout(tick, 30000);
      }
    };

    tick();
    const onVis = () => {
      if (document.visibilityState === "visible" && alive) {
        if (timer) clearTimeout(timer);
        tick();
      }
    };
    document.addEventListener("visibilitychange", onVis);

    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  const sortedAgents = useMemo<WorkspaceCostByAgent[]>(() => {
    if (!data) return [];
    const out = [...data.by_agent];
    out.sort((a, b) => {
      if (sortAgentsBy === "mtd_usd") return b.mtd_usd - a.mtd_usd;
      if (sortAgentsBy === "run_count") return b.run_count - a.run_count;
      return a.agent_name.localeCompare(b.agent_name);
    });
    return out;
  }, [data, sortAgentsBy]);

  const totalForModelPct = data?.mtd_usd ?? 0;

  return (
    <section
      className="rounded-lg border border-indigo-900/40 bg-white shadow-sm p-6"
      aria-label="Cost"
    >
      <div className="flex items-baseline justify-between gap-3 mb-1">
        <span className="text-[11px] uppercase tracking-[0.18em] text-indigo-700 font-medium">
          Cost
        </span>
        <span className="text-xs text-gray-400">
          month-to-date · refreshes every 30s
        </span>
      </div>

      {error && (
        <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      )}

      {data === null && !error && (
        <div className="mt-6 text-sm text-gray-400">loading…</div>
      )}

      {data !== null && data.mtd_usd === 0 && (
        <div className="mt-4 rounded-md border border-dashed border-gray-200 bg-gray-50/50 px-4 py-6 text-center">
          <div className="font-serif italic text-gray-700">
            No spend yet.
          </div>
          <p className="text-xs text-gray-500 mt-1">
            Polaris&apos;s first tick will land here.
          </p>
        </div>
      )}

      {data !== null && data.mtd_usd > 0 && (
        <>
          {/* Top row: MTD spend big, projected EOM muted, budget bar */}
          <div className="mt-3 flex items-baseline gap-3 flex-wrap">
            <span className="font-mono tabular-nums text-3xl text-gray-900">
              {fmtUsd(data.mtd_usd)}
            </span>
            <span className="text-sm text-gray-500">
              <span className="font-mono tabular-nums">
                {fmtUsd(data.projected_eom_usd)}
              </span>{" "}
              projected EOM
            </span>
          </div>

          {/* Budget bar — only when a cap is set */}
          {data.budget_usd_monthly !== null ? (
            <div className="mt-4">
              <div className="flex items-baseline justify-between text-xs text-gray-500 mb-1.5">
                <span>
                  budget{" "}
                  <span className="font-mono tabular-nums">
                    {fmtUsd(data.budget_usd_monthly)}
                  </span>
                  /month
                </span>
                <span
                  className={
                    data.budget_used_pct !== null && data.budget_used_pct >= 80
                      ? "text-amber-600 font-medium"
                      : "text-gray-500"
                  }
                >
                  {fmtPct(data.budget_used_pct)} used
                </span>
              </div>
              <div className="h-1.5 rounded-full bg-gray-100 overflow-hidden">
                <div
                  className={
                    "h-full rounded-full transition-all duration-500 " +
                    (data.budget_used_pct !== null &&
                    data.budget_used_pct >= 100
                      ? "bg-red-400"
                      : data.budget_used_pct !== null &&
                        data.budget_used_pct >= 80
                      ? "bg-amber-400"
                      : "bg-indigo-500")
                  }
                  style={{
                    width: `${Math.min(
                      100,
                      Math.max(0, data.budget_used_pct ?? 0),
                    )}%`,
                  }}
                />
              </div>
            </div>
          ) : (
            <Link
              href="/account"
              className="mt-3 inline-block text-xs text-gray-500 hover:text-indigo-700"
            >
              No monthly cap set ·{" "}
              <span className="underline underline-offset-2">configure →</span>
            </Link>
          )}

          {/* Per-agent breakdown */}
          {sortedAgents.length > 0 && (
            <div className="mt-6">
              <div className="flex items-baseline justify-between mb-2">
                <span className="text-[11px] uppercase tracking-wider text-gray-500 font-medium">
                  By agent
                </span>
                <div className="flex items-baseline gap-3 text-[11px] text-gray-400">
                  <span>sort:</span>
                  {(["mtd_usd", "run_count", "agent_name"] as const).map(
                    (k) => (
                      <button
                        key={k}
                        type="button"
                        onClick={() => setSortAgentsBy(k)}
                        className={
                          sortAgentsBy === k
                            ? "text-indigo-700 font-medium"
                            : "text-gray-400 hover:text-gray-700"
                        }
                      >
                        {k === "mtd_usd"
                          ? "cost"
                          : k === "run_count"
                          ? "runs"
                          : "name"}
                      </button>
                    ),
                  )}
                </div>
              </div>
              <ul className="divide-y divide-gray-100 border-y border-gray-100">
                {sortedAgents.map((a) => {
                  const pct =
                    data.mtd_usd > 0 ? (a.mtd_usd / data.mtd_usd) * 100 : 0;
                  return (
                    <li
                      key={a.agent_name}
                      className="py-2.5 flex items-center justify-between gap-3"
                    >
                      <Link
                        href={`/agents/${encodeURIComponent(a.agent_name)}`}
                        className="flex items-center gap-2 min-w-0 group"
                      >
                        <StarBullet name={a.agent_name} />
                        <span className="font-mono text-sm text-gray-800 group-hover:text-indigo-700 truncate">
                          {a.agent_name}
                        </span>
                        <span className="text-xs text-gray-400 font-mono">
                          {a.run_count} runs
                        </span>
                      </Link>
                      <div className="flex items-baseline gap-3 shrink-0">
                        <span className="text-[11px] text-gray-400 tabular-nums">
                          {pct >= 1 ? `${Math.round(pct)}%` : ""}
                        </span>
                        <span className="font-mono tabular-nums text-sm text-gray-900">
                          {fmtUsd(a.mtd_usd)}
                        </span>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}

          {/* Per-model summary — Phase 12's multi-provider work makes
              this row interesting (Atlas-on-Haiku vs Atlas-on-Llama-Groq
              would show the cost shift here at a glance). */}
          {data.by_model.length > 0 && (
            <div className="mt-5">
              <span className="text-[11px] uppercase tracking-wider text-gray-500 font-medium">
                By model
              </span>
              <div className="mt-2 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs">
                {data.by_model.map((m) => {
                  const pct =
                    totalForModelPct > 0
                      ? Math.round((m.mtd_usd / totalForModelPct) * 100)
                      : 0;
                  return (
                    <span
                      key={m.model}
                      className="inline-flex items-baseline gap-1.5"
                    >
                      <span className="font-mono text-gray-800">
                        {m.model}
                      </span>
                      <span className="font-mono tabular-nums text-gray-700">
                        {fmtUsd(m.mtd_usd)}
                      </span>
                      <span className="text-gray-400">({pct}%)</span>
                    </span>
                  );
                })}
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
