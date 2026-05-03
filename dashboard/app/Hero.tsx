"use client";

/**
 * Phase 11B.2: home-page status hero.
 *
 * Full-width dark gradient banner that lives at the top of `/`,
 * matching `/polaris`'s treatment so the visual language is one
 * thing across the app. Reads `GET /workspaces/me/pulse` every 5s
 * and renders one of two states:
 *
 *   "Everything calm."         when issues_count === 0
 *   "N things want your        when there's any pending approval,
 *    attention."                failed validation, budget warning,
 *                              or stale agent.
 *
 * The headline is serif (Fraunces) and the subtitle is muted serif —
 * keeps the editorial-observatory feel from /polaris carrying through
 * to the home page.
 *
 * A small constellation icon top-right pulses gently when any new
 * event lands in the workspace (driven by the same poll, no separate
 * websocket needed for v1). Respects prefers-reduced-motion.
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  fetchWorkspacePulse,
  PulseIssues,
  UnauthorizedError,
  WorkspacePulse,
} from "./api";

function relativeTime(iso: string | null): string {
  if (!iso) return "never";
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return "never";
  const delta = Date.now() - ts;
  const sec = Math.floor(delta / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const d = Math.floor(hr / 24);
  return `${d}d ago`;
}

// Each issue type maps to (label, route) so the headline's expander
// can render a list with click-throughs to the right surface.
const ISSUE_LABELS: Record<keyof PulseIssues, { label: string; href: string }> = {
  pending_approvals: {
    label: "command approval",
    href: "/dispatch",
  },
  failed_validations: {
    label: "failed validation",
    href: "/runs",
  },
  budget_warnings: {
    label: "budget warning",
    href: "/account",
  },
  stale_agents: {
    label: "stale agent",
    href: "/",
  },
};

function pluralize(n: number, singular: string): string {
  return n === 1 ? `${n} ${singular}` : `${n} ${singular}s`;
}

export default function Hero() {
  const [pulse, setPulse] = useState<WorkspacePulse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      if (document.visibilityState === "hidden") {
        if (alive) timer = setTimeout(tick, 5000);
        return;
      }
      try {
        const fresh = await fetchWorkspacePulse();
        if (!alive) return;
        setPulse(fresh);
        setError(null);
      } catch (e) {
        if (!alive) return;
        if (e instanceof UnauthorizedError) {
          // Parent page handles the redirect; we just stay quiet.
          return;
        }
        setError(String(e));
      } finally {
        if (alive) timer = setTimeout(tick, 5000);
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

  const isEmpty = pulse !== null && pulse.agent_count === 0;
  const isCalm = pulse?.status === "calm";

  // Build the issue list for expanded state.
  const issueList: { count: number; label: string; href: string }[] = [];
  if (pulse) {
    (Object.keys(pulse.issues) as (keyof PulseIssues)[]).forEach((k) => {
      const n = pulse.issues[k];
      if (n > 0) {
        const meta = ISSUE_LABELS[k];
        issueList.push({
          count: n,
          label: pluralize(n, meta.label),
          href: meta.href,
        });
      }
    });
  }

  return (
    <section
      className="relative"
      aria-label="Workspace status"
    >
      {/* No background, no border, no shadow, no field stars —
          the parent section paints all of that, so this component
          just contributes its content layer. Keeps the hero ↔
          constellation transition seamless instead of two stacked
          cards with their own atmospheres. */}

      <div className="relative px-6 sm:px-10 pt-10 sm:pt-12 pb-6 sm:pb-7">
        {/* Headline + subtitle */}
        {pulse === null && !error && (
          <>
            <h1 className="font-serif italic text-3xl sm:text-4xl text-indigo-100/80">
              ...
            </h1>
            <p className="font-serif italic text-sm text-indigo-200/40 mt-3">
              tuning in
            </p>
          </>
        )}

        {error && (
          <>
            <h1 className="font-serif italic text-3xl sm:text-4xl text-rose-200">
              Lost the signal.
            </h1>
            <p className="font-mono text-xs text-rose-200/70 mt-3 truncate">
              {error}
            </p>
          </>
        )}

        {pulse !== null && !error && isEmpty && (
          <>
            <h1 className="font-serif italic text-3xl sm:text-4xl text-indigo-100">
              Sky empty.
            </h1>
            <p className="font-serif text-sm text-indigo-200/70 mt-3">
              Deploy your first agent →
            </p>
          </>
        )}

        {pulse !== null && !error && !isEmpty && (
          <>
            {isCalm ? (
              <h1 className="font-serif italic text-3xl sm:text-4xl text-indigo-100">
                Everything calm.
              </h1>
            ) : (
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                className="text-left font-serif italic text-3xl sm:text-4xl text-amber-100 hover:text-amber-50 transition-colors"
                aria-expanded={expanded}
              >
                {pulse.issues_count}{" "}
                {pulse.issues_count === 1 ? "thing wants" : "things want"}{" "}
                your attention.
              </button>
            )}

            <p className="font-serif text-sm text-indigo-200/70 mt-3">
              <span>{pulse.workspace_name}</span>
              <span className="mx-2 text-indigo-300/40">·</span>
              <span>
                {pluralize(pulse.agent_count, "agent")}
              </span>
              <span className="mx-2 text-indigo-300/40">·</span>
              <span>
                Polaris last tick {relativeTime(pulse.last_polaris_tick_at)}
              </span>
            </p>

            {!isCalm && expanded && issueList.length > 0 && (
              <ul className="mt-5 space-y-1.5">
                {issueList.map((it, i) => (
                  <li key={i}>
                    <Link
                      href={it.href}
                      className="inline-flex items-baseline gap-2 font-mono text-xs text-amber-100/85 hover:text-amber-50 transition-colors"
                    >
                      <span>{it.label}</span>
                      <span className="text-amber-200/60">→</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </section>
  );
}
