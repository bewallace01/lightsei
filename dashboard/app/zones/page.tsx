"use client";

// Phase 16.6: workspace trust-zone topology.
//
// One vertical lane per sensitivity level (public / internal /
// sensitive / pii), agents grouped into their lane. Quick at-a-
// glance answer to "where does the data go in this workspace?"
// for the non-technical user.
//
// Separate "Cross-zone dispatchers" section calls out the agents
// that have opted into cross-zone dispatch — these are the only
// agents whose send_command calls can cross between lanes. Default
// for new agents is OFF (Phase 16.4), so this list should be small
// and well-considered.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  Agent,
  SENSITIVITY_LEVELS,
  SensitivityLevel,
  UnauthorizedError,
  fetchAgents,
} from "../api";
import EmptyState from "../EmptyState";
import { SENSITIVITY_TONE, SensitivityChip } from "../sensitivity";


type AgentSummary = {
  name: string;
  sensitivity_level: SensitivityLevel;
  dispatches_cross_zone: boolean;
  capabilities: string[];
  description: string | null;
};


export default function ZonesPage(): JSX.Element {
  const router = useRouter();
  const [agents, setAgents] = useState<AgentSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const all = await fetchAgents();
        if (!alive) return;
        setAgents(
          all.map((a) => ({
            name: a.name,
            sensitivity_level: a.sensitivity_level,
            dispatches_cross_zone: a.dispatches_cross_zone,
            capabilities: a.capabilities,
            description: a.description,
          })),
        );
      } catch (e) {
        if (e instanceof UnauthorizedError) {
          router.replace("/login");
          return;
        }
        if (alive) setError(String(e instanceof Error ? e.message : e));
      }
    };
    load();
    return () => {
      alive = false;
    };
  }, [router]);

  return (
    <div className="max-w-6xl mx-auto px-6 py-8">
      <h1 className="text-2xl font-semibold tracking-tight mb-1">
        Trust zones
      </h1>
      <p className="text-sm text-gray-500 mb-6">
        Where does your workspace&apos;s data live, and what can cross between
        zones? Each lane below is a sensitivity level; agents in the lane
        live in that zone by default. Cross-zone dispatch is off by default
        (Phase 16.4) — agents at the bottom of the page are the explicit
        exceptions.
      </p>

      {error && (
        <div className="mb-6 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </div>
      )}

      {agents === null ? (
        <p className="text-sm text-gray-500">loading…</p>
      ) : agents.length === 0 ? (
        <EmptyState
          title="No agents yet"
          body={
            <>
              Your team&apos;s trust-zone topology will appear here once you
              deploy. Drop a README + pick the Compliance preset to land a
              team with PII isolated from public-side bots by default.
            </>
          }
          primary={{
            href: "/agents/team-from-readme",
            label: "✨ Drop a README to build your team",
          }}
        />
      ) : (
        <>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-10">
            {SENSITIVITY_LEVELS.map((lvl) => (
              <ZoneLane
                key={lvl}
                level={lvl}
                agents={agents.filter((a) => a.sensitivity_level === lvl)}
              />
            ))}
          </div>

          <CrossZoneSection agents={agents.filter((a) => a.dispatches_cross_zone)} />
        </>
      )}
    </div>
  );
}


function ZoneLane({
  level,
  agents,
}: {
  level: SensitivityLevel;
  agents: AgentSummary[];
}): JSX.Element {
  const tone = SENSITIVITY_TONE[level];
  return (
    <div
      className={`rounded-lg border ${tone.lane} p-4 min-h-[180px] flex flex-col`}
    >
      <div className="flex items-center justify-between mb-3">
        <SensitivityChip level={level} size="md" />
        <span className="text-xs text-gray-500">
          {agents.length} agent{agents.length === 1 ? "" : "s"}
        </span>
      </div>
      {agents.length === 0 ? (
        <p className="text-xs text-gray-400 italic">
          no agents in this zone
        </p>
      ) : (
        <ul className="space-y-2 flex-1">
          {agents.map((a) => (
            <li key={a.name} className="bg-white/70 rounded border border-gray-200 px-3 py-2">
              <Link
                href={`/agents/${encodeURIComponent(a.name)}`}
                className="font-mono text-sm text-accent-700 hover:text-accent-900"
              >
                {a.name}
              </Link>
              {a.description && (
                <p className="text-xs text-gray-600 mt-1 line-clamp-2">
                  {a.description}
                </p>
              )}
              {a.capabilities.length > 0 && (
                <div className="flex flex-wrap gap-1 mt-1">
                  {a.capabilities.slice(0, 4).map((c) => (
                    <code
                      key={c}
                      className="font-mono text-[10px] bg-gray-100 text-gray-700 rounded px-1 py-0.5"
                    >
                      {c}
                    </code>
                  ))}
                  {a.capabilities.length > 4 && (
                    <span className="text-[10px] text-gray-500">
                      +{a.capabilities.length - 4}
                    </span>
                  )}
                </div>
              )}
              {a.dispatches_cross_zone && (
                <span
                  className="inline-block mt-2 text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-1.5 py-0.5"
                  title="this agent's send_command can target other zones"
                >
                  ↔ cross-zone enabled
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}


function CrossZoneSection({
  agents,
}: {
  agents: AgentSummary[];
}): JSX.Element {
  return (
    <section className="mb-10">
      <h2 className="text-[11px] font-semibold text-gray-500 mb-3 uppercase tracking-wider">
        Cross-zone dispatchers
      </h2>
      <p className="text-xs text-gray-500 mb-3">
        Agents below have <code className="font-mono">dispatches_cross_zone=True</code>{" "}
        — their <code className="font-mono">send_command</code> calls can
        target agents in a different sensitivity zone. Phase 11.2 auto-
        approval rules still apply on top: cross-zone-enabled does NOT
        mean auto-approved.
      </p>
      {agents.length === 0 ? (
        <div className="rounded-md border border-gray-200 bg-gray-50 p-4 text-sm text-gray-600">
          No cross-zone dispatchers in this workspace. Every agent is
          locked to its own zone — the default-deny posture from Phase
          16.4.
        </div>
      ) : (
        <ul className="space-y-2">
          {agents.map((a) => (
            <li
              key={a.name}
              className="flex items-center gap-3 rounded border border-amber-200 bg-amber-50/50 px-3 py-2"
            >
              <SensitivityChip level={a.sensitivity_level} />
              <Link
                href={`/agents/${encodeURIComponent(a.name)}`}
                className="font-mono text-sm text-accent-700 hover:text-accent-900"
              >
                {a.name}
              </Link>
              {a.description && (
                <span className="text-xs text-gray-600 truncate">
                  — {a.description}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
