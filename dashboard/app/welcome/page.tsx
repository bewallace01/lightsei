"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import {
  DeployTeamResult,
  OnboardingCatalog,
  OnboardingPlan,
  OnboardingProfile,
  TeamStatusResult,
  deployTeam,
  fetchOnboarding,
  fetchTeamStatus,
  handleAuthError,
  renameAssistant,
  submitOnboarding,
} from "../api";

/**
 * Live deploy status for the provisioned assistants. Polls /team/status so
 * the owner watches their team come online (queued -> running). Shows a
 * prompt for an Anthropic key when an AI assistant needs one.
 */
function TeamStatusPanel() {
  const [status, setStatus] = useState<TeamStatusResult | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);

  async function refresh() {
    try {
      setStatus(await fetchTeamStatus());
    } catch {
      /* enrichment; ignore */
    }
  }

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const s = await fetchTeamStatus();
        // Don't clobber the field the owner is actively editing.
        if (alive && !editing) setStatus(s);
      } catch {
        /* enrichment; ignore */
      }
    };
    tick();
    const id = setInterval(tick, 4000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [editing]);

  async function saveName(agent: string) {
    setSaving(true);
    try {
      await renameAssistant(agent, draft);
      setEditing(null);
      setDraft("");
      await refresh();
    } finally {
      setSaving(false);
    }
  }

  if (!status || status.assistants.length === 0) return null;

  const dot = (a: { running: boolean; deployed: boolean }) =>
    a.running ? "bg-emerald-500" : a.deployed ? "bg-amber-400" : "bg-gray-300";
  const label = (a: { running: boolean; deployed: boolean; status: string | null }) =>
    a.running ? "running" : a.deployed ? "starting…" : a.status ?? "not deployed";

  return (
    <div className="mt-4 rounded-lg border border-gray-200 p-5">
      <div className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider mb-3">
        Your team
      </div>
      <ul className="space-y-2">
        {status.assistants.map((a) => (
          <li key={a.name} className="flex items-center gap-2 text-sm group">
            <span className={"h-2 w-2 rounded-full " + dot(a)} />
            {editing === a.name ? (
              <>
                <input
                  value={draft}
                  autoFocus
                  maxLength={80}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") saveName(a.name);
                    if (e.key === "Escape") setEditing(null);
                  }}
                  placeholder={a.display_name}
                  className="text-sm rounded-md ring-1 ring-gray-300 px-2 py-0.5 w-32 focus:outline-none focus:ring-2 focus:ring-indigo-600"
                />
                <button
                  onClick={() => saveName(a.name)}
                  disabled={saving}
                  className="text-xs text-accent-600 hover:text-accent-700 disabled:opacity-50"
                >
                  save
                </button>
                <button
                  onClick={() => setEditing(null)}
                  className="text-xs text-gray-400 hover:text-gray-600"
                >
                  cancel
                </button>
              </>
            ) : (
              <>
                <span className="text-gray-900">{a.display_name}</span>
                {a.role && (
                  <span className="text-xs text-gray-400">· {a.role}</span>
                )}
                <span className="text-xs text-gray-400">{label(a)}</span>
                <button
                  onClick={() => {
                    setEditing(a.name);
                    setDraft(a.is_custom_name ? a.display_name : "");
                  }}
                  className="text-xs text-gray-300 hover:text-accent-600 opacity-0 group-hover:opacity-100 transition-opacity"
                  title={`Rename ${a.display_name}`}
                >
                  rename
                </button>
              </>
            )}
          </li>
        ))}
      </ul>
      {status.needs_anthropic_key && (
        <div className="mt-3 text-xs text-amber-700">
          Some assistants use AI.{" "}
          <Link href="/account" className="underline font-medium">
            Add an Anthropic key
          </Link>{" "}
          to start them working.
        </div>
      )}
    </div>
  );
}

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
  const [websiteUrl, setWebsiteUrl] = useState("");
  const [step, setStep] = useState<1 | 2>(1);
  const [submitting, setSubmitting] = useState(false);
  const [plan, setPlan] = useState<OnboardingPlan | null>(null);
  const [deploy, setDeploy] = useState<DeployTeamResult | null>(null);
  const [profile, setProfile] = useState<OnboardingProfile | null>(null);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const { catalog, profile } = await fetchOnboarding();
        if (!alive) return;
        setCatalog(catalog);
        setProfile(profile);
        // Pre-fill from a prior run so "Adjust my team" starts from the
        // current answers instead of a blank wizard.
        if (profile) {
          setIndustry(profile.industry);
          setGoals(new Set(profile.goals));
        }
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
      // Send the site URL when any URL-needing goal (website / seo) is
      // selected; the backend applies it to each one's feeder and ignores it
      // otherwise (won't clobber an existing target when left blank).
      const needsUrl = goals.has("website") || goals.has("seo");
      const sendUrl = needsUrl ? websiteUrl.trim() || null : null;
      const res = await submitOnboarding(industry, Array.from(goals), sendUrl);
      setPlan(res.plan);
      // Bring the provisioned assistants online (best-effort: if it
      // fails, the team is still provisioned and can be deployed later).
      try {
        setDeploy(await deployTeam());
      } catch {
        setDeploy(null);
      }
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
          {deploy &&
            (deploy.deployed.length > 0 || deploy.already_running.length > 0) && (
              <> They&apos;re coming online now.</>
            )}
        </p>

        {deploy?.needs_anthropic_key && (
          <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
            Some assistants use AI and need an Anthropic API key to start
            working.{" "}
            <Link href="/account" className="underline font-medium">
              Add your key
            </Link>
            .
          </div>
        )}

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

        <TeamStatusPanel />

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
              setDeploy(null);
              setEditing(true);
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

  // ---- Already onboarded: summary + live status (not a fresh wizard) ----
  if (profile && !editing) {
    const goalLabels = profile.goals
      .map((k) => goalLabel[k])
      .filter(Boolean)
      // Lowercase the first letter so the labels read naturally mid-
      // sentence ("helping with answer customer emails, capture leads…").
      .map((l) => l.charAt(0).toLowerCase() + l.slice(1));
    return (
      <main className="px-4 py-10 max-w-2xl mx-auto">
        <h1 className="text-2xl font-semibold tracking-tight">Your AI team</h1>
        <p className="text-sm text-gray-500 mt-1">
          You&apos;re set up
          {profile.industry ? ` for a ${profile.industry.replace(/_/g, " ")} business` : ""}
          {goalLabels.length > 0 && <>, helping with {goalLabels.join(", ")}</>}.
        </p>

        <TeamStatusPanel />

        <div className="mt-6 flex gap-3">
          <Link
            href="/"
            className="text-sm px-4 py-2 rounded-md bg-accent-600 text-white hover:bg-accent-700 no-underline"
          >
            Go to my dashboard
          </Link>
          <button
            onClick={() => {
              setEditing(true);
              setStep(1);
            }}
            className="text-sm px-4 py-2 rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50"
          >
            Adjust my team
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
            {(() => {
              // The site-URL field is shared across url-needing goals (website /
              // seo). Render it once, under the first checked one, so picking
              // both doesn't show two identical inputs.
              const firstUrlGoal = catalog.goals.find(
                (g) => g.needs_url && goals.has(g.key),
              )?.key;
              return catalog.goals.map((g) => {
                const checked = goals.has(g.key);
                return (
                  <div key={g.key}>
                    <label
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
                    {g.key === firstUrlGoal && (
                      <div className="mt-2 ml-4 pl-3 border-l-2 border-accent-100">
                        <label className="block text-xs text-gray-500 mb-1">
                          What&apos;s your website address?
                        </label>
                        <input
                          type="url"
                          inputMode="url"
                          value={websiteUrl}
                          onChange={(e) => setWebsiteUrl(e.target.value)}
                          placeholder="yourbusiness.com"
                          className="w-full text-sm rounded-md ring-1 ring-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-accent-600"
                        />
                        <p className="mt-1 text-[11px] text-gray-400">
                          Used for website monitoring and SEO checks.
                        </p>
                      </div>
                    )}
                  </div>
                );
              });
            })()}
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
