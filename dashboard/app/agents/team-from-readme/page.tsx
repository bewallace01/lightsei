"use client";

import JSZip from "jszip";
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
  fetchSecrets,
  fetchTeamPlan,
  generateAgent,
  patchAgent,
  uploadDeploymentBundle,
  upsertAutoApprovalRule,
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

type Phase = "plan" | "generating" | "review" | "deploying" | "success";

type DeployStatus = "pending" | "zipping" | "deploying" | "deployed" | "failed";

type DeployResult = {
  status: DeployStatus;
  deploymentId?: string;
  error?: string;
};

type RulesResult = {
  installed: { source: string; target: string; kind: string }[];
  failed: { source: string; target: string; kind: string; error: string }[];
};

// Bulk-generate concurrency cap (12C.6). The per-bot endpoint runs up
// to two Opus + tool-call round trips and can take 60-120s; firing all
// N at once trips proxy / connection timeouts upstream. 2 in flight at
// a time with a small jitter between starts is empirically enough to
// finish a 5-bot team in the same wall-clock as the unbounded burst,
// without the long tail of timeout failures.
const BULK_GENERATE_CONCURRENCY = 2;
const BULK_GENERATE_JITTER_MS = 250;

// ---------- Workspace-secrets guidance ---------- //
//
// Rendered inside the missing-secrets <details> on the deploy-success
// view. Keyed on the secret name the team-planner LLM proposes; the
// fallback handles secret names we haven't seen before (Claude can
// invent reasonable ones for niche providers).
//
// Keep this short: one-line "what it is" + a hint about scopes /
// permissions when relevant. The link goes to the page where the user
// generates / copies the value.

type SecretGuide = {
  what: string;
  where: string;
  url: string;
};

const SECRET_GUIDANCE: Record<string, SecretGuide> = {
  ANTHROPIC_API_KEY: {
    what: "Used by bots that call Claude (Anthropic) for LLM responses.",
    where:
      "Anthropic Console → Settings → API Keys. Create a key, copy it (you only see it once), drop it on /account.",
    url: "https://console.anthropic.com/settings/keys",
  },
  OPENAI_API_KEY: {
    what: "Used by bots that call GPT / o-series models.",
    where:
      "OpenAI platform → API keys → Create new secret key. Project-scoped keys are fine; you only see the value once.",
    url: "https://platform.openai.com/api-keys",
  },
  GOOGLE_API_KEY: {
    what: "Used by bots that call Gemini.",
    where:
      "Google AI Studio → Get API key → Create API key in new project (or use an existing one).",
    url: "https://aistudio.google.com/apikey",
  },
  GITHUB_TOKEN: {
    what:
      "Used by bots that read or write to GitHub (PRs, issues, repo contents, push status).",
    where:
      "GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens. Grant only the repos + permissions the bots need (e.g. Contents: Read, Pull requests: Read & Write).",
    url: "https://github.com/settings/personal-access-tokens/new",
  },
  SLACK_WEBHOOK_URL: {
    what: "Posts messages to a single Slack channel.",
    where:
      "Slack → Apps → Incoming Webhooks → Add to Slack → pick the channel → copy the webhook URL (starts with https://hooks.slack.com/services/...).",
    url: "https://api.slack.com/messaging/webhooks",
  },
  DISCORD_WEBHOOK_URL: {
    what: "Posts messages to a single Discord channel.",
    where:
      "Discord channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL.",
    url: "https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks",
  },
  LIGHTSEI_API_KEY: {
    what:
      "Your workspace's own Lightsei key. Bots use it to send telemetry + check policies. Usually auto-provided on the bot's runtime, but list it here if you're running a bot outside Lightsei's worker.",
    where: "Lightsei → /account → API keys → Generate new key.",
    url: "/account",
  },
};

// Catch-all for whatever Claude decides to propose. Keeps the dropdown
// useful even when the secret name isn't in SECRET_GUIDANCE — better
// than a blank box.
function guidanceFor(name: string): SecretGuide {
  const exact = SECRET_GUIDANCE[name];
  if (exact) return exact;
  return {
    what:
      "A workspace secret one of the proposed bots expects. Lightsei doesn't have a built-in template for this one.",
    where:
      "Check the relevant provider's API key / token page, copy the value, then add it on /account with this exact name.",
    url: "/account",
  };
}


async function runWithConcurrencyLimit<T>(
  items: T[],
  limit: number,
  worker: (item: T) => Promise<void>,
): Promise<void> {
  let cursor = 0;
  const lanes = Array.from(
    { length: Math.min(limit, items.length) },
    async () => {
      while (cursor < items.length) {
        const i = cursor++;
        if (i >= limit) {
          await new Promise((r) =>
            setTimeout(
              r,
              BULK_GENERATE_JITTER_MS + Math.floor(Math.random() * BULK_GENERATE_JITTER_MS),
            ),
          );
        }
        await worker(items[i]);
      }
    },
  );
  await Promise.all(lanes);
}

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

  // Phase 12C.4 bulk-deploy state. Each approved bot lands here as the
  // zip/upload completes. `rulesResult` is the auto-approval install
  // outcome (only attempted once all deploys finish); `missingSecrets`
  // is the checklist rendered on the success page.
  const [deployResults, setDeployResults] = useState<
    Record<string, DeployResult>
  >({});
  const [rulesResult, setRulesResult] = useState<RulesResult | null>(null);
  const [missingSecrets, setMissingSecrets] = useState<string[]>([]);

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

    // Capped concurrency (12C.6). runOneGenerate already swallows its
    // own errors into per-row state, so we don't need allSettled — we
    // just need to stop launching all N at once.
    await runWithConcurrencyLimit(team, BULK_GENERATE_CONCURRENCY, (m) =>
      runOneGenerate(m, descriptionFor(m, team)),
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

  // ---------- Bulk deploy + rule wiring (12C.4) ---------- //

  /** Build a one-bot deployment zip in-browser and POST it. Mirrors
   *  the 12B.2 path; returns the resolved Deployment row or throws. */
  const zipAndDeploy = async (
    name: string,
    botPy: string,
    requirementsTxt: string,
  ): Promise<{ id: string }> => {
    const zip = new JSZip();
    zip.file("bot.py", botPy);
    zip.file("requirements.txt", requirementsTxt || "lightsei>=0.1.6\n");
    const blob = await zip.generateAsync({ type: "blob" });
    const file = new File([blob], `${name}.zip`, { type: "application/zip" });
    return await uploadDeploymentBundle(name, file);
  };

  /** Install auto-approval rules from the plan's dispatch graph. Edges
   *  to team-members get rules per kind the target handles; edges to
   *  existing (out-of-team) agents are skipped here because we don't
   *  know their command kinds from the plan — the user can wire those
   *  on the agent's auto-approval page if they want. */
  const installRules = async (): Promise<RulesResult> => {
    const teamByName = new Map(team.map((m) => [m.name, m]));
    const installed: RulesResult["installed"] = [];
    const failed: RulesResult["failed"] = [];

    const promises: Promise<void>[] = [];
    for (const src of team) {
      // Only wire rules for bots that actually got deployed.
      if (deployResults[src.name]?.status !== "deployed") continue;
      for (const targetName of src.dispatches_to) {
        const targetMember = teamByName.get(targetName);
        if (!targetMember) continue; // existing-agent edge; skip
        if (deployResults[targetName]?.status !== "deployed") continue;
        for (const kind of targetMember.command_kinds) {
          const rule = {
            source_agent: src.name,
            target_agent: targetName,
            command_kind: kind,
            mode: "auto_approve" as const,
          };
          promises.push(
            upsertAutoApprovalRule(rule)
              .then(() => {
                installed.push({
                  source: src.name,
                  target: targetName,
                  kind,
                });
              })
              .catch((e) => {
                failed.push({
                  source: src.name,
                  target: targetName,
                  kind,
                  error: e instanceof Error ? e.message : String(e),
                });
              }),
          );
        }
      }
    }
    await Promise.allSettled(promises);
    return { installed, failed };
  };

  const onDeploy = async () => {
    // Approved bots = those with a successful generation that the user
    // didn't skip. Failed-and-not-skipped rows get an alert so the user
    // can decide explicitly rather than silently dropping them.
    const approved = team.filter(
      (m) =>
        genResults[m.name]?.status === "success" && genResults[m.name]?.output,
    );
    const stillFailed = team.filter(
      (m) => genResults[m.name]?.status === "failed",
    );
    if (approved.length === 0) {
      setError(
        "No bots are ready to deploy. Generate code or unskip at least one bot.",
      );
      return;
    }
    if (stillFailed.length > 0) {
      const ok = confirm(
        `${stillFailed.length} bot(s) still show "failed" and will be excluded ` +
        `from the deploy. Skip them and continue, or cancel to retry first?\n\n` +
        "OK = deploy the rest, Cancel = stay on review.",
      );
      if (!ok) return;
    }

    setPhase("deploying");
    setError(null);
    setRulesResult(null);
    setMissingSecrets([]);

    // Seed every approved row to `pending` so the progress UI lights
    // up immediately.
    const initial: Record<string, DeployResult> = {};
    for (const m of approved) initial[m.name] = { status: "pending" };
    setDeployResults(initial);

    await Promise.allSettled(
      approved.map(async (m) => {
        const out = genResults[m.name]?.output;
        if (!out) return;
        setDeployResults((cur) => ({
          ...cur,
          [m.name]: { status: "zipping" },
        }));
        try {
          const dep = await zipAndDeploy(m.name, out.bot_py, out.requirements_txt);
          setDeployResults((cur) => ({
            ...cur,
            [m.name]: { status: "deployed", deploymentId: dep.id },
          }));
          // Best-effort: carry the LLM rationale as the agent's
          // description so /agents has something to show. Don't block
          // on failure — the bot is deployed and that's what counts.
          if (out.rationale) {
            patchAgent(m.name, { description: out.rationale }).catch(
              () => undefined,
            );
          }
        } catch (e) {
          if (e instanceof UnauthorizedError) {
            router.replace("/login");
            return;
          }
          setDeployResults((cur) => ({
            ...cur,
            [m.name]: {
              status: "failed",
              error: e instanceof Error ? e.message : String(e),
            },
          }));
        }
      }),
    );

    // Rules + secrets after deploys settle. Both are best-effort —
    // failures here surface in the success view but don't roll back
    // the deploy.
    try {
      const r = await installRules();
      setRulesResult(r);
    } catch (e) {
      setRulesResult({
        installed: [],
        failed: [
          {
            source: "?",
            target: "?",
            kind: "?",
            error: e instanceof Error ? e.message : String(e),
          },
        ],
      });
    }

    try {
      const have = new Set((await fetchSecrets()).map((s) => s.name));
      const wanted = new Set<string>();
      for (const m of approved) {
        for (const s of m.needs_workspace_secrets || []) wanted.add(s);
      }
      const missing = Array.from(wanted).filter((n) => !have.has(n));
      setMissingSecrets(missing);
    } catch {
      // Couldn't list secrets — don't gate the success view on it.
      setMissingSecrets([]);
    }

    setPhase("success");
  };

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

          <div className="grid grid-cols-1 md:grid-cols-[1fr_320px] gap-6 md:items-start">
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

          {(phase === "generating" || phase === "review") && (
            <GenerationSection
              team={team}
              genResults={genResults}
              phase={phase}
              onRetry={onRetry}
              onSkip={onSkip}
              onBackToPlan={onBackToPlan}
              onDeploy={onDeploy}
            />
          )}

          {(phase === "deploying" || phase === "success") && (
            <DeploySection
              team={team}
              genResults={genResults}
              deployResults={deployResults}
              rulesResult={rulesResult}
              missingSecrets={missingSecrets}
              phase={phase}
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

// Square viewBox + `aspect-square` on the container = the dark canvas
// is a balanced square regardless of how tall the side panel grows.
// `md:items-start` on the grid wrapper detaches the dark box from the
// panel's column-stretch so it sizes by its own aspect ratio.
const VB_W = 600;
const VB_H = 600;

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
  // Specialists sit at ~67% of the half-canvas; messengers push a
  // little further out so their edge length implies "downstream / leaf".
  const radius = role === "messenger" ? 240 : 200;
  const angle =
    (i / Math.max(1, total)) * Math.PI * 2 - Math.PI / 2;
  return {
    x: VB_W / 2 + Math.cos(angle) * radius,
    y: VB_H / 2 + Math.sin(angle) * radius,
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
      // Spread ghost stubs along the right edge but keep them away
      // from the top/bottom corners so labels don't clip.
      const y = VB_H * (0.18 + (0.64 * i) / Math.max(1, total - 1 || 1));
      m.set(nm, { x: VB_W - 60, y });
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
    <div className="rounded-lg border border-indigo-900/30 bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900 overflow-hidden aspect-square md:self-start">
      <svg
        viewBox={`0 0 ${VB_W} ${VB_H}`}
        preserveAspectRatio="xMidYMid meet"
        className="w-full h-full block"
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
          const size = isOrch ? 26 : 18;
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
                  r={size + 10}
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
                y={p.y + size + 18}
                textAnchor="middle"
                fontSize={14}
                fill="rgb(199 210 254 / 0.95)"
                className="font-mono"
              >
                {m.name}
              </text>
              <text
                x={p.x}
                y={p.y + size + 34}
                textAnchor="middle"
                fontSize={11}
                fill="rgb(165 180 252 / 0.65)"
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
              <circle cx={p.x} cy={p.y} r={9} fill="rgb(99 102 241 / 0.4)" />
              <text
                x={p.x}
                y={p.y + 24}
                textAnchor="middle"
                fontSize={12}
                fill="rgb(165 180 252 / 0.65)"
                className="font-mono"
              >
                {nm}
              </text>
              <text
                x={p.x}
                y={p.y + 38}
                textAnchor="middle"
                fontSize={10}
                fill="rgb(165 180 252 / 0.45)"
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
  onDeploy,
}: {
  team: TeamMember[];
  genResults: Record<string, GenResult>;
  phase: Phase;
  onRetry: (name: string, descriptionOverride?: string) => void;
  onSkip: (name: string) => void;
  onBackToPlan: () => void;
  onDeploy: () => void;
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
            onClick={onDeploy}
            disabled={deployableCount === 0}
            className="px-5 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Deploy team →
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


// ---------- Deploy + rule wiring + success view (12C.4) ---------- //

function deployStatusChipClass(s: DeployStatus): string {
  switch (s) {
    case "deployed":
      return "bg-emerald-100 text-emerald-800";
    case "failed":
      return "bg-red-100 text-red-800";
    case "deploying":
    case "zipping":
      return "bg-indigo-100 text-indigo-800 animate-pulse";
    default:
      return "bg-gray-100 text-gray-700";
  }
}

function DeploySection({
  team,
  genResults,
  deployResults,
  rulesResult,
  missingSecrets,
  phase,
}: {
  team: TeamMember[];
  genResults: Record<string, GenResult>;
  deployResults: Record<string, DeployResult>;
  rulesResult: RulesResult | null;
  missingSecrets: string[];
  phase: Phase;
}) {
  // Approved (non-skipped, generated) bots only — the rest never
  // entered the deploy pipeline.
  const approved = team.filter(
    (m) => genResults[m.name]?.status === "success",
  );

  const deployed = approved.filter(
    (m) => deployResults[m.name]?.status === "deployed",
  );
  const failed = approved.filter(
    (m) => deployResults[m.name]?.status === "failed",
  );
  const inFlight = approved.filter((m) => {
    const s = deployResults[m.name]?.status;
    return s === "pending" || s === "zipping" || s === "deploying";
  });

  return (
    <div className="mt-8 border-t border-gray-100 pt-6">
      <div className="flex items-baseline justify-between mb-4">
        <h3 className="text-lg font-semibold tracking-tight">
          {phase === "deploying" ? "Deploying team…" : "Team deployed"}
        </h3>
        <div className="text-xs text-gray-500 space-x-3 tabular-nums">
          <span className="text-emerald-700">{deployed.length} deployed</span>
          {inFlight.length > 0 && (
            <span className="text-indigo-700">{inFlight.length} running</span>
          )}
          {failed.length > 0 && (
            <span className="text-red-700">{failed.length} failed</span>
          )}
        </div>
      </div>

      {/* Per-bot deploy rows. */}
      <ul className="space-y-2 mb-6">
        {approved.map((m) => {
          const r = deployResults[m.name] ?? { status: "pending" as const };
          return (
            <li
              key={m.name}
              className="rounded-md border border-gray-200 bg-white px-4 py-3 flex items-center justify-between gap-4"
            >
              <div className="flex items-center gap-3 min-w-0">
                <span className="font-mono text-sm text-gray-900">
                  {m.name}
                </span>
                <span
                  className={
                    "text-[10px] uppercase tracking-wider font-medium px-2 py-0.5 rounded-full " +
                    deployStatusChipClass(r.status)
                  }
                >
                  {r.status}
                </span>
                {r.error && (
                  <span className="text-xs text-red-700 truncate">
                    {r.error}
                  </span>
                )}
              </div>
              {r.status === "deployed" && r.deploymentId && (
                <Link
                  href={`/deployments/${r.deploymentId}`}
                  className="text-xs text-accent-600 hover:text-accent-700 whitespace-nowrap"
                >
                  view deployment →
                </Link>
              )}
            </li>
          );
        })}
      </ul>

      {/* Success-only panels. */}
      {phase === "success" && (
        <div className="space-y-6">
          {/* Auto-approval rule summary. */}
          {rulesResult && (rulesResult.installed.length > 0 || rulesResult.failed.length > 0) && (
            <div className="rounded-md border border-gray-200 bg-white p-4">
              <h4 className="text-sm font-semibold tracking-tight mb-2">
                Auto-approval rules
              </h4>
              {rulesResult.installed.length > 0 && (
                <ul className="text-xs text-gray-700 space-y-1 mb-2">
                  {rulesResult.installed.map((r, i) => (
                    <li key={i}>
                      <code className="font-mono">{r.source}</code> →{" "}
                      <code className="font-mono">{r.target}</code> for{" "}
                      <code className="font-mono">{r.kind}</code>
                      <span className="text-emerald-700 ml-2">installed</span>
                    </li>
                  ))}
                </ul>
              )}
              {rulesResult.failed.length > 0 && (
                <ul className="text-xs text-red-700 space-y-1">
                  {rulesResult.failed.map((r, i) => (
                    <li key={i}>
                      <code className="font-mono">{r.source}</code> →{" "}
                      <code className="font-mono">{r.target}</code> for{" "}
                      <code className="font-mono">{r.kind}</code>:{" "}
                      {r.error}
                    </li>
                  ))}
                </ul>
              )}
              <p className="text-[11px] text-gray-400 mt-2">
                Rules govern the dispatch graph: when{" "}
                <code className="font-mono">source</code> sends to{" "}
                <code className="font-mono">target</code> with this kind,
                the command auto-runs instead of waiting for human approval.
                Edit on the agent page or{" "}
                <Link
                  href="/dispatch"
                  className="text-accent-600 hover:text-accent-700"
                >
                  dispatch chains
                </Link>
                .
              </p>
            </div>
          )}

          {/* Missing-secrets checklist. */}
          {missingSecrets.length > 0 && (
            <div className="rounded-md border border-amber-200 bg-amber-50 p-4">
              <h4 className="text-sm font-semibold tracking-tight text-amber-900 mb-2">
                ⚠ Set these workspace secrets before the bots run
              </h4>
              <p className="text-xs text-amber-900 mb-3">
                The proposed team needs the following secrets you
                haven&apos;t set yet. Without them, the relevant bots
                will crash on first run.
              </p>
              <ul className="space-y-2">
                {missingSecrets.map((name) => {
                  const g = guidanceFor(name);
                  const isExternal = /^https?:\/\//.test(g.url);
                  return (
                    <li key={name}>
                      <details className="group rounded border border-amber-200 bg-white/60 open:bg-white">
                        <summary className="cursor-pointer list-none px-3 py-2 text-sm text-amber-900 flex items-center justify-between">
                          <code className="font-mono">{name}</code>
                          <span className="text-xs text-amber-700 group-open:hidden">
                            where to get this →
                          </span>
                          <span className="text-xs text-amber-700 hidden group-open:inline">
                            hide
                          </span>
                        </summary>
                        <div className="px-3 pb-3 pt-1 text-xs text-amber-900 space-y-2">
                          <p>{g.what}</p>
                          <p>{g.where}</p>
                          {isExternal ? (
                            <a
                              href={g.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-block text-accent-600 hover:text-accent-700 font-medium"
                            >
                              open the page →
                            </a>
                          ) : (
                            <Link
                              href={g.url}
                              className="inline-block text-accent-600 hover:text-accent-700 font-medium"
                            >
                              open {g.url} →
                            </Link>
                          )}
                        </div>
                      </details>
                    </li>
                  );
                })}
              </ul>
              <Link
                href="/account"
                className="inline-block mt-3 text-xs text-accent-600 hover:text-accent-700 font-medium"
              >
                set secrets on /account →
              </Link>
            </div>
          )}

          {/* CTAs */}
          <div className="flex items-center justify-between gap-4 border-t border-gray-100 pt-4">
            <span className="text-xs text-gray-500">
              {deployed.length} of {approved.length} bot
              {approved.length === 1 ? "" : "s"} deployed
              {failed.length > 0 && ` · ${failed.length} failed (see above)`}
            </span>
            <div className="flex items-center gap-3">
              <Link
                href="/"
                className="text-xs text-gray-500 hover:text-gray-900"
              >
                home
              </Link>
              <Link
                href="/agents"
                className="px-4 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 no-underline"
              >
                See the roster →
              </Link>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
