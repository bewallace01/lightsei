"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import {
  OnboardingCatalog,
  OnboardingPlan,
  fetchOnboarding,
  handleAuthError,
  submitOnboarding,
} from "../api";

/**
 * Business-shaped onboarding: "answer a few questions -> your AI team".
 * Step 1 picks an industry (which pre-checks recommended goals); step 2
 * picks what the team should help with; submitting provisions the matching
 * assistants + turns on the right feeders, then shows what to connect next.
 */
export default function WelcomePage() {
  const router = useRouter();
  const [catalog, setCatalog] = useState<OnboardingCatalog | null>(null);
  const [industry, setIndustry] = useState<string | null>(null);
  const [goals, setGoals] = useState<Set<string>>(new Set());
  const [step, setStep] = useState<1 | 2>(1);
  const [submitting, setSubmitting] = useState(false);
  const [plan, setPlan] = useState<OnboardingPlan | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const { catalog } = await fetchOnboarding();
        if (alive) setCatalog(catalog);
      } catch (e) {
        if (!alive) return;
        if (handleAuthError(e, router)) return;
        setError(String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [router]);

  function pickIndustry(key: string) {
    setIndustry(key);
    // Pre-check the recommended goals for this industry.
    const rec = catalog?.recommendations[key] ?? [];
    setGoals(new Set(rec));
  }

  function toggleGoal(key: string) {
    setGoals((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  async function onSubmit() {
    setSubmitting(true);
    setError(null);
    try {
      const res = await submitOnboarding(industry, Array.from(goals));
      setPlan(res.plan);
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  }

  const goalLabel = useMemo(() => {
    const m: Record<string, string> = {};
    catalog?.goals.forEach((g) => (m[g.key] = g.label));
    return m;
  }, [catalog]);

  if (error) {
    return (
      <main className="px-4 py-10 max-w-2xl mx-auto">
        <div className="p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      </main>
    );
  }

  if (!catalog) {
    return (
      <main className="px-4 py-10 max-w-2xl mx-auto text-sm text-gray-400">
        loading…
      </main>
    );
  }

  // ---- Provisioned: show the team + next steps ----
  if (plan) {
    return (
      <main className="px-4 py-10 max-w-2xl mx-auto">
        <h1 className="text-2xl font-semibold tracking-tight">
          Your team is set up 🎉
        </h1>
        <p className="text-sm text-gray-500 mt-1">
          We provisioned {plan.assistants.length} assistant
          {plan.assistants.length === 1 ? "" : "s"} and turned on the feeders
          that keep them proactive.
        </p>

        <div className="mt-6 rounded-lg border border-gray-200 p-5">
          <div className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider mb-2">
            Your assistants
          </div>
          <div className="flex flex-wrap gap-2">
            {plan.assistants.map((a) => (
              <span
                key={a}
                className="text-xs px-2.5 py-1 rounded-full bg-accent-50 text-accent-700 capitalize"
              >
                {a}
              </span>
            ))}
          </div>
        </div>

        {plan.connectors_needed.length > 0 && (
          <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-5">
            <div className="text-sm font-medium text-amber-900 mb-1">
              Connect these to bring your team fully online
            </div>
            <ul className="text-sm text-amber-800 list-disc pl-5">
              {plan.connectors_needed.map((c) => (
                <li key={c.type}>{c.label}</li>
              ))}
            </ul>
            <Link
              href="/integrations"
              className="inline-block mt-3 text-sm px-3 py-1.5 rounded-md bg-amber-600 text-white hover:bg-amber-500 no-underline"
            >
              Go to Integrations →
            </Link>
          </div>
        )}

        <div className="mt-6 flex gap-3">
          <Link
            href="/"
            className="text-sm px-4 py-2 rounded-md bg-accent-600 text-white hover:bg-accent-700 no-underline"
          >
            Go to my dashboard
          </Link>
          <button
            onClick={() => {
              setPlan(null);
              setStep(1);
            }}
            className="text-sm px-4 py-2 rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50"
          >
            Adjust answers
          </button>
        </div>
      </main>
    );
  }

  // ---- Wizard ----
  return (
    <main className="px-4 py-10 max-w-2xl mx-auto">
      <h1 className="text-2xl font-semibold tracking-tight">
        Let&apos;s build your AI business team
      </h1>
      <p className="text-sm text-gray-500 mt-1">
        Two quick questions. No setup, no code.
      </p>

      {step === 1 && (
        <div className="mt-8">
          <div className="text-sm font-medium text-gray-900 mb-3">
            What kind of business do you run?
          </div>
          <div className="space-y-2">
            {catalog.industries.map((ind) => (
              <button
                key={ind.key}
                onClick={() => pickIndustry(ind.key)}
                className={
                  "w-full text-left text-sm px-4 py-3 rounded-lg border transition-colors " +
                  (industry === ind.key
                    ? "border-accent-600 bg-accent-50 text-accent-800"
                    : "border-gray-200 hover:border-gray-300")
                }
              >
                {ind.label}
              </button>
            ))}
          </div>
          <button
            disabled={!industry}
            onClick={() => setStep(2)}
            className="mt-6 text-sm px-4 py-2 rounded-md bg-accent-600 text-white hover:bg-accent-700 disabled:opacity-50"
          >
            Next →
          </button>
        </div>
      )}

      {step === 2 && (
        <div className="mt-8">
          <div className="text-sm font-medium text-gray-900 mb-1">
            What should your team help with?
          </div>
          <div className="text-xs text-gray-500 mb-3">
            We pre-checked what usually helps a business like yours. Adjust
            freely.
          </div>
          <div className="space-y-2">
            {catalog.goals.map((g) => {
              const checked = goals.has(g.key);
              return (
                <label
                  key={g.key}
                  className={
                    "flex items-center gap-3 text-sm px-4 py-3 rounded-lg border cursor-pointer transition-colors " +
                    (checked
                      ? "border-accent-600 bg-accent-50"
                      : "border-gray-200 hover:border-gray-300")
                  }
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggleGoal(g.key)}
                    className="h-4 w-4 accent-accent-600"
                  />
                  <span className="flex-1">{g.label}</span>
                  {g.connector && (
                    <span className="text-[10px] uppercase tracking-wider text-gray-400">
                      needs connect
                    </span>
                  )}
                </label>
              );
            })}
          </div>

          <div className="mt-6 flex items-center gap-3">
            <button
              onClick={() => setStep(1)}
              className="text-sm px-4 py-2 rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50"
            >
              ← Back
            </button>
            <button
              disabled={submitting || goals.size === 0}
              onClick={onSubmit}
              className="text-sm px-4 py-2 rounded-md bg-accent-600 text-white hover:bg-accent-700 disabled:opacity-50"
            >
              {submitting ? "Building your team…" : "Build my team"}
            </button>
            {goals.size === 0 && (
              <span className="text-xs text-gray-400">
                pick at least one
              </span>
            )}
          </div>

          {goals.size > 0 && (
            <p className="mt-3 text-xs text-gray-400">
              Building:{" "}
              {Array.from(goals)
                .map((k) => goalLabel[k])
                .filter(Boolean)
                .join(", ")}
            </p>
          )}
        </div>
      )}
    </main>
  );
}
