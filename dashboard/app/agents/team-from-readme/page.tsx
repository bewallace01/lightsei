"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Agent,
  AgentGenerateOutput,
  TeamMember,
  TeamMemberRole,
  TeamPlan,
  UnauthorizedError,
  fetchAgents,
  fetchTeamPlan,
  generateAgent,
} from "../../api";
import { sparklePath, tintForAgent } from "../../stars";
import { STAR_DICTIONARY, isStarName } from "./star_dictionary";


// ---------- Generation state (12C.3) ---------- //

type GenStatus = "pending" | "generating" | "success" | "failed" | "skipped";

type GenResult = {
  status: GenStatus;
  // Description sent to /agents/generate. Kept on the row so retry/edit
  // can mutate it without losing the original team draft_description.
  description: string;
  output?: AgentGenerateOutput;
  error?: string;
};

type Phase = "plan" | "generating" | "review";

/** Build the per-bot description fed to /agents/generate: the team
 *  member's draft_description + an explicit "coordinate with" footer
 *  listing the other team members (so the generator wires send_command
 *  calls to its teammates rather than re-implementing). */
function descriptionFor(member: TeamMember, team: TeamMember[]): string {
  const others = team
    .filter((m) => m.name !== member.name)
    .map((m) => `${m.name} (${m.summary})`);
  if (others.length === 0) return member.draft_description;
  return (
    member.draft_description.trim() +
    "\n\nCoordinate with these other agents in this team:\n" +
    others.map((o) => `- ${o}`).join("\n")
  );
}


// ---------- Page ---------- //

export default function TeamFromReadmePage() {
  const router = useRouter();
  const [readmeText, setReadmeText] = useState("");
  const [freeform, setFreeform] = useState("");
  const [githubRepo, setGithubRepo] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [plan, setPlan] = useState<TeamPlan | null>(null);
  const [team, setTeam] = useState<TeamMember[]>([]);
  // Existing agents in this workspace — surfaced in the preview as
  // "wires-into" stubs so the user can see the proposed bot's dispatch
  // targets in the same canvas.
  const [existingAgents, setExistingAgents] = useState<string[]>([]);
  const [selectedName, setSelectedName] = useState<string | null>(null);

  // Phase 12C.3 bulk-generation state.
  const [phase, setPhase] = useState<Phase>("plan");
  const [genResults, setGenResults] = useState<Record<string, GenResult>>({});

  useEffect(() => {
    let alive = true;
    fetchAgents()
      .then((rows) => {
        if (!alive) return;
        setExistingAgents(
          rows
            .map((a) => a.name)
            .filter((n) => !n.startsWith("lightsei.")),
        );
      })
      .catch((e) => {
        if (e instanceof UnauthorizedError) router.replace("/login");
      });
    return () => {
      alive = false;
    };
  }, [router]);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!readmeText && !freeform && !githubRepo) {
      setError(
        "Provide at least one of: README text, a freeform description, or a GitHub repo URL.",
      );
      return;
    }
    setSubmitting(true);
    setError(null);
    setPlan(null);
    setTeam([]);
    setSelectedName(null);
    try {
      const got = await fetchTeamPlan({
        readme_text: readmeText || undefined,
        freeform_description: freeform || undefined,
        github_repo: githubRepo || undefined,
      });
      setPlan(got);
      setTeam(got.team);
      setSelectedName(got.team[0]?.name ?? null);
    } catch (e) {
      if (e instanceof UnauthorizedError) {
        router.replace("/login");
        return;
      }
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setSubmitting(false);
    }
  };

  const reservedNames = useMemo(() => {
    const reserved = new Set<string>(existingAgents.map((n) => n.toLowerCase()));
    for (const m of team) reserved.add(m.name.toLowerCase());
    return reserved;
  }, [existingAgents, team]);

  const onUpdateMember = (origName: string, patch: Partial<TeamMember>) => {
    setTeam((cur) =>
      cur.map((m) => (m.name === origName ? { ...m, ...patch } : m)),
    );
    if (patch.name && selectedName === origName) {
      setSelectedName(patch.name);
    }
  };

  const onRemoveMember = (name: string) => {
    if (!confirm(`Remove "${name}" from the team?`)) return;
    setTeam((cur) => {
      const next = cur.filter((m) => m.name !== name);
      // Also drop dispatch edges pointing at the removed member.
      return next.map((m) => ({
        ...m,
        dispatches_to: m.dispatches_to.filter((t) => t !== name),
      }));
    });
    if (selectedName === name) setSelectedName(null);
  };

  const onAddMember = () => {
    // Pick the first dictionary name that's free in this team +
    // workspace as a sensible default; the user can change it inline.
    const free = STAR_DICTIONARY.find(
      (s) => !reservedNames.has(s.name),
    );
    if (!free) {
      setError("No free star-dictionary names left to add a new bot.");
      return;
    }
    const nm: TeamMember = {
      name: free.name,
      role: "specialist",
      summary: "(describe what this bot does)",
      command_kinds: [],
      dispatches_to: [],
      needs_workspace_secrets: [],
      draft_description: `A new specialist named ${free.name}. Edit this description before generating.`,
    };
    setTeam((cur) => [...cur, nm]);
    setSelectedName(free.name);
  };

  const selected = useMemo<TeamMember | null>(() => {
    if (selectedName === null) return null;
    return team.find((m) => m.name === selectedName) ?? null;
  }, [selectedName, team]);

  // ---------- Bulk generation ---------- //

  // Single-bot call. Updates genResults as the request resolves; safe
  // to call standalone (for retry) or in parallel from onGenerate.
  const runOneGenerate = async (member: TeamMember, description: string) => {
    setGenResults((cur) => ({
      ...cur,
      [member.name]: {
        status: "generating",
        description,
        output: cur[member.name]?.output,
      },
    }));
    try {
      const output = await generateAgent({
        description,
        // Suggest existing-agent dispatch targets so the generator
        // wires send_command to them rather than to non-existent peers.
        target_agents: Array.from(
          new Set([
            ...team.filter((m) => m.name !== member.name).map((m) => m.name),
            ...member.dispatches_to,
          ]),
        ),
        name_hint: member.name,
      });
      setGenResults((cur) => ({
        ...cur,
        [member.name]: { status: "success", description, output },
      }));
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setGenResults((cur) => ({
        ...cur,
        [member.name]: { status: "failed", description, error: msg },
      }));
    }
  };

  const onGenerate = async () => {
    if (team.length === 0) return;
    setPhase("generating");
    // Seed every member to `generating` so the progress UI lights up
    // immediately — runOneGenerate will overwrite each as they resolve.
    const initial: Record<string, GenResult> = {};
    for (const m of team) {
      initial[m.name] = {
        status: "generating",
        description: descriptionFor(m, team),
      };
    }
    setGenResults(initial);

    // Parallel — Anthropic supports concurrent requests; backend
    // serializes per-workspace-budget on its side. Promise.allSettled
    // so a single failure doesn't short-circuit the rest.
    await Promise.allSettled(
      team.map((m) => runOneGenerate(m, descriptionFor(m, team))),
    );
    setPhase("review");
  };

  const onRetry = async (memberName: string, descriptionOverride?: string) => {
    const member = team.find((m) => m.name === memberName);
    if (!member) return;
    const desc =
      descriptionOverride ??
      genResults[memberName]?.description ??
      descriptionFor(member, team);
    await runOneGenerate(member, desc);
  };

  const onSkip = (memberName: string) => {
    setGenResults((cur) => ({
      ...cur,
      [memberName]: {
        ...cur[memberName],
        status: "skipped",
      },
    }));
  };

  const onBackToPlan = () => {
    setPhase("plan");
    // Keep genResults around so re-generating doesn't lose successful
    // bots' code — if the user only tweaked one description, the rest
    // can stay as-is. (Re-running onGenerate would overwrite them, but
    // we let the user decide when to do that.)
  };

  const planEditable = phase === "plan";

  return (
    <main className="px-8 py-10 max-w-6xl mx-auto">
      <div className="mb-2">
        <Link
          href="/agents"
          className="text-sm text-gray-500 hover:text-gray-900"
        >
          ← agents
        </Link>
      </div>
      <h1 className="text-2xl font-semibold tracking-tight">
        ✨ propose a team from a README
      </h1>
      <p className="text-sm text-gray-500 mt-1 mb-8 max-w-2xl">
        Drop a project README (or paste freeform context, or point at a
        GitHub repo) and Lightsei proposes a team of 3-7 bots wired
        into a constellation. Review and edit the plan; nothing is
        deployed until you confirm.
      </p>

      <InputCard
        readmeText={readmeText}
        setReadmeText={setReadmeText}
        freeform={freeform}
        setFreeform={setFreeform}
        githubRepo={githubRepo}
        setGithubRepo={setGithubRepo}
        onSubmit={onSubmit}
        submitting={submitting}
      />

      {error && (
        <div className="mt-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {plan && team.length > 0 && (
        <section className="mt-10">
          <div className="flex items-baseline justify-between mb-3">
            <div>
              <h2 className="text-xl font-semibold tracking-tight">
                Proposed team
              </h2>
              <p className="text-sm text-gray-500 mt-1 max-w-2xl">
                {plan.rationale}
              </p>
            </div>
            {planEditable && (
              <button
                type="button"
                onClick={onAddMember}
                className="text-xs text-accent-600 hover:text-accent-700 font-medium whitespace-nowrap"
              >
                + add bot
              </button>
            )}
          </div>

          <div className="grid grid-cols-1 md:grid-cols-[1fr_320px] gap-6">
            <TeamConstellation
              team={team}
              existingAgents={existingAgents}
              selectedName={selectedName}
              onSelect={setSelectedName}
            />
            {planEditable ? (
              <MemberPanel
                member={selected}
                reservedNames={reservedNames}
                teammates={team.filter((m) => m.name !== selected?.name).map((m) => m.name)}
                existingAgents={existingAgents}
                onUpdate={onUpdateMember}
                onRemove={onRemoveMember}
              />
            ) : (
              <aside className="rounded-lg border border-gray-200 bg-gray-50 p-5 text-sm text-gray-500">
                Plan is locked while generation runs. Hit{" "}
                <button
                  type="button"
                  onClick={onBackToPlan}
                  className="text-accent-600 hover:text-accent-700 underline"
                >
                  back to plan
                </button>{" "}
                to edit further.
              </aside>
            )}
          </div>

          {phase === "plan" && (
            <div className="mt-8 border-t border-gray-100 pt-6 flex items-center justify-between gap-4">
              <p className="text-xs text-gray-500">
                Click any star above to inspect or edit. Add or remove bots
                freely — nothing is deployed yet.
              </p>
              <button
                type="button"
                onClick={onGenerate}
                disabled={team.length === 0}
                className="px-5 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 disabled:opacity-50"
              >
                ✨ Generate code →
              </button>
            </div>
          )}

          {phase !== "plan" && (
            <GenerationSection
              team={team}
              genResults={genResults}
              phase={phase}
              onRetry={onRetry}
              onSkip={onSkip}
              onBackToPlan={onBackToPlan}
            />
          )}
        </section>
      )}
    </main>
  );
}


// ---------- Input card ---------- //

function InputCard({
  readmeText,
  setReadmeText,
  freeform,
  setFreeform,
  githubRepo,
  setGithubRepo,
  onSubmit,
  submitting,
}: {
  readmeText: string;
  setReadmeText: (s: string) => void;
  freeform: string;
  setFreeform: (s: string) => void;
  githubRepo: string;
  setGithubRepo: (s: string) => void;
  onSubmit: (e: React.FormEvent) => void;
  submitting: boolean;
}) {
  const [dragActive, setDragActive] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const onFile = async (file: File | undefined) => {
    if (!file) return;
    if (file.size > 1_000_000) {
      alert("File is over 1MB; paste a smaller README or use the URL field.");
      return;
    }
    const text = await file.text();
    setReadmeText(text);
  };

  return (
    <form
      onSubmit={onSubmit}
      className="rounded-lg border border-gray-200 bg-white p-6"
    >
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* README drop zone + textarea */}
        <div>
          <label className="block text-[11px] uppercase tracking-wider text-gray-500 mb-2">
            README
          </label>
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragActive(true);
            }}
            onDragLeave={() => setDragActive(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragActive(false);
              onFile(e.dataTransfer.files[0]);
            }}
            onClick={() => fileInputRef.current?.click()}
            className={
              "rounded-md border-2 border-dashed px-4 py-3 text-center text-xs text-gray-500 cursor-pointer transition-colors " +
              (dragActive
                ? "border-accent-500 bg-accent-50"
                : "border-gray-300 hover:border-gray-400")
            }
          >
            drop a <code className="font-mono">.md</code> file here, or click to pick
          </div>
          <input
            ref={fileInputRef}
            type="file"
            accept=".md,.markdown,.txt,text/markdown,text/plain"
            className="hidden"
            onChange={(e) => onFile(e.target.files?.[0])}
          />
          <textarea
            value={readmeText}
            onChange={(e) => setReadmeText(e.target.value)}
            placeholder="…or paste README contents here"
            className="mt-2 w-full h-44 px-3 py-2 border border-gray-300 rounded-md text-sm font-mono focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none"
          />
        </div>

        {/* Freeform + GitHub URL */}
        <div className="space-y-4">
          <div>
            <label className="block text-[11px] uppercase tracking-wider text-gray-500 mb-2">
              Freeform description
            </label>
            <textarea
              value={freeform}
              onChange={(e) => setFreeform(e.target.value)}
              placeholder="What does this project do? What recurring work would you like automated?"
              className="w-full h-32 px-3 py-2 border border-gray-300 rounded-md text-sm focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none"
            />
          </div>
          <div>
            <label className="block text-[11px] uppercase tracking-wider text-gray-500 mb-2">
              GitHub repo (optional)
            </label>
            <input
              type="text"
              value={githubRepo}
              onChange={(e) => setGithubRepo(e.target.value)}
              placeholder="owner/name or https://github.com/owner/name"
              className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm font-mono focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none"
            />
            <p className="text-[11px] text-gray-400 mt-1">
              Public repos work without auth; private repos use the workspace&apos;s GitHub PAT if set.
            </p>
          </div>
        </div>
      </div>

      <div className="flex items-center justify-end mt-4">
        <button
          type="submit"
          disabled={submitting}
          className="px-5 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 disabled:opacity-50"
        >
          {submitting ? "analyzing…" : "✨ propose a team"}
        </button>
      </div>
    </form>
  );
}


// ---------- Constellation preview ---------- //

const VB_W = 720;
const VB_H = 360;

function positionFor(
  i: number,
  total: number,
  role: TeamMemberRole,
): { x: number; y: number } {
  // Orchestrator goes dead center; everyone else evenly distributed
  // around a ring biased by role (specialists closer in, messengers
  // further out). Deterministic — no force-directed layout.
  if (role === "orchestrator") {
    return { x: VB_W / 2, y: VB_H / 2 };
  }
  const radius = role === "messenger" ? 130 : 100;
  const total_non_orch = total; // approximate; clipping is fine for previews
  const angle = (i / Math.max(1, total_non_orch)) * Math.PI * 2 - Math.PI / 2;
  return {
    x: VB_W / 2 + Math.cos(angle) * radius,
    y: VB_H / 2 + Math.sin(angle) * radius * 0.7, // slight squash so the canvas reads wider than tall
  };
}

function TeamConstellation({
  team,
  existingAgents,
  selectedName,
  onSelect,
}: {
  team: TeamMember[];
  existingAgents: string[];
  selectedName: string | null;
  onSelect: (name: string) => void;
}) {
  // Compute positions for proposed team. Existing agents referenced by
  // dispatches_to but not in the team get drawn as "ghost" stubs along
  // the right edge so the dispatch arrow has a target.
  const positions = useMemo(() => {
    const m = new Map<string, { x: number; y: number }>();
    // Lay out non-orchestrators first; orchestrator always at center.
    const nonOrch = team.filter((t) => t.role !== "orchestrator");
    const orch = team.find((t) => t.role === "orchestrator");
    nonOrch.forEach((t, i) => {
      m.set(t.name, positionFor(i, nonOrch.length, t.role));
    });
    if (orch) m.set(orch.name, positionFor(0, 1, "orchestrator"));
    // Ghost stubs for referenced existing agents.
    const referenced = new Set<string>();
    for (const t of team) {
      for (const d of t.dispatches_to) {
        if (
          existingAgents.includes(d) &&
          !team.some((tt) => tt.name === d)
        ) {
          referenced.add(d);
        }
      }
    }
    Array.from(referenced).forEach((nm, i) => {
      const total = referenced.size;
      const y = VB_H * (0.2 + (0.6 * i) / Math.max(1, total - 1 || 1));
      m.set(nm, { x: VB_W - 50, y });
    });
    return m;
  }, [team, existingAgents]);

  const ghostNames = useMemo(() => {
    const inTeam = new Set(team.map((t) => t.name));
    const refs = new Set<string>();
    for (const t of team) {
      for (const d of t.dispatches_to) {
        if (existingAgents.includes(d) && !inTeam.has(d)) refs.add(d);
      }
    }
    return Array.from(refs);
  }, [team, existingAgents]);

  return (
    <div className="rounded-lg border border-indigo-900/30 bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 overflow-hidden">
      <svg
        viewBox={`0 0 ${VB_W} ${VB_H}`}
        className="w-full h-auto"
        role="img"
        aria-label="Proposed team constellation preview"
      >
        {/* Edges first (under stars). */}
        {team.map((src) =>
          src.dispatches_to.map((dst) => {
            const a = positions.get(src.name);
            const b = positions.get(dst);
            if (!a || !b) return null;
            return (
              <line
                key={`${src.name}->${dst}`}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke="rgb(199 210 254 / 0.3)"
                strokeWidth={1}
                strokeDasharray="3 3"
              />
            );
          }),
        )}

        {/* Team stars. */}
        {team.map((m) => {
          const p = positions.get(m.name);
          if (!p) return null;
          const isOrch = m.role === "orchestrator";
          const tint = isOrch ? "#fde68a" : tintForAgent(m.name);
          const size = isOrch ? 18 : 12;
          const isSelected = selectedName === m.name;
          return (
            <g
              key={m.name}
              onClick={() => onSelect(m.name)}
              className="cursor-pointer"
            >
              {isSelected && (
                <circle
                  cx={p.x}
                  cy={p.y}
                  r={size + 8}
                  fill="none"
                  stroke="rgb(165 180 252 / 0.7)"
                  strokeWidth={1.5}
                />
              )}
              <path
                d={sparklePath(p.x, p.y, size)}
                fill={tint}
                opacity={isSelected ? 1 : 0.85}
              />
              <text
                x={p.x}
                y={p.y + size + 14}
                textAnchor="middle"
                fontSize={11}
                fill="rgb(199 210 254 / 0.9)"
                className="font-mono"
              >
                {m.name}
              </text>
              <text
                x={p.x}
                y={p.y + size + 26}
                textAnchor="middle"
                fontSize={9}
                fill="rgb(165 180 252 / 0.6)"
              >
                {m.role}
              </text>
            </g>
          );
        })}

        {/* Ghost stubs for existing agents we wire into. */}
        {ghostNames.map((nm) => {
          const p = positions.get(nm);
          if (!p) return null;
          return (
            <g key={`ghost-${nm}`}>
              <circle cx={p.x} cy={p.y} r={6} fill="rgb(99 102 241 / 0.4)" />
              <text
                x={p.x}
                y={p.y + 18}
                textAnchor="middle"
                fontSize={10}
                fill="rgb(165 180 252 / 0.6)"
                className="font-mono"
              >
                {nm}
              </text>
              <text
                x={p.x}
                y={p.y + 30}
                textAnchor="middle"
                fontSize={8}
                fill="rgb(165 180 252 / 0.4)"
              >
                existing
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}


// ---------- Member detail / edit panel ---------- //

function MemberPanel({
  member,
  reservedNames,
  teammates,
  existingAgents,
  onUpdate,
  onRemove,
}: {
  member: TeamMember | null;
  reservedNames: Set<string>;
  teammates: string[];
  existingAgents: string[];
  onUpdate: (origName: string, patch: Partial<TeamMember>) => void;
  onRemove: (name: string) => void;
}) {
  const [nameDraft, setNameDraft] = useState("");
  const [editingDescription, setEditingDescription] = useState(false);
  const [descriptionDraft, setDescriptionDraft] = useState("");

  useEffect(() => {
    setNameDraft(member?.name ?? "");
    setDescriptionDraft(member?.draft_description ?? "");
    setEditingDescription(false);
  }, [member?.name]);

  if (!member) {
    return (
      <aside className="rounded-lg border border-gray-200 bg-white p-5 text-sm text-gray-500">
        Click a star in the preview to inspect or edit a bot.
      </aside>
    );
  }

  const validDispatchTargets = [
    ...teammates,
    ...existingAgents.filter((n) => n !== member.name),
  ];

  const onSaveName = () => {
    const candidate = nameDraft.trim().toLowerCase();
    if (candidate === member.name) return;
    if (!isStarName(candidate)) {
      alert(`"${candidate}" is not in the star-naming dictionary.`);
      return;
    }
    if (reservedNames.has(candidate) && candidate !== member.name) {
      alert(`"${candidate}" is already in use.`);
      return;
    }
    onUpdate(member.name, { name: candidate });
  };

  const onToggleDispatch = (target: string) => {
    const cur = new Set(member.dispatches_to);
    if (cur.has(target)) cur.delete(target);
    else if (cur.size >= 2) {
      alert("Each bot dispatches to at most 2 targets (avoid spaghetti).");
      return;
    } else cur.add(target);
    onUpdate(member.name, { dispatches_to: Array.from(cur) });
  };

  return (
    <aside className="rounded-lg border border-gray-200 bg-white p-5 space-y-4">
      {/* Name + role */}
      <div>
        <label className="block text-[11px] uppercase tracking-wider text-gray-500 mb-1">
          Name
        </label>
        <div className="flex items-center gap-2">
          <select
            value={nameDraft}
            onChange={(e) => setNameDraft(e.target.value)}
            className="font-mono text-sm border border-gray-300 rounded px-2 py-1"
          >
            <option value={member.name}>{member.name}</option>
            {STAR_DICTIONARY.filter((s) => !reservedNames.has(s.name)).map(
              (s) => (
                <option key={s.name} value={s.name} title={s.theme}>
                  {s.name}
                </option>
              ),
            )}
          </select>
          {nameDraft !== member.name && (
            <button
              type="button"
              onClick={onSaveName}
              className="text-xs text-accent-600 hover:text-accent-700"
            >
              rename
            </button>
          )}
        </div>
      </div>

      <div>
        <label className="block text-[11px] uppercase tracking-wider text-gray-500 mb-1">
          Role
        </label>
        <select
          value={member.role}
          onChange={(e) =>
            onUpdate(member.name, {
              role: e.target.value as TeamMemberRole,
            })
          }
          className="text-sm border border-gray-300 rounded px-2 py-1"
        >
          <option value="orchestrator">orchestrator</option>
          <option value="specialist">specialist</option>
          <option value="messenger">messenger</option>
        </select>
      </div>

      {/* Summary */}
      <div>
        <label className="block text-[11px] uppercase tracking-wider text-gray-500 mb-1">
          Summary
        </label>
        <p className="text-sm text-gray-800">{member.summary}</p>
      </div>

      {/* Command kinds */}
      {member.command_kinds.length > 0 && (
        <div>
          <label className="block text-[11px] uppercase tracking-wider text-gray-500 mb-1">
            Commands
          </label>
          <div className="flex flex-wrap gap-1">
            {member.command_kinds.map((c) => (
              <code
                key={c}
                className="font-mono text-[11px] bg-gray-100 text-gray-700 px-1.5 py-0.5 rounded"
              >
                {c}
              </code>
            ))}
          </div>
        </div>
      )}

      {/* Dispatch targets — checklist */}
      <div>
        <label className="block text-[11px] uppercase tracking-wider text-gray-500 mb-1">
          Dispatches to
        </label>
        {validDispatchTargets.length === 0 ? (
          <p className="text-xs text-gray-400">
            no other bots to dispatch to yet
          </p>
        ) : (
          <ul className="space-y-1">
            {validDispatchTargets.map((t) => {
              const checked = member.dispatches_to.includes(t);
              const isExisting = existingAgents.includes(t);
              return (
                <li key={t} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => onToggleDispatch(t)}
                    className="rounded"
                  />
                  <span className="font-mono text-gray-800">{t}</span>
                  {isExisting && (
                    <span className="text-[10px] uppercase tracking-wider text-emerald-700 bg-emerald-50 px-1.5 py-0.5 rounded">
                      existing
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {/* Required secrets */}
      {member.needs_workspace_secrets.length > 0 && (
        <div>
          <label className="block text-[11px] uppercase tracking-wider text-gray-500 mb-1">
            Needs secrets
          </label>
          <div className="flex flex-wrap gap-1">
            {member.needs_workspace_secrets.map((s) => (
              <code
                key={s}
                className="font-mono text-[11px] bg-amber-50 text-amber-900 px-1.5 py-0.5 rounded"
              >
                {s}
              </code>
            ))}
          </div>
        </div>
      )}

      {/* Draft description (editable) */}
      <div>
        <label className="block text-[11px] uppercase tracking-wider text-gray-500 mb-1">
          Description (feeds the per-bot generator in 12C.3)
        </label>
        {editingDescription ? (
          <div className="space-y-2">
            <textarea
              value={descriptionDraft}
              onChange={(e) => setDescriptionDraft(e.target.value)}
              className="w-full h-32 px-2 py-1 border border-gray-300 rounded text-xs"
            />
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => {
                  onUpdate(member.name, {
                    draft_description: descriptionDraft,
                  });
                  setEditingDescription(false);
                }}
                className="px-2 py-0.5 text-xs bg-accent-600 text-white rounded hover:bg-accent-700"
              >
                save
              </button>
              <button
                type="button"
                onClick={() => {
                  setDescriptionDraft(member.draft_description);
                  setEditingDescription(false);
                }}
                className="text-xs text-gray-500 hover:text-gray-900"
              >
                cancel
              </button>
            </div>
          </div>
        ) : (
          <div>
            <p className="text-xs text-gray-700 whitespace-pre-wrap leading-relaxed">
              {member.draft_description}
            </p>
            <button
              type="button"
              onClick={() => setEditingDescription(true)}
              className="text-xs text-accent-600 hover:text-accent-700 mt-1"
            >
              edit description
            </button>
          </div>
        )}
      </div>

      {/* Remove */}
      <div className="border-t border-gray-100 pt-3 flex justify-end">
        <button
          type="button"
          onClick={() => onRemove(member.name)}
          className="text-xs text-gray-400 hover:text-red-600"
        >
          remove from team
        </button>
      </div>
    </aside>
  );
}


// ---------- Generation progress + review (12C.3) ---------- //

function statusChipClass(s: GenStatus): string {
  switch (s) {
    case "success":
      return "bg-emerald-100 text-emerald-800";
    case "failed":
      return "bg-red-100 text-red-800";
    case "generating":
      return "bg-indigo-100 text-indigo-800 animate-pulse";
    case "skipped":
      return "bg-gray-100 text-gray-500";
    default:
      return "bg-gray-100 text-gray-700";
  }
}

function GenerationSection({
  team,
  genResults,
  phase,
  onRetry,
  onSkip,
  onBackToPlan,
}: {
  team: TeamMember[];
  genResults: Record<string, GenResult>;
  phase: Phase;
  onRetry: (name: string, descriptionOverride?: string) => void;
  onSkip: (name: string) => void;
  onBackToPlan: () => void;
}) {
  const successCount = team.filter(
    (m) => genResults[m.name]?.status === "success",
  ).length;
  const failedCount = team.filter(
    (m) => genResults[m.name]?.status === "failed",
  ).length;
  const skippedCount = team.filter(
    (m) => genResults[m.name]?.status === "skipped",
  ).length;
  const generatingCount = team.filter(
    (m) => genResults[m.name]?.status === "generating",
  ).length;

  const allDone = phase === "review";
  const deployableCount = successCount;

  return (
    <div className="mt-8 border-t border-gray-100 pt-6">
      <div className="flex items-baseline justify-between mb-4">
        <h3 className="text-lg font-semibold tracking-tight">
          {phase === "generating"
            ? "Generating code…"
            : "Review generated code"}
        </h3>
        <div className="text-xs text-gray-500 space-x-3 tabular-nums">
          <span className="text-emerald-700">{successCount} ok</span>
          {generatingCount > 0 && (
            <span className="text-indigo-700">{generatingCount} running</span>
          )}
          {failedCount > 0 && (
            <span className="text-red-700">{failedCount} failed</span>
          )}
          {skippedCount > 0 && (
            <span className="text-gray-500">{skippedCount} skipped</span>
          )}
        </div>
      </div>

      <ul className="space-y-3">
        {team.map((m) => {
          const r = genResults[m.name];
          if (!r) return null;
          return (
            <GenerationRow
              key={m.name}
              member={m}
              result={r}
              onRetry={onRetry}
              onSkip={onSkip}
            />
          );
        })}
      </ul>

      {allDone && (
        <div className="mt-8 flex items-center justify-between gap-4 border-t border-gray-100 pt-6">
          <div className="flex items-center gap-3 text-xs text-gray-500">
            <button
              type="button"
              onClick={onBackToPlan}
              className="text-accent-600 hover:text-accent-700"
            >
              ← back to plan
            </button>
            <span className="text-gray-300">·</span>
            <span>
              {deployableCount} of {team.length} bot
              {team.length === 1 ? "" : "s"} ready to deploy
              {failedCount > 0 && ` (${failedCount} need attention)`}
            </span>
          </div>
          <button
            type="button"
            disabled
            className="px-5 py-2 bg-accent-600 text-white rounded-md text-sm font-medium opacity-50 cursor-not-allowed"
            title="Bulk deploy + auto-approval rules ship in Phase 12C.4."
          >
            Deploy team → (12C.4)
          </button>
        </div>
      )}
    </div>
  );
}

function GenerationRow({
  member,
  result,
  onRetry,
  onSkip,
}: {
  member: TeamMember;
  result: GenResult;
  onRetry: (name: string, descriptionOverride?: string) => void;
  onSkip: (name: string) => void;
}) {
  const [showCode, setShowCode] = useState(false);
  const [editingDesc, setEditingDesc] = useState(false);
  const [draft, setDraft] = useState(result.description);

  // Keep the textarea draft in sync if the description gets rewritten
  // externally (e.g. by a fresh onGenerate from "back to plan").
  useEffect(() => {
    setDraft(result.description);
    setEditingDesc(false);
  }, [result.description, result.status]);

  return (
    <li className="rounded-lg border border-gray-200 bg-white px-5 py-4">
      <div className="flex items-baseline justify-between gap-4">
        <div className="flex items-baseline gap-3 min-w-0">
          <span className="font-mono text-sm text-gray-900">{member.name}</span>
          <span
            className={
              "text-[10px] uppercase tracking-wider font-medium px-2 py-0.5 rounded-full " +
              statusChipClass(result.status)
            }
          >
            {result.status}
          </span>
          {result.output?.model_used && (
            <span className="text-[11px] text-gray-400 font-mono truncate">
              {result.output.model_used}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 text-xs shrink-0">
          {result.status === "success" && (
            <button
              type="button"
              onClick={() => setShowCode((s) => !s)}
              className="text-accent-600 hover:text-accent-700"
            >
              {showCode ? "hide code" : "show code"}
            </button>
          )}
          {result.status === "failed" && (
            <>
              <button
                type="button"
                onClick={() => onRetry(member.name)}
                className="text-accent-600 hover:text-accent-700"
              >
                retry
              </button>
              <button
                type="button"
                onClick={() => setEditingDesc(true)}
                className="text-accent-600 hover:text-accent-700"
              >
                edit &amp; retry
              </button>
              <button
                type="button"
                onClick={() => onSkip(member.name)}
                className="text-gray-400 hover:text-red-600"
              >
                skip
              </button>
            </>
          )}
        </div>
      </div>

      {result.status === "failed" && result.error && (
        <p className="mt-2 text-xs text-red-700 break-words">
          {result.error}
        </p>
      )}

      {editingDesc && (
        <div className="mt-3 space-y-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="w-full h-32 px-2 py-1 border border-gray-300 rounded text-xs font-mono"
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => {
                onRetry(member.name, draft);
                setEditingDesc(false);
              }}
              className="px-2 py-0.5 text-xs bg-accent-600 text-white rounded hover:bg-accent-700"
            >
              retry with new description
            </button>
            <button
              type="button"
              onClick={() => {
                setDraft(result.description);
                setEditingDesc(false);
              }}
              className="text-xs text-gray-500 hover:text-gray-900"
            >
              cancel
            </button>
          </div>
        </div>
      )}

      {showCode && result.output && (
        <div className="mt-3 space-y-3">
          {result.output.rationale && (
            <div>
              <div className="text-[11px] uppercase tracking-wider text-gray-500 mb-1">
                Rationale
              </div>
              <p className="text-xs text-gray-700">
                {result.output.rationale}
              </p>
            </div>
          )}
          <div>
            <div className="text-[11px] uppercase tracking-wider text-gray-500 mb-1">
              bot.py
            </div>
            <pre className="bg-gray-50 border border-gray-100 rounded p-3 text-[11px] font-mono text-gray-800 overflow-x-auto max-h-96">
              {result.output.bot_py}
            </pre>
          </div>
          <div>
            <div className="text-[11px] uppercase tracking-wider text-gray-500 mb-1">
              requirements.txt
            </div>
            <pre className="bg-gray-50 border border-gray-100 rounded p-3 text-[11px] font-mono text-gray-800 overflow-x-auto">
              {result.output.requirements_txt}
            </pre>
          </div>
        </div>
      )}
    </li>
  );
}
