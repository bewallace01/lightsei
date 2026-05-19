"use client";

// Phase 18.3: first-run onboarding checklist.
//
// Dismissible 4-step widget that surfaces on the home page until the
// user has either completed all four setup steps OR explicitly
// dismissed it. Steps are derived from existing data (workspace
// secrets, agents) + a couple of localStorage flags for steps the
// backend doesn't track ("did they actually visit /zones?").
//
// Why localStorage for dismiss + visited-zones (not a workspace-row
// column): keeps the change lightweight + lets us iterate on what the
// checklist says without a backend migration each time. Trade-off:
// dismiss state is per-browser, not per-workspace. We'll promote to a
// DB column if multi-device dismissal becomes a customer complaint.

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  Agent,
  fetchAgents,
  fetchSecrets,
  UnauthorizedError,
} from "./api";

const DISMISS_KEY = "lightsei.onboarding.dismissed";
const VISITED_ZONES_KEY = "lightsei.onboarding.visited_zones";

type Step = {
  id: string;
  label: string;
  detail: string;
  href: string;
  done: boolean;
};

function detectSteps(secretNames: string[], agents: Agent[]): Step[] {
  // `lightsei.*` agents are workspace-internal bookkeeping
  // (lightsei.system carries cost/quality data). Filter them out of
  // the user-deployed agent count so the "deploy a team" step only
  // flips complete on a real bot.
  const userAgents = agents.filter((a) => !a.name.startsWith("lightsei."));

  const hasAnthropicKey = secretNames.includes("ANTHROPIC_API_KEY");
  const hasAgents = userAgents.length > 0;

  // The "see your trust-zone topology" step waits for the user to
  // actually visit /zones — set in the zones page's useEffect.
  const visitedZones =
    typeof window !== "undefined" &&
    localStorage.getItem(VISITED_ZONES_KEY) === "true";

  // "Configure a trust zone" flips complete when at least one user
  // agent has either a non-default sensitivity OR a non-empty
  // capability list OR an opted-in cross-zone flag. Compliance preset
  // deployers will satisfy this automatically (it sets caps + zones);
  // Standard preset deployers will need to do it manually.
  const hasConfiguredAgent = userAgents.some(
    (a) =>
      a.sensitivity_level !== "internal" ||
      (a.capabilities && a.capabilities.length > 0) ||
      a.dispatches_cross_zone,
  );

  return [
    {
      id: "anthropic_key",
      label: "Add an Anthropic API key",
      detail:
        "Lightsei uses this to generate bot code and run the team planner.",
      href: "/account",
      done: hasAnthropicKey,
    },
    {
      id: "deploy_team",
      label: "Drop a README and deploy a team",
      detail:
        "Paste your project's README; Lightsei proposes a team of bots tailored to it.",
      href: "/agents/team-from-readme",
      done: hasAgents,
    },
    {
      id: "visit_zones",
      label: "See your trust-zone topology",
      detail:
        "Visit /zones to see which bots can touch which data. Cross-zone dispatch is off by default.",
      href: "/zones",
      done: visitedZones,
    },
    {
      id: "configure_zone",
      label: "Configure a trust zone on at least one bot",
      detail:
        "Pick a sensitivity level + capability list (or use the Compliance preset, which does this for you).",
      href: "/agents",
      done: hasConfiguredAgent,
    },
  ];
}

export default function OnboardingChecklist() {
  const [steps, setSteps] = useState<Step[] | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;

    if (localStorage.getItem(DISMISS_KEY) === "true") {
      setDismissed(true);
      return;
    }

    let alive = true;
    (async () => {
      try {
        const [secretsList, agentsList] = await Promise.all([
          fetchSecrets().catch(() => []),
          fetchAgents().catch(() => []),
        ]);
        if (!alive) return;
        const secretNames = secretsList.map((s) => s.name);
        setSteps(detectSteps(secretNames, agentsList));
      } catch (e) {
        // UnauthorizedError is handled by the parent page's redirect;
        // anything else just hides the checklist silently. The
        // checklist is supplemental UI — never block the page on it.
        if (e instanceof UnauthorizedError) return;
      }
    })();

    return () => {
      alive = false;
    };
  }, []);

  if (dismissed) return null;
  if (!steps) return null;

  const doneCount = steps.filter((s) => s.done).length;
  const allDone = doneCount === steps.length;

  // Auto-hide when all complete. Keep DISMISS_KEY separate from
  // all-done so the checklist doesn't pop back if e.g. the user
  // deletes a secret later.
  if (allDone) return null;

  const onDismiss = () => {
    if (typeof window !== "undefined") {
      localStorage.setItem(DISMISS_KEY, "true");
    }
    setDismissed(true);
  };

  return (
    <section className="mb-10 rounded-lg border border-accent-200 bg-accent-50/40 p-5">
      <div className="flex items-start justify-between mb-4">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-gray-900">
            Get your team running ({doneCount}/{steps.length})
          </h2>
          <p className="text-sm text-gray-600 mt-1 max-w-2xl">
            Four steps from empty workspace to deployed bots with trust
            zones configured. Each link takes you to the right surface.
          </p>
        </div>
        <button
          type="button"
          onClick={onDismiss}
          className="text-xs text-gray-500 hover:text-gray-800 transition-colors shrink-0"
          aria-label="Dismiss onboarding checklist"
        >
          dismiss
        </button>
      </div>
      <ul className="space-y-2">
        {steps.map((step, i) => (
          <li key={step.id}>
            <Link
              href={step.href}
              className={
                "block rounded-md border px-3 py-3 hover:border-accent-300 transition-colors no-underline " +
                (step.done
                  ? "border-green-200 bg-green-50/70"
                  : "border-gray-200 bg-white")
              }
            >
              <div className="flex items-center gap-3">
                <div
                  className={
                    "w-6 h-6 rounded-full border-2 flex items-center justify-center text-xs font-semibold shrink-0 " +
                    (step.done
                      ? "border-green-600 bg-green-600 text-white"
                      : "border-gray-300 text-gray-400")
                  }
                  aria-hidden="true"
                >
                  {step.done ? "✓" : i + 1}
                </div>
                <div className="flex-1 min-w-0">
                  <div
                    className={
                      "text-sm font-medium " +
                      (step.done ? "text-gray-600" : "text-gray-900")
                    }
                  >
                    {step.label}
                  </div>
                  <div className="text-xs text-gray-600 mt-0.5">
                    {step.detail}
                  </div>
                </div>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
