"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  PolarisPlan,
  PolarisValidation,
  PolarisViolation,
  UnauthorizedError,
  ValidationStatus,
  fetchEventValidations,
  fetchPolarisPlans,
  handleAuthError,
  worstValidationStatus,
} from "../api";
import PolarisCostAnalysisPanel from "../PolarisCostAnalysisPanel";

const AGENT_NAME = "polaris";
const POLL_MS = 30000;

function fmtAbs(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function fmtRelative(iso: string): string {
  try {
    const ts = new Date(iso).getTime();
    const now = Date.now();
    const diff = Math.max(0, now - ts);
    const m = Math.round(diff / 60000);
    if (m < 1) return "just now";
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    const d = Math.round(h / 24);
    return `${d}d ago`;
  } catch {
    return "";
  }
}

function Star({ className }: { className?: string }) {
  // 4-pointed celestial star — distinct from the 5-pointed stars used elsewhere.
  return (
    <svg viewBox="0 0 24 24" className={className} fill="currentColor">
      <path d="M12 2 L13.6 10.4 L22 12 L13.6 13.6 L12 22 L10.4 13.6 L2 12 L10.4 10.4 Z" />
    </svg>
  );
}

function StarField() {
  // Hand-placed faint stars for the hero band. Deterministic so it doesn't
  // jitter on every render. Positioned in % so the band scales cleanly.
  const dots = [
    { top: "12%", left: "8%", size: 1, opacity: 0.6 },
    { top: "28%", left: "22%", size: 2, opacity: 0.4 },
    { top: "65%", left: "14%", size: 1, opacity: 0.5 },
    { top: "18%", left: "44%", size: 1, opacity: 0.3 },
    { top: "78%", left: "38%", size: 2, opacity: 0.5 },
    { top: "32%", left: "62%", size: 1, opacity: 0.4 },
    { top: "8%", left: "78%", size: 1, opacity: 0.5 },
    { top: "55%", left: "72%", size: 2, opacity: 0.3 },
    { top: "82%", left: "84%", size: 1, opacity: 0.6 },
    { top: "42%", left: "88%", size: 1, opacity: 0.4 },
    { top: "70%", left: "56%", size: 1, opacity: 0.3 },
    { top: "22%", left: "92%", size: 1, opacity: 0.5 },
  ];
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden">
      {dots.map((d, i) => (
        <span
          key={i}
          className="absolute rounded-full bg-white"
          style={{
            top: d.top,
            left: d.left,
            width: `${d.size}px`,
            height: `${d.size}px`,
            opacity: d.opacity,
          }}
        />
      ))}
    </div>
  );
}

function CopyableCommand({ label, command }: { label: string; command: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // ignore — user can still hand-select.
    }
  };
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-gray-500 mb-1.5">
        {label}
      </div>
      <div className="flex items-center gap-2 rounded-md border border-gray-200 bg-gray-50 px-3 py-2">
        <code className="flex-1 font-mono text-xs text-gray-800 overflow-x-auto whitespace-nowrap">
          {command}
        </code>
        <button
          type="button"
          onClick={onCopy}
          className="text-[11px] uppercase tracking-wider text-indigo-600 hover:text-indigo-800 transition-colors shrink-0"
        >
          {copied ? "copied" : "copy"}
        </button>
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold tracking-tight text-gray-900">
          Polaris is dark.
        </h2>
        <p className="text-sm text-gray-600 mt-1.5 leading-relaxed">
          The orchestrator hasn&apos;t reported in yet. It reads your
          project&apos;s MEMORY.md and TASKS.md and proposes the next moves.
          Three steps to get it running:
        </p>
      </div>

      <ol className="space-y-5">
        <li className="flex gap-4">
          <span className="shrink-0 w-7 h-7 rounded-full bg-indigo-100 text-indigo-700 flex items-center justify-center text-sm font-semibold">
            1
          </span>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium text-gray-900 mb-2">
              Build the lightsei wheel
            </div>
            <CopyableCommand
              label="run from repo root"
              command="cd sdk && python -m build --wheel && cp dist/*.whl ../polaris/"
            />
          </div>
        </li>

        <li className="flex gap-4">
          <span className="shrink-0 w-7 h-7 rounded-full bg-indigo-100 text-indigo-700 flex items-center justify-center text-sm font-semibold">
            2
          </span>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium text-gray-900 mb-2">
              Store ANTHROPIC_API_KEY as a workspace secret
            </div>
            <p className="text-sm text-gray-600 mb-2 leading-relaxed">
              The worker injects every workspace secret into the bot&apos;s
              env. Polaris uses the Anthropic API for the planning call;
              LIGHTSEI_API_KEY also needs to be set so the bot can post
              events back.
            </p>
            <Link
              href="/account"
              className="text-xs text-indigo-600 hover:text-indigo-800 transition-colors"
            >
              manage secrets in account settings →
            </Link>
          </div>
        </li>

        <li className="flex gap-4">
          <span className="shrink-0 w-7 h-7 rounded-full bg-indigo-100 text-indigo-700 flex items-center justify-center text-sm font-semibold">
            3
          </span>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium text-gray-900 mb-2">
              Deploy via the Lightsei CLI
            </div>
            <CopyableCommand
              label="from a terminal with LIGHTSEI_API_KEY set"
              command="lightsei deploy ./polaris --agent polaris"
            />
          </div>
        </li>
      </ol>

      <div className="mt-8 pt-6 border-t border-gray-100">
        <div className="text-[11px] uppercase tracking-wider text-gray-500 mb-2">
          What Polaris does
        </div>
        <p className="text-sm text-gray-600 leading-relaxed">
          Read-only orchestrator. On each tick (default hourly), Polaris
          reads MEMORY.md + TASKS.md, calls Claude with an orchestrator
          prompt, and emits a structured plan: a 1-2 sentence state
          summary, 3-5 next actions, parking-lot promotion candidates,
          and any drift it spots between docs and the Done Log. Output
          is visible-only — Polaris doesn&apos;t open PRs or dispatch
          commands. Acting on the plan lands in Phase 6B.
        </p>
      </div>
    </div>
  );
}

// Tailwind class sets for each validation status. Kept in one place so
// the sidebar chip and the detail panel header use the same colors.
const STATUS_STYLES: Record<
  ValidationStatus | "unchecked",
  { chip: string; label: string }
> = {
  pass: { chip: "bg-emerald-50 text-emerald-700 border-emerald-200", label: "PASS" },
  warn: { chip: "bg-amber-50 text-amber-800 border-amber-200", label: "WARN" },
  fail: { chip: "bg-red-50 text-red-700 border-red-200", label: "FAIL" },
  error: { chip: "bg-red-50 text-red-700 border-red-200", label: "ERROR" },
  timeout: { chip: "bg-amber-50 text-amber-800 border-amber-200", label: "TIMEOUT" },
  unchecked: { chip: "bg-gray-50 text-gray-500 border-gray-200", label: "—" },
};

function StatusChip({
  status,
  className = "",
}: {
  status: ValidationStatus | "unchecked";
  className?: string;
}) {
  const s = STATUS_STYLES[status];
  return (
    <span
      className={
        "inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold tracking-wide border " +
        s.chip + " " + className
      }
    >
      {s.label}
    </span>
  );
}

function ViolationItem({ v }: { v: PolarisViolation }) {
  return (
    <li className="rounded-md bg-gray-50 border border-gray-200 px-3 py-2">
      <div className="flex items-baseline gap-2">
        <span className="text-xs font-mono text-gray-700 font-semibold">
          {v.rule}
        </span>
        {v.path && (
          <span className="text-[11px] font-mono text-gray-500">
            at {v.path}
          </span>
        )}
        {v.matched && (
          <span className="text-[11px] font-mono text-gray-500">
            matched <span className="text-amber-700">{v.matched}</span>
          </span>
        )}
      </div>
      <div className="text-xs text-gray-600 mt-1">{v.message}</div>
    </li>
  );
}

function ValidationsPanel({
  validations,
  loading,
}: {
  validations: PolarisValidation[] | null;
  loading: boolean;
}) {
  if (validations === null && loading) {
    return (
      <section className="rounded-lg border border-gray-200 bg-white p-4">
        <div className="text-xs text-gray-400">loading violations…</div>
      </section>
    );
  }
  if (!validations || validations.length === 0) return null;

  // Don't render the panel at all when everything passed — the
  // hero-band timestamp + payload speak for themselves.
  const hasNonPass = validations.some((v) => v.status !== "pass");
  if (!hasNonPass) return null;

  return (
    <section>
      <h3 className="text-sm font-semibold tracking-wide text-gray-900 uppercase mb-3">
        Validation
      </h3>
      <div className="space-y-3">
        {validations.map((v) => (
          <div
            key={v.validator}
            className="rounded-lg border border-gray-200 bg-white p-4"
          >
            <div className="flex items-baseline gap-2 mb-2">
              <span className="font-mono text-sm font-semibold text-gray-900">
                {v.validator}
              </span>
              <StatusChip status={v.status} />
              <span className="text-xs text-gray-400">
                {v.violations
                  ? `${v.violations.length} ${
                      v.violations.length === 1 ? "violation" : "violations"
                    }`
                  : v.violation_count !== undefined
                  ? `${v.violation_count} ${
                      v.violation_count === 1 ? "violation" : "violations"
                    }`
                  : ""}
              </span>
            </div>
            {v.violations && v.violations.length > 0 && (
              <ul className="space-y-1.5">
                {v.violations.map((vi, i) => (
                  <ViolationItem key={i} v={vi} />
                ))}
              </ul>
            )}
            {!v.violations && v.violation_count !== undefined && v.violation_count > 0 && (
              <div className="text-xs text-gray-500 italic">
                violation details loading…
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

function PlanDetail({
  plan,
  validations,
  loading,
}: {
  plan: PolarisPlan;
  validations: PolarisValidation[] | null;
  loading: boolean;
}) {
  const p = plan.payload;
  const next = p.next_actions ?? [];
  const proms = p.parking_lot_promotions ?? [];
  const drift = p.drift ?? [];

  return (
    <div className="space-y-8">
      <ValidationsPanel validations={validations} loading={loading} />
      {p.parse_error && (
        <div className="border border-amber-200 bg-amber-50 rounded-md p-4 text-sm text-amber-900">
          <div className="font-medium mb-1">Plan parse failed</div>
          <div className="text-amber-800 text-xs font-mono">
            {p.parse_error}
          </div>
          <p className="mt-2 text-amber-800">
            Raw text from Claude is preserved below; structured fields are
            absent. The next tick after a docs change will retry.
          </p>
        </div>
      )}

      {/* Next actions */}
      {next.length > 0 && (
        <section>
          <div className="flex items-baseline justify-between mb-3">
            <h3 className="text-sm font-semibold tracking-wide text-gray-900 uppercase">
              Next actions
            </h3>
            <span className="text-[11px] text-gray-400">
              {next.length} {next.length === 1 ? "item" : "items"}
            </span>
          </div>
          <ol className="space-y-3">
            {next.map((a, i) => (
              <li
                key={i}
                className="flex gap-4 rounded-lg border border-gray-200 bg-white p-4 hover:border-indigo-200 transition-colors"
              >
                <span className="shrink-0 w-6 h-6 rounded-full bg-indigo-50 text-indigo-700 flex items-center justify-center text-xs font-semibold">
                  {i + 1}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-gray-900">
                    {a.task}
                  </div>
                  {a.why && (
                    <div className="text-sm text-gray-600 mt-1 leading-relaxed">
                      {a.why}
                    </div>
                  )}
                  {a.blocked_by && (
                    <div className="mt-2">
                      <span className="inline-block text-[10px] uppercase tracking-wider px-2 py-0.5 rounded bg-amber-50 text-amber-800 border border-amber-200">
                        blocked by: {a.blocked_by}
                      </span>
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </section>
      )}

      {/* Parking lot promotions */}
      {proms.length > 0 && (
        <section>
          <h3 className="text-sm font-semibold tracking-wide text-gray-900 uppercase mb-3">
            Parking-lot promotions
          </h3>
          <ul className="space-y-2">
            {proms.map((pr, i) => (
              <li
                key={i}
                className="rounded-md border border-gray-200 bg-white p-3"
              >
                <div className="text-sm font-medium text-gray-900">
                  {pr.item}
                </div>
                <div className="text-sm text-gray-600 mt-0.5">{pr.why}</div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Drift */}
      {drift.length > 0 && (
        <section>
          <h3 className="text-sm font-semibold tracking-wide text-amber-900 uppercase mb-3">
            Drift
          </h3>
          <ul className="space-y-2">
            {drift.map((d, i) => (
              <li
                key={i}
                className="rounded-md border border-amber-200 bg-amber-50 p-3"
              >
                <div className="text-xs uppercase tracking-wider text-amber-800 font-semibold">
                  {d.between}
                </div>
                <div className="text-sm text-amber-900 mt-1">
                  {d.observation}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Footer: source-doc hashes + tokens */}
      <section className="mt-8 pt-6 border-t border-gray-100">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-xs">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-gray-400 mb-1">
              MEMORY.md
            </div>
            <div className="font-mono text-gray-700">
              {p.doc_hashes?.memory_md ?? "—"}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-gray-400 mb-1">
              TASKS.md
            </div>
            <div className="font-mono text-gray-700">
              {p.doc_hashes?.tasks_md ?? "—"}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-gray-400 mb-1">
              Model
            </div>
            <div className="font-mono text-gray-700">{p.model ?? "—"}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-gray-400 mb-1">
              Tokens (in / out)
            </div>
            <div className="font-mono text-gray-700">
              {(p.tokens_in ?? 0).toLocaleString()} /{" "}
              {(p.tokens_out ?? 0).toLocaleString()}
            </div>
          </div>
        </div>
      </section>

      {/* Optional: raw text expander for debugging or when parse failed */}
      <details className="mt-2">
        <summary className="cursor-pointer text-xs text-gray-400 hover:text-gray-600">
          raw response from claude
        </summary>
        <pre className="mt-2 rounded-md bg-gray-50 border border-gray-200 p-3 text-[11px] text-gray-700 overflow-x-auto whitespace-pre-wrap font-mono">
          {p.text}
        </pre>
      </details>
    </div>
  );
}

export default function PolarisPage() {
  const router = useRouter();
  const [plans, setPlans] = useState<PolarisPlan[]>([]);
  const [selectedEventId, setSelectedEventId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Cache of full violation details keyed by event_id. The list endpoint
  // ships only summaries (validator + status + violation_count); when a
  // plan is selected and has any non-PASS validations, we lazy-load full
  // violations from /events/{id}/validations once and cache.
  const [fullValidations, setFullValidations] = useState<
    Map<number, PolarisValidation[]>
  >(new Map());
  const [validationsLoading, setValidationsLoading] = useState<Set<number>>(
    new Set(),
  );
  const inFlightRef = useRef<Set<number>>(new Set());

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const data = await fetchPolarisPlans(AGENT_NAME, 50);
        if (!alive) return;
        setPlans(data);
        setError(null);
      } catch (e) {
        if (!alive) return;
        if (handleAuthError(e, router)) return;
        setError(String(e));
      } finally {
        if (alive) setLoading(false);
      }
    };
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [router]);

  const selected = useMemo<PolarisPlan | null>(() => {
    if (plans.length === 0) return null;
    if (selectedEventId === null) return plans[0];
    return plans.find((p) => p.event_id === selectedEventId) ?? plans[0];
  }, [plans, selectedEventId]);

  // Lazy-load full violations for the selected plan when it has any
  // non-pass validation summaries. PASS-only plans never need a fetch
  // (the panel doesn't render at all in that case).
  useEffect(() => {
    if (!selected) return;
    const summaries = selected.validations ?? [];
    const needsFetch = summaries.some((v) => v.status !== "pass");
    if (!needsFetch) return;
    if (fullValidations.has(selected.event_id)) return;
    if (inFlightRef.current.has(selected.event_id)) return;

    inFlightRef.current.add(selected.event_id);
    setValidationsLoading((s) => new Set(s).add(selected.event_id));
    let alive = true;
    fetchEventValidations(selected.event_id)
      .then((full) => {
        if (!alive) return;
        setFullValidations((prev) => {
          const next = new Map(prev);
          next.set(selected.event_id, full);
          return next;
        });
      })
      .catch((e) => {
        handleAuthError(e, router);
        // Other errors: leave summaries showing, no detail panel; the
        // chip on the sidebar still tells the user something failed.
      })
      .finally(() => {
        inFlightRef.current.delete(selected.event_id);
        setValidationsLoading((s) => {
          const next = new Set(s);
          next.delete(selected.event_id);
          return next;
        });
      });
    return () => {
      alive = false;
    };
  }, [selected, fullValidations, router]);

  // Resolve which validation list to feed PlanDetail: if we've fetched
  // full details, prefer those; otherwise fall back to the summaries the
  // list endpoint gave us (rendered as count-only chips).
  const resolvedValidations = useMemo<PolarisValidation[] | null>(() => {
    if (!selected) return null;
    const full = fullValidations.get(selected.event_id);
    if (full) return full;
    return selected.validations ?? [];
  }, [selected, fullValidations]);

  const isValidationsLoading = selected
    ? validationsLoading.has(selected.event_id)
    : false;

  return (
    <main className="min-h-screen">
      <div className="px-8 pt-10 max-w-6xl mx-auto">
      </div>

      {/* Hero band — dark celestial */}
      <section className="relative overflow-hidden bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 text-white border-y border-indigo-900/40">
        <StarField />
        <div className="relative max-w-6xl mx-auto px-8 py-12">
          <div className="flex items-center gap-3 mb-2">
            <Star className="w-5 h-5 text-indigo-300" />
            <span className="text-[11px] uppercase tracking-[0.2em] text-indigo-300 font-medium">
              Polaris · project orchestrator
            </span>
          </div>
          {selected && selected.payload.summary ? (
            <h1 className="font-serif text-3xl sm:text-4xl leading-tight tracking-tight text-white mt-3 max-w-3xl">
              {selected.payload.summary}
            </h1>
          ) : selected ? (
            <h1 className="font-serif text-2xl text-indigo-100 mt-3">
              The latest plan didn&apos;t parse cleanly.
            </h1>
          ) : (
            <h1 className="font-serif text-3xl sm:text-4xl text-indigo-100 mt-3">
              Awaiting first sighting.
            </h1>
          )}
          {selected && (
            <div className="mt-5 flex items-center gap-4 text-xs text-indigo-300/80">
              <span>
                generated{" "}
                <span className="font-mono text-indigo-200">
                  {fmtRelative(selected.timestamp)}
                </span>
              </span>
              <span className="text-indigo-700">·</span>
              <span className="font-mono text-indigo-300/60">
                {fmtAbs(selected.timestamp)}
              </span>
            </div>
          )}
        </div>
      </section>

      <div className="max-w-6xl mx-auto px-8 py-10">
        {/* Phase 12D.2: Polaris narrates the cost audit alongside the
            plan stream. Renders nothing when there's no event yet or
            no actionable insights, so the page reads the same as before
            on a quiet workspace. */}
        <PolarisCostAnalysisPanel />

        {error && (
          <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
            {error}
          </div>
        )}

        {loading ? (
          <div className="text-gray-400 text-sm">loading…</div>
        ) : plans.length === 0 ? (
          <div className="max-w-2xl mx-auto py-4">
            <EmptyState />
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-[260px_1fr] gap-10">
            {/* History sidebar */}
            <aside className="md:sticky md:top-6 md:self-start">
              <div className="text-[11px] uppercase tracking-wider text-gray-500 mb-3">
                Past readings
              </div>
              <ul className="space-y-1">
                {plans.map((p, idx) => {
                  const isSelected = selected?.event_id === p.event_id;
                  const status = worstValidationStatus(p.validations);
                  return (
                    <li key={p.event_id}>
                      <button
                        type="button"
                        onClick={() => setSelectedEventId(p.event_id)}
                        className={
                          "w-full text-left px-3 py-2 rounded-md transition-colors text-sm border-l-2 " +
                          (isSelected
                            ? "bg-indigo-50 border-indigo-500 text-indigo-900"
                            : "border-transparent text-gray-700 hover:bg-gray-50 hover:border-gray-200")
                        }
                      >
                        <div className="flex items-center gap-2">
                          {idx === 0 && (
                            <Star className="w-3 h-3 text-indigo-400 shrink-0" />
                          )}
                          <span
                            className={
                              "font-medium flex-1 " +
                              (idx === 0 ? "" : "text-gray-700")
                            }
                          >
                            {fmtRelative(p.timestamp)}
                          </span>
                          {status !== "unchecked" && (
                            <StatusChip status={status} />
                          )}
                        </div>
                        <div className="text-[11px] text-gray-400 font-mono mt-0.5 ml-5">
                          {fmtAbs(p.timestamp)}
                        </div>
                      </button>
                    </li>
                  );
                })}
              </ul>
              <div className="mt-4 text-[11px] text-gray-400">
                refreshes every 30s
              </div>
            </aside>

            {/* Plan detail */}
            <div className="min-w-0">
              {selected && (
                <PlanDetail
                  plan={selected}
                  validations={resolvedValidations}
                  loading={isValidationsLoading}
                />
              )}
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
