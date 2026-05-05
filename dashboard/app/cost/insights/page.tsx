"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  CostInsight,
  UnauthorizedError,
  fetchCostInsights,
  patchAgent,
} from "../../api";


// The five insight kinds we render today; the order here is the order
// the backend already returns. Mapping to a user-friendly label + an
// indication of whether the row is an actionable optimization, an
// informational audit, or a status check.
const KIND_META: Record<string, { label: string; tone: "fix" | "audit" | "status" }> = {
  model_tier_mismatch: { label: "Model tier swap", tone: "fix" },
  per_trigger_roi: { label: "Low useful-rate", tone: "fix" },
  cache_skip_savings: { label: "Cache savings", tone: "audit" },
  failed_call_cost: { label: "Failed-call cost", tone: "audit" },
  plan_volatility: { label: "Plan volatility", tone: "status" },
};


function fmtUsd(n: number): string {
  if (n === 0) return "$0";
  if (n < 0.01) return "<$0.01";
  if (n < 1) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
}


export default function CostInsightsPage() {
  const router = useRouter();
  const [insights, setInsights] = useState<CostInsight[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [applyingKey, setApplyingKey] = useState<string | null>(null);
  const [appliedKeys, setAppliedKeys] = useState<Set<string>>(new Set());

  const refresh = async () => {
    try {
      const data = await fetchCostInsights();
      setInsights(data);
      setError(null);
    } catch (e) {
      if (e instanceof UnauthorizedError) {
        router.replace("/login");
        return;
      }
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    // Insights are derived from data the rest of the dashboard already
    // polls; refreshing every 30s keeps this page in sync without
    // overloading.
    const id = setInterval(refresh, 30000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onApply = async (insight: CostInsight, key: string) => {
    if (!insight.apply) return;
    const patch = insight.apply.patch;
    const detail = insight.detail as Record<string, unknown>;
    // For model-tier-swap insights we know the agent + the patch; do
    // the PATCH directly. For others (no patch field), the apply link
    // is just a navigation suggestion — fall through to opening it.
    if (patch && typeof detail.agent === "string") {
      setApplyingKey(key);
      try {
        await patchAgent(detail.agent, patch as Parameters<typeof patchAgent>[1]);
        setAppliedKeys((cur) => new Set(cur).add(key));
        await refresh();
      } catch (e) {
        setError(String(e instanceof Error ? e.message : e));
      } finally {
        setApplyingKey(null);
      }
    }
  };

  const hasApplyButton = (insight: CostInsight): boolean => {
    return Boolean(
      insight.apply?.patch &&
      typeof (insight.detail as Record<string, unknown>).agent === "string",
    );
  };

  return (
    <main className="px-8 py-10 max-w-4xl mx-auto">
      <div className="mb-8">
        <Link
          href="/cost"
          className="text-sm text-gray-500 hover:text-gray-900"
        >
          ← cost
        </Link>
      </div>

      <h1 className="text-2xl font-semibold tracking-tight mb-2">
        Cost insights
      </h1>
      <p className="text-sm text-gray-500 mb-8 max-w-2xl">
        Where your dollars went over the last 30 days, what was wasted,
        and one-click fixes where they exist. All numbers are computed
        from the runs + events the dashboard already records — no extra
        billing API calls.
      </p>

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-gray-400 text-sm">loading…</div>
      ) : insights.length === 0 ? (
        <div className="border border-dashed border-gray-200 rounded-lg p-10 text-center text-sm text-gray-500">
          No insights yet — check back after Polaris and the team have
          had a few days of activity.
        </div>
      ) : (
        <ul className="space-y-3">
          {insights.map((insight, idx) => {
            const key = `${insight.kind}-${idx}`;
            const meta = KIND_META[insight.kind] ?? {
              label: insight.kind,
              tone: "audit" as const,
            };
            const applied = appliedKeys.has(key);
            const isApplying = applyingKey === key;
            return (
              <li
                key={key}
                className="rounded-lg border border-gray-200 bg-white px-5 py-4"
              >
                <div className="flex items-baseline justify-between gap-4 mb-2">
                  <span
                    className={
                      "text-[10px] uppercase tracking-wider font-medium px-2 py-0.5 rounded-full " +
                      (meta.tone === "fix"
                        ? "bg-amber-100 text-amber-800"
                        : meta.tone === "status"
                          ? "bg-emerald-100 text-emerald-800"
                          : "bg-gray-100 text-gray-700")
                    }
                  >
                    {meta.label}
                  </span>
                  {applied && (
                    <span className="text-xs text-emerald-700">applied ✓</span>
                  )}
                </div>
                <p className="text-sm text-gray-900 leading-relaxed">
                  {insight.headline}
                </p>

                {/* Detail dump for transparency — small font, muted. */}
                <details className="mt-2">
                  <summary className="text-[11px] text-gray-400 cursor-pointer hover:text-gray-600 select-none">
                    detail
                  </summary>
                  <pre className="mt-1 font-mono text-[11px] bg-gray-50 border border-gray-100 rounded p-2 overflow-x-auto text-gray-700">
                    {JSON.stringify(insight.detail, null, 2)}
                  </pre>
                </details>

                {insight.apply && (
                  <div className="mt-3 flex items-center justify-end gap-3">
                    {hasApplyButton(insight) ? (
                      <>
                        <Link
                          href={insight.apply.href}
                          className="text-xs text-gray-500 hover:text-gray-900"
                        >
                          review on agent page
                        </Link>
                        <button
                          type="button"
                          onClick={() => onApply(insight, key)}
                          disabled={isApplying || applied}
                          className="px-3 py-1.5 text-xs bg-accent-600 text-white rounded-md hover:bg-accent-700 disabled:opacity-50 transition-colors"
                        >
                          {isApplying ? "applying…" : applied ? "applied" : "apply"}
                        </button>
                      </>
                    ) : (
                      <Link
                        href={insight.apply.href}
                        className="text-xs text-accent-600 hover:text-accent-700 font-medium"
                      >
                        {insight.apply.label}
                      </Link>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <p className="text-xs text-gray-400 mt-8">
        Phase 12D.1 ships the read-only audit. Phase 12D.2 will have
        Polaris narrate this analysis directly in its plan events;
        Phase 12D.3 will auto-tune the safe knobs (model tier, tick
        interval) with revert-on-regression once the eval layer is in
        place. For now the apply buttons are scoped to model swaps —
        anything more invasive routes to the agent page for a
        considered review.
      </p>
    </main>
  );
}
