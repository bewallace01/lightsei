"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  CostInsight,
  PolarisCostAnalysis,
  UnauthorizedError,
  fetchLatestPolarisCostAnalysis,
} from "./api";

const POLL_MS = 30000;
const AGENT_NAME = "polaris";

// Mirrors `dashboard/app/cost/insights/page.tsx` so the pulse style is
// recognizable across surfaces. "fix" = actionable optimization,
// "audit" = informational, "status" = healthy/no-action.
const KIND_META: Record<
  string,
  { label: string; tone: "fix" | "audit" | "status" }
> = {
  model_tier_mismatch: { label: "Model tier swap", tone: "fix" },
  per_trigger_roi: { label: "Low useful-rate", tone: "fix" },
  cache_skip_savings: { label: "Cache savings", tone: "audit" },
  failed_call_cost: { label: "Failed-call cost", tone: "audit" },
  plan_volatility: { label: "Plan volatility", tone: "status" },
};


function ToneChip({ tone, label }: { tone: "fix" | "audit" | "status"; label: string }) {
  const cls =
    tone === "fix"
      ? "bg-amber-100 text-amber-800"
      : tone === "status"
        ? "bg-emerald-100 text-emerald-800"
        : "bg-gray-100 text-gray-700";
  return (
    <span
      className={
        "text-[10px] uppercase tracking-wider font-medium px-2 py-0.5 rounded-full " +
        cls
      }
    >
      {label}
    </span>
  );
}


function InsightRow({ insight }: { insight: CostInsight }) {
  const meta = KIND_META[insight.kind] ?? {
    label: insight.kind,
    tone: "audit" as const,
  };
  return (
    <li className="rounded-md border border-gray-200 bg-white px-4 py-3 flex items-start gap-3">
      <ToneChip tone={meta.tone} label={meta.label} />
      <div className="flex-1 min-w-0">
        <p className="text-sm text-gray-900 leading-relaxed">
          {insight.headline}
        </p>
      </div>
      {insight.apply && (
        <Link
          href={insight.apply.href}
          className="text-xs text-accent-600 hover:text-accent-700 font-medium whitespace-nowrap"
        >
          {insight.apply.label} →
        </Link>
      )}
    </li>
  );
}


/**
 * Renders the latest `polaris.cost_analysis` event as a "Polaris
 * noticed about cost" section. Returns null while loading or when no
 * event has landed yet — the home page and /polaris both want the
 * section absent rather than empty when there's nothing to say.
 *
 * `compact` uses smaller header copy so the home page's existing
 * cadence (cost panel + recent runs) isn't disrupted.
 */
export default function PolarisCostAnalysisPanel({
  compact = false,
}: {
  compact?: boolean;
}) {
  const [data, setData] = useState<PolarisCostAnalysis | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [unauthorized, setUnauthorized] = useState(false);

  useEffect(() => {
    let alive = true;
    const refresh = async () => {
      try {
        const got = await fetchLatestPolarisCostAnalysis(AGENT_NAME);
        if (!alive) return;
        setData(got);
      } catch (e) {
        if (!alive) return;
        if (e instanceof UnauthorizedError) {
          setUnauthorized(true);
        }
        // All other errors are silently dropped: this is enrichment
        // and we don't want a flapping endpoint to render an error
        // banner on every page.
      } finally {
        if (alive) setLoaded(true);
      }
    };
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (unauthorized) return null;
  if (!loaded || data === null) return null;

  const insights = data.payload.insights ?? [];
  if (insights.length === 0) return null;

  // Sort fix-ables first so the user's eye lands on actionable items.
  const sorted = [...insights].sort((a, b) => {
    const order = (i: CostInsight) =>
      (KIND_META[i.kind]?.tone ?? "audit") === "fix" ? 0 : 1;
    return order(a) - order(b);
  });

  return (
    <section className={compact ? "mb-10" : "mb-12"}>
      <div className="flex items-baseline justify-between mb-3">
        <div>
          <h2
            className={
              "tracking-tight " +
              (compact
                ? "text-lg font-semibold"
                : "text-xl font-semibold")
            }
          >
            ✨ Polaris noticed about cost
          </h2>
          <p className="text-xs text-gray-500 mt-1">
            From the latest{" "}
            <code className="font-mono">polaris.cost_analysis</code> event.
            Audit window: last {data.payload.window_days} days.
          </p>
        </div>
        <Link
          href="/cost/insights"
          className="text-xs text-accent-600 hover:text-accent-700 font-medium whitespace-nowrap"
        >
          full insights →
        </Link>
      </div>
      <ul className="space-y-2">
        {sorted.map((insight, idx) => (
          <InsightRow key={`${insight.kind}-${idx}`} insight={insight} />
        ))}
      </ul>
    </section>
  );
}
