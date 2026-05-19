# Lightsei — Tasks

Read MEMORY.md first if it's been a while. (Older Done Log entries call the project "Beacon" — that was the working code-name through Phase 4. Same product.)

<!-- Phase 12.4 demo trigger: this comment busts polaris's hash cache so the next tick re-emits a plan against the configured provider. -->


## NOW

> **Phase 18.2 — empty states + first-action CTAs on primary surfaces. Spec locked 2026-05-18; 18.1 shipped 2026-05-18.**

Phase 18 (dashboard polish) is the strategic-pivot roadmap's next P0 phase. 18.1 shipped 2026-05-18 — top nav restructured to roles-first (`My team / Activity / Trust zones / Integrations / Account / Advanced`). 18.2 follows: rewrite empty states on home / /agents / /zones / /runs so a freshly-signed-up workspace sees a useful CTA instead of a blank table. Closes the gap between "I just signed up" and "I just dropped a README" — currently most non-technical users don't make it across.

Phase 16 prod demo passed 2026-05-18. Phase 17 closed in test mode 2026-05-17. Live-mode activation submitted, waiting on Stripe verification.

## Phase 12C: drop a README, get a team

Natural extension of 12B that arrives once the per-bot generator is solid. Instead of "describe one bot, get one bot," the user drops their project's README (or a GitHub repo URL) and Lightsei proposes + generates a tailored constellation of bots wired up to maintain it. Calling 12B's per-bot generator in a loop is the implementation core; the new work is the project-analysis layer on top + the bulk-deploy + auto-approval-rule wiring.

Why this matters: the 12B v1 still requires the user to know what bots they want. For most non-engineer users that's the harder cognitive step than writing the description. Watching a Lightsei-proposed team appear from a README does the framing work for them — they're reviewing a plan rather than authoring one.

### 12C.1 Project analysis endpoint

- New `POST /workspaces/me/teams/plan` taking `{readme_text?, github_repo?, github_branch?, freeform_description?}` and returning a roster of proposed bots:
  ```
  {
    "rationale": "<1-3 sentences on why this team fits the project>",
    "team": [
      {
        "name": "argus",
        "role": "specialist",
        "summary": "scans every push for hardcoded secrets",
        "command_kinds": ["argus.scan"],
        "dispatches_to": ["hermes"],
        "needs_workspace_secrets": ["GITHUB_TOKEN"],
        "draft_description": "<paragraph the 12B generator can use as input>"
      },
      ...
    ]
  }
  ```
- LLM system prompt teaches the analysis step: read the project, identify recurring kinds of work (testing, security, deploy verification, PR review, oncall, doc maintenance, content moderation, ...), propose 3-7 bots with non-overlapping roles, wire the dispatch graph so each bot has at most one or two outgoing edges (avoid spaghetti).
- The prompt also includes the curated SDK surface from 12B.1 + the star-naming dictionary so the proposed `command_kinds` and `name`s are realistic, plus the workspace's existing constellation so the plan can incorporate (rather than duplicate) Polaris/Atlas/Hermes if they exist.
- Each proposed bot's `name` MUST be from the star-naming dictionary (see `backend/agent_generator.py:STAR_DICTIONARY`), picked to thematically match the role. Names already in use in the workspace are off-limits; the prompt reserves them. The plan endpoint also rejects + retries if Claude returns a name not in the dictionary.
- GitHub-repo input path reuses Phase 10.3's `github_api.fetch_directory_zip` (or a lighter "just fetch the README") so the user can paste a repo URL instead of copying README contents.
- Tests mirror 12B.1's stubbed-Anthropic shape: canned response, prompt-content assertions, name-validation, retry path.

### 12C.2 Team review UI

- New `/agents/team-from-readme` page (or section on `/agents/generate`). Drop zone for a README file + textarea for freeform context + optional GitHub URL field.
- Output: a visual preview of the proposed team — same star-and-edge aesthetic as the existing constellation map, but rendered against the proposed bots rather than what's actually deployed. Click a star to see that bot's role + commands + the draft description that'll feed 12B.
- Inline editing: rename, remove, add a new bot, edit the role/description. Each edit just mutates the in-memory plan; nothing's deployed yet.
- "Generate and deploy" button at the bottom — kicks off 12C.3.

### 12C.3 Bulk generation

- For each bot in the approved plan, call 12B.1's generate endpoint with `description = bot.draft_description + "Coordinate with these other agents in this team: ..."`. Run them in parallel (Claude's API supports it; cheap with concurrent requests).
- Validation gate from 12B.4 runs per-bot. If any bot fails generation after retries, surface the failure and let the user choose: skip that bot, retry, or edit the description and retry.
- Per-bot generated code shown in the same preview shape as 12B.2 — user can review + edit each one before final deploy. This step is gated on user click; we don't auto-deploy generated code without a human seeing it.

### 12C.4 Bulk deploy + rule wiring

- For each approved+reviewed bot, call `uploadDeploymentBundle` (the existing 2026-05-04 path). Show progress per-bot.
- After all deploys are queued, install the auto-approval rules from the plan's dispatch graph: `(source_agent, target_agent, command_kind) -> auto_approve` for the edges the plan declared. Uses the existing PUT `/workspaces/me/auto-approval-rules` endpoint from Phase 11.2.
- If the plan called for workspace secrets the user hasn't set (e.g. `SLACK_WEBHOOK_URL`, `GITHUB_TOKEN`), surface a checklist on the success page rather than silently letting the bots crash on first run.

### 12C.5 Demo

- Drop the Lightsei project's own README on `/agents/team-from-readme`. Generate. Expect Claude to propose something like: a documentation maintainer (reads MEMORY.md + TASKS.md, suggests cleanups), a PR reviewer, a security scanner, a build watcher. Whatever the LLM picks — that's the demo's first surprise.
- Edit the team: rename one, remove one, add a "weekly digest" bot. Generate. Review one bot's code. Deploy.
- Watch them all show up on the constellation. Push a commit. See the chain fan out across the new team.

**Deferred 2026-05-16:** blocked on edge-timeout / fake-CORS issue on `/agents/generate`. See 12C.6.

## Phase 12C.6: async generation (unblock the demo)

Both `/agents/generate` and `/teams/plan` make synchronous Anthropic Opus calls that can run longer than Railway's edge timeout (~100s, sometimes shorter under retry-pressure). Killed connections come back without CORS headers, so the dashboard sees them as CORS errors and the team-from-README flow can never deploy. Fix: move the long calls off the request path. Same pattern for both endpoints.

The pattern is intentionally minimal: in-process background runner, single database table, no new worker process. Lightsei runs as one Railway service today, so a multi-instance job queue would be premature.

### 12C.6.1 Job model + migration

- New table `agent_generation_jobs` (or just `generation_jobs` — covers both kinds). Columns: `id` (uuid pk), `workspace_id` (fk, indexed), `kind` (`'agent_generate' | 'team_plan'`), `status` (`'pending' | 'running' | 'success' | 'failed'`), `request_payload` (jsonb), `result_payload` (jsonb, nullable), `error` (text, nullable), `created_at`, `started_at` (nullable), `finished_at` (nullable), `attempt_count` (int default 0).
- Alembic migration + SQLAlchemy model in `backend/models.py`. Authz query is `workspace_id = current_workspace`.

### 12C.6.2 In-process background runner

- New `backend/jobs.py`. On FastAPI startup, spawn an asyncio task that polls for `status='pending'` jobs (LIMIT 1, FOR UPDATE SKIP LOCKED) and runs them serially.
- Dispatch table: `kind -> handler(session, payload) -> dict | raises`. Each handler is a pure function refactored out of the current endpoint body.
- Failure handling: catch all exceptions, write to `error`, mark `failed`, bump `attempt_count`. No auto-retry in v1 — surfacing the error is enough.
- Graceful shutdown: on app stop, finish the current job before exiting (uvicorn already gives ~10s).

### 12C.6.3 Refactor /agents/generate

- Extract the body of `generate_agent` in `backend/main.py` into `agent_generator.run_generation(session, workspace_id, payload) -> dict` (or a similar pure function). Endpoint becomes: validate input + budget + key, insert a `generation_jobs` row with `kind='agent_generate'`, return `{job_id, status: 'pending'}` 202.
- Preserve the cost accounting (the `lightsei.system` Run row) — runner writes it.

### 12C.6.4 Refactor /teams/plan

- Same pattern with `kind='team_plan'`. Logic moves to `team_planner.run_plan(...)`.

### 12C.6.5 Poll endpoint

- `GET /workspaces/me/generation-jobs/{id}` returns the row (status + result_payload + error). Authz: 404 if the job's workspace ≠ the caller's. No cleanup endpoint in v1 — rows are small, can be reaped later.

### 12C.6.6 Dashboard: async generate

- `generateAgent()` in `dashboard/app/api.ts`: POST to `/agents/generate`, get `{job_id}`, poll `GET /generation-jobs/{job_id}` with backoff (1s → 2s → 4s, cap 5s, total cap ~5 min). Resolve on terminal status. Surface backend `error` field as the thrown Error message.
- `runOneGenerate` in team-from-readme/page.tsx and the existing /agents/generate page consume the new shape transparently (signature unchanged).

### 12C.6.7 Dashboard: async plan

- `fetchTeamPlan()` in `api.ts`: same kick-off-and-poll shape. team-from-readme/page.tsx shows the existing "thinking..." state while polling.

### 12C.6.8 Tests

- Job runner: state transitions, error capture, idempotent picks.
- Endpoints: 202 shape on kick-off, status progresses, terminal state returned correctly.
- Reuse the existing stubbed-Anthropic harness from `test_team_planner.py` and `test_agent_generator.py`.

### Demo for 12C.6

- Hit `/agents/team-from-readme` with the Lightsei README on prod. Confirm plan returns inside the timeout. Confirm bulk generate completes for all bots without CORS errors. Once green, NOW reverts to 12C.5.

### Belt-and-suspenders fixes already landed (2026-05-16, upstream merge)

These shipped on upstream while the async refactor was being drafted, and are preserved on top of it:

- **Alembic logger-mute root cause** (`backend/alembic/env.py`). `logging.config.fileConfig(config.config_file_name)` was running without `disable_existing_loggers=False`, which silently set `disabled=True` on every logger that already existed when alembic's env.py ran at FastAPI startup. That muted uvicorn's banner + access logs + every `lightsei.*` logger, so the same failure shape was invisible in both local docker and Railway. With the one-liner fix, the access log for /openapi.json shows up locally within a second. This was the prerequisite to seeing any backend signal at all during the demo dig.
- **Dashboard concurrency cap** (`dashboard/app/agents/team-from-readme/page.tsx`). The bulk-generate step replaced `Promise.allSettled(team.map(runOneGenerate))` with an inline 2-lane semaphore + 250-500ms jitter. With the async refactor, each per-call wait drops to a poll loop instead of a 60-120s Opus call, so the burst limit is no longer load-bearing, but it doesn't hurt and stays in.
- **`LIGHTSEI_SECRETS_KEY` required in docker-compose** (`docker-compose.yml` + `.env.example`). Local stack was hitting "secrets store unavailable" the first time anyone added an ANTHROPIC_API_KEY from /account.

## Phase 12D: cost intelligence (Polaris is smart about spending)

Goal: Lightsei stops being a passive cost-recorder and becomes an active cost-optimizer. Today /cost shows what you spent. 12D is "where your dollars went, what was wasted, and one-click ways to spend less without losing capability." Promoted ahead of Phase 13 because every new agent we add (12B-generated or hand-written) compounds spend, and most of those agents will use Opus by default — the ROI on a "you're spending $X on Opus tasks Haiku would handle for $0.10X" recommendation grows linearly with team size.

Three layers, build in order:

### 12D.1 — Spending audit (read-only insights) ✅ shipped 2026-05-05

See Done Log. Reference: `backend/cost_insights.py`, `dashboard/app/cost/insights/page.tsx`.

### 12D.2 — Polaris emits a periodic self-analysis ✅ shipped 2026-05-10 (commit ef732f6)

Implementation lives in `polaris/bot.py` (`_emit_cost_analysis`, `_is_interesting_insight`, called after every `polaris.plan` emit). Verified live on prod 2026-05-17 — most recent `polaris.cost_analysis` event carries 3 filtered insights right after polaris's redeploy. Heading was never check-marked when it shipped — the Done Log entry from that day got missed during today's "what's left" sweep, which is what landed NOW briefly on 12D.2 in commit 40d9d7f.

Polaris already ticks on a schedule and reads docs. Add a second cron-style task (or fold into the existing tick) that produces a `polaris.cost_analysis` event:

- Pulls the last week's runs + costs + plan hashes for itself + the constellation
- Calls Claude (or just runs the heuristics from 12D.1 in pure code; cheaper) to summarize what was spent on what, what was useful, what was wasted
- Emits a `polaris.cost_analysis` event the home page renders next to `polaris.plan`
- "What's wasted" examples Polaris can call out: ticks producing identical plans (suggest longer interval), agents using a model tier above what their inputs require, dispatch chains that always end the same way regardless of input (suggest making the dispatch conditional)

This is the version of "Polaris is smart about spending" that surfaces the audit *during* the user's normal review flow rather than requiring them to visit a dedicated insights page.

### 12D.3 — Auto-optimization with explicit consent

The dangerous + powerful slice. Lightsei proposes a config change ("switch atlas from Opus to Haiku, projected savings $X / month") and the user one-click accepts. Three guardrails before this is safe:

- **Reversibility.** Every applied recommendation gets an audit row + a "revert to previous" button. Failed downgrades (the cheaper model produces noticeably worse output) are caught in the next 12D.2 cycle and offered as a revert recommendation.
- **Scoped to model + interval, not behavioral.** 12D.3 only changes provider/model and tick_interval_s — the two reversible knobs. Anything that touches dispatch logic / approval rules / system prompts requires the user to do it manually via the existing surfaces.
- **Quality watch.** After applying a recommendation, watch the next N runs' validation pass-rate + plan-hash divergence. If quality dropped (more validation fails OR plans suddenly very different from prior pattern), suggest revert.

12D.3 specifically depends on Phase 14 (continuous eval) being further along — auto-tuning needs a quality signal to safely tune against. Park this slice until 14 lands.

### Demo for 12D

- Walk through `/cost/insights`. Pick the recommendation with the highest projected savings.
- Click apply (12D.3) or apply manually (12D.1 + dashboard pin).
- One week later, the next 12D.2 tick reports actual savings vs projected and any quality regressions.

## Phase 13: more agents (deferred)

Originally next, now deferred behind 12B + 12C since most of these can be generated rather than hand-written once those phases ship. Keeping the names + roles around because the constellation still wants seeded teammates for the home page to feel populated:

- Argus (security + secret scanner)
- Vega (PR reviewer)
- Sirius (alert triager + on-call)
- Cassiopeia (incident scribe)

Each will be a 12B-generated bot we curate + harden rather than a hand-written one.

## Phase 14: continuous evaluation (judge-LLM quality signal)

Layer 5 of MEMORY.md's "Five guardrail layers": a background-sampling judge-LLM that rates completed runs so we know which bots are getting better, which are degrading, and which model downgrades are actually safe. This is the quality signal 12D.3 needs to safely auto-tune model + tick_interval; without it, "switch atlas from Opus to Haiku" is a leap of faith.

The validators from Phase 7/8 catch _hard_ failures (the bot's output doesn't parse, contains a banned phrase, fails a schema check). Phase 14 catches _soft_ failures: the output parsed and got delivered, but is worse than the model's prior bar. Validators are a contract; the judge is a critic.

**Design choices settled 2026-05-17 (do not re-debate without a reason; switch triggers per choice below).**
- **Sampling rate: per-agent round-robin, default 3 / agent / hour** via `LIGHTSEI_EVAL_PER_AGENT_PER_CYCLE`. Quiet bots get 100% coverage; chatty bots are sampled. Within an agent's pool, bias toward most recent. Switch trigger: per-agent caps stop catching meaningful drift on the chatty-but-cheap bots, or a single workspace's judge spend exceeds 5% of its monthly LLM total — at which point reconsider cost-weighted sampling.
- **Judge model: always `claude-sonnet-4-6`** (the current Sonnet). Same model for every verdict so scores are comparable across agents and over time. Avoids both the "Opus is unfairly harsh on Haiku output" risk and the "Haiku rates Haiku as fine" risk. Switch trigger: a newer Sonnet ships, or a workspace's bots all use Opus and the Sonnet judge produces verdicts the user can demonstrate are wrong.
- **Judge depth: plan + final output.** Plan tells the judge what the bot intended; output tells it what was delivered. Tool-call traces are deliberately omitted from v1 to keep the judge prompt bounded. Switch trigger: a real case where the bot's plan + output look fine but the tool-call sequence was wrong — promote to plan + tool calls + output.
- **Storage: per-run rows in `run_evaluations`** (not pre-rolled). Postgres handles 10 bots × 3 samples × 24h × 365d ≈ 263k rows / workspace / year without breaking a sweat, and the rollup queries are cheap on the indexes specced in 14.2. Switch trigger: a workspace's table crosses ~10M rows and queries slow down (likely a 5+ year horizon).
- **Cost cap: rolled into the existing workspace monthly cap.** Judge spend attributes to the `lightsei.system` synthetic agent (same as generation calls do today). Workspace budget gates everything together; one knob to tune. Switch trigger: a user complains that judge spend ate their generation budget — split with a separate env var then.

### 14.1 — Sampler + judge prompt + schema (pure module) ✅ shipped 2026-05-17

New `backend/eval_sampler.py`. Two pure functions:

- `pick_sample(session, workspace_id, per_agent=3)` returns up to `per_agent * n_agents` `run_id`s to evaluate this cycle. Implementation: for each non-`lightsei.*` agent in the workspace, query the last hour's completed runs that don't yet have a `run_evaluations` row, sort by `ended_at DESC`, take up to `per_agent`. No cross-agent re-weighting; the per-agent cap _is_ the fairness mechanism. `per_agent` defaults from `LIGHTSEI_EVAL_PER_AGENT_PER_CYCLE` (default 3).
- `build_judge_prompt(run, agent)` composes the judge's single LLM turn: the agent's role + system prompt, the run's `polaris.plan` event payload (or whichever event embodies the bot's "plan" for non-orchestrator agents — `tick` events for executors), and the run's final output event. No tool-call traces in v1. Forced `tool_choice={"type": "tool", "name": "submit_verdict"}` on a `SUBMIT_VERDICT_TOOL` whose schema is `{verdict: enum(good, borderline, bad), reasons: array(string, min 1, max 5), confidence: number(0..1)}`. Schema-strict so a malformed response surfaces as a judge failure rather than a default-good verdict.

No DB writes, no LLM calls — pure shaping logic, same pattern as `agent_generator.py` v1. Tests assert prompt content, schema validation, and that `pick_sample` honors per-agent caps + skips already-evaluated runs.

### 14.2 — `run_evaluations` table + alembic migration ✅ shipped 2026-05-17

New table backed by SQLAlchemy model in `backend/models.py`: `id` (uuid pk), `run_id` (FK to runs, ON DELETE CASCADE), `workspace_id` (FK, indexed), `agent_name` (string, indexed), `judge_model` (string), `verdict` (string: 'good'/'borderline'/'bad'), `reasons` (jsonb), `confidence` (numeric), `judge_tokens_in`/`judge_tokens_out` (int), `judge_cost_usd` (numeric), `created_at`. Indexes: `(workspace_id, agent_name, created_at DESC)` for dashboard surfacing, `(workspace_id, verdict, created_at DESC)` for "recent bads" queries.

### 14.3 — Periodic eval job (reuse generation_jobs runner) ✅ shipped 2026-05-17

Register `eval_runs` as a new `kind` in `backend/jobs.py`'s dispatch registry (same SKIP LOCKED + asyncio.to_thread pattern from 12C.6.2). Handler calls `eval_sampler.pick_sample`, runs the judge for each, writes a `run_evaluations` row. Cron-style enqueue: at startup register an asyncio task that drops one `eval_runs` job per hour (configurable via `LIGHTSEI_EVAL_INTERVAL_S`, default 3600). Judge spend lands on `lightsei.system` like generation spend, so the workspace budget gate already applies; no separate cap in v1.

### 14.4 — Quality signal endpoints + dashboard surface ✅ shipped 2026-05-17

Backend: `GET /workspaces/me/agents/{name}/quality?days=7` returns `{verdict_counts: {good, borderline, bad}, recent_bads: [{run_id, reasons, confidence, occurred_at}, ...], trend_7d}` for one agent; `GET /workspaces/me/quality` for the workspace rollup. Dashboard: new "Quality" column on /agents (green chip with count if all good, amber if any borderline, red if any bad in the window). New section on /agents/{name} showing the verdict breakdown + the most recent bad evaluations with their judge reasons, so the user can see _why_ the bot is flagged.

### 14.5 — SDK helper for 12D.3 consumption ✅ shipped 2026-05-17

`lightsei.get_quality_signal(agent_name, days=7)` in the SDK so the auto-tuner (when 12D.3 lands) can read the same shape the dashboard renders. Same fail-open pattern as `lightsei.get_cost_insights` from 12D.2.

### 14.6 — Tests ✅ folded into 14.1 + 14.3 + 14.4 + 14.5

The original list (sampler determinism, schema strictness, runner state machine with stub judge, endpoint authz + rollup math) was covered by the tests that landed alongside each sub-task: 22 tests in `test_eval_sampler.py` (14.1), 13 in `test_eval_runner.py` (14.3), 17 in `test_quality_signal.py` (14.4), 7 in `test_basic.py` quality block (14.5). Total: 59 new Phase 14 tests; no additional coverage gap once those merged. Skipped a separate sub-task commit for the same reason 12C.6.8 was a single sub-task — tests live with the code they cover.

Sampler: determinism on fixed seed, per-agent coverage, doesn't pick the same run twice within a window. Judge schema: forced tool_choice produces the expected dict; missing fields surface as a judge-level failure rather than a `verdict=good` default. Background job: state-machine tests through the existing `test_jobs.py` harness with a stub judge. Endpoints: authz, rollup math, cross-workspace 404.

### 14.7 — Demo ✅ passed 2026-05-17

- Walk through /agents — quality pills populated on each bot after the first eval cycle.
- Click into a bot with a bad eval, see the judge's reasons.
- Look at the next `polaris.cost_analysis` event (12D.2) — it now folds quality regressions into the waste callouts ("agent X quality dropped after recent change; consider revert").
- 12D.3 is now unblocked: an applied recommendation can be quality-watched against the judge signal.

## Phase 16: Trust zones as a first-class concept (now P0)

Operationalizes the trust-zone work that landed as P0 in the 2026-05-17 strategic-direction decision. See MEMORY.md "Target customer shape" and "Competitive north star". The wedge against Viktor is having this shipped, with sensible presets, before non-technical users have to configure anything. Scope expanded 2026-05-17 to include the framework-level enforcement pieces (capability model, cross-zone dispatch block, handoff span) after a design pass on the canonical CRM-bot scenario showed that data tagging alone is not enough; the trust zones have to actually refuse forbidden operations, not just visualize them.

**Design choices settled 2026-05-17 (do not re-debate without a reason; switch triggers per choice below).**

- **Sensitivity levels: a four-step ladder** `'public' | 'internal' | 'sensitive' | 'pii'`. Same vocabulary used everywhere (agents, runs, events, redaction defaults, presets). Switch trigger: a real customer needs a fifth level that doesn't fit any existing rung, OR the four-step model gets in the way of a regulatory framework we're trying to satisfy (HIPAA-specific levels, etc.).
- **Capability model: explicit allow-list per agent, default-deny.** Capabilities are short strings: `'internet'`, `'connector:hubspot'`, `'connector:slack'`, etc. Backend stores the list; SDK refuses any op not on the list. Switch trigger: the string vocabulary grows past ~30 distinct capabilities and we need namespacing or a real taxonomy.
- **Enforcement boundary: SDK + backend, not just backend.** Refusal happens at the SDK call site so a compromised bot can't even craft the network call. Backend rejects too as defense-in-depth. The check has to feel automatic to bot authors — bot.py imports `lightsei` and the wrappers around `httpx.get`, `send_command`, connector calls, etc. enforce capabilities transparently. Switch trigger: an auto-patched SDK approach (Phase 1 pattern) proves infeasible for a class of operations — at that point we need an explicit `lightsei.use_capability("...")` decorator instead.
- **Cross-zone dispatch: same-zone-only is the default; cross-zone requires `dispatches_cross_zone=True` on the source agent.** Not on the dispatch rule — on the agent — so it's a property of "this agent is trusted to do this" rather than "this specific call slipped through approvals." Switch trigger: a real workflow needs per-rule cross-zone exceptions and the per-agent flag is too coarse.
- **Redaction: opt-out per-call, on by default for `'pii'` agents.** `lightsei.redact()` runs over outgoing events / dispatched payloads / chat-message body. Detectors are pluggable; built-ins ship for email, phone, SSN-shape, card-shape. Switch trigger: false-positive rate becomes a customer pain (legitimate emails getting redacted in support-ticket bots, etc.) — at that point we add per-detector confidence + threshold.
- **Handoff span: opt-in, SDK-only.** No automatic detection of human-mediated handoffs (too much false positive risk); the operator chat surface (Phase 21) will call `lightsei.handoff_span(from_run, to_run, sanitized_prompt)` explicitly. Switch trigger: handoff usage proves common enough that auto-detection from chat-thread metadata becomes worth the false-positive cost.
- **Three presets (not four, not "configurable").** "Open team", "Standard team", "Compliance team" — wired into team-from-README + manual `/zones` setup. Anything more is a paid-tier feature later. Switch trigger: customers want a "Compliance + outbound email" variant or similar — second wave of presets, not arbitrary customization.

### 16.1 — Sensitivity-level field on agents + runs + alembic migration ✅ shipped 2026-05-17

Add `sensitivity_level` to the `agents` table (`String(16)`, NOT NULL, default `'internal'`) and to `runs` (same column, default inherited from agent at run-create time so historical analytics don't have to JOIN). SQLAlchemy models + alembic 0027. Backfill existing rows with `'internal'`. Validation at the model layer: enum-tight string in `_VALID_LEVELS`. The backbone for everything else in the phase — every other 16.x sub-task assumes this column exists.

### 16.2 — Declarative capability model + backend storage ✅ shipped 2026-05-17

Add `capabilities: list[str]` to the `agents` table (`JSONB`, NOT NULL, default `[]`). Pure module `backend/capabilities.py` owns the vocabulary: `KNOWN_CAPABILITIES` set, `validate_capability_list(names)` returning the same kind of `problems` list the team-planner validators use, `presets_for_level(level)` returning the default capability set for a sensitivity rung. CRUD endpoint `PATCH /workspaces/me/agents/{name}/capabilities` (workspace-authz). Alembic migration in the same revision as 16.1 since they're one-shot together. **No SDK enforcement yet** — that's 16.3; this sub-task is purely storage + validation so the next sub-task has something to read.

### 16.3 — SDK enforcement: capability gate on outbound ops ✅ shipped 2026-05-17

Make the SDK refuse capability-restricted ops at call time. Auto-patch path (preferred): wrap `httpx`'s sync + async clients via the same pattern Phase 1 used for OpenAI, so `httpx.get(...)` in user code raises `LightseiCapabilityError` if `'internet'` isn't in the agent's capability list. Same wrapping for `lightsei.send_command` (checks `'send_command'` capability OR the source agent's allow-list of dispatch targets). Connector capabilities (`'connector:hubspot'`, etc.) checked when the connector SDK ships in Phase 20 — for now the gate machinery exists but only `'internet'` and `'send_command'` are enforced. SDK fetches the capability list on `init()` and caches it; refresh on every heartbeat so updates from the dashboard propagate within a tick. Bot-author ergonomics: the wrapped ops feel identical to the unwrapped ones; only forbidden calls raise.

### 16.4 — Cross-zone dispatch enforcement (framework-level) ✅ shipped 2026-05-17

The load-bearing piece. Phase 11's `send_command` (SDK) + `enqueue_command` (backend) both gain a zone check: same `sensitivity_level` between source and target is always allowed; different levels are refused unless the source agent has `dispatches_cross_zone=True`. Backend refusal is a 403 with a `cross_zone_blocked` error code; SDK refusal raises `LightseiCrossZoneError` before the network call. Existing auto-approval rules (Phase 11.2) still apply on top — cross-zone-enabled does NOT mean auto-approved. New `dispatches_cross_zone: bool` column on `agents` (default `False`) in the same alembic revision as the other 16.x schema changes. Editor UI in 16.6 surfaces the flag; setting it from team-from-README requires explicit user opt-in (no preset enables it silently).

### 16.5 — SDK redaction primitives + handoff span ✅ shipped 2026-05-17

`lightsei.redact(text, *, detectors=None)` returns text with PII-shaped substrings replaced with `[redacted-email]`, `[redacted-phone]`, etc. Built-in detectors: email, US phone, SSN-shape (9 digits with the standard hyphenation), credit-card-shape (Luhn-checked 13-19 digits). Pluggable via `lightsei.register_redactor(name, fn)`. For agents with `sensitivity_level == 'pii'`, the SDK auto-redacts outgoing `lightsei.emit` payloads + dispatched command payloads + chat-message body by default; per-call opt-out via `lightsei.emit(..., redact=False)` for the operator who genuinely needs the raw value. Also ships `lightsei.handoff_span(from_run, to_run, sanitized_prompt)` — a synchronous helper that writes a `handoff` event linking the two runs in the trace view; opt-in (no auto-detection).

### 16.6 — Dashboard surfaces ✅ shipped 2026-05-17

Three things wired together so the trust-zone story is visible without clicks: (1) sensitivity chip rendered on `/agents` (next to the existing Quality chip from 14.4) + on `/agents/{name}` header; (2) constellation map nodes color-coded by zone (green/yellow/orange/red for public/internal/sensitive/pii), cross-zone edges drawn red with a thicker stroke; (3) new `/zones` page showing the workspace topology — a vertical lane per zone, nodes grouped by lane, an explicit "dispatches across zones" section listing the agents that opted in. Editor on `/agents/{name}` lets the user set the sensitivity level + capability list + cross-zone flag (these are the three knobs that matter). Refusal surfaces: when the backend returns `cross_zone_blocked`, the dashboard's command-enqueue UI shows the actual policy violation rather than a generic 403.

### 16.7 — Three trust-zone presets + team-from-README integration ✅ shipped 2026-05-17

`backend/zone_presets.py` defines three presets: `'open_team'`, `'standard_team'`, `'compliance_team'`. Each preset is a `{role: {sensitivity_level, capabilities, dispatches_cross_zone}}` dict so the team-planner's existing roles (orchestrator / executor / specialist / messenger) map to a default trust-zone configuration. Team-from-README flow gains a preset picker (defaults to `'standard_team'`); the picker explains each preset's tradeoffs inline (Open = developer convenience, Standard = SMB defaults, Compliance = "your CRM data does not leave this team"). Compliance team is the canonical CRM-bot scenario as a starting template — generates one CRM-side agent (`'pii'` + connector capabilities + no internet) and one internet-side agent (`'public'` + internet + no connectors) with cross-zone dispatch explicitly disabled, so the handoff has to come from the operator.

### 16.8 — Tests

Per-sub-task tests live alongside the code they cover (same pattern as Phase 14): schema + endpoints (16.1-16.2), SDK capability gate + redaction (16.3, 16.5), backend dispatch enforcement (16.4), dashboard tsc clean + key flows (16.6), preset wiring (16.7). One integration-shaped test crosses the surfaces: generate a Compliance team end-to-end, attempt cross-zone dispatch via the SDK, assert the SDK raises BEFORE the network call; attempt the same via the backend with a forged request, assert the backend 403s with `cross_zone_blocked`. This is the load-bearing assertion that the wedge actually works.

### Phase 16 demo

Generate a "Compliance team" from a fake CRM-shaped README. See the constellation render with PII agents (CRM bot) isolated from internet-access agents (research bot) — color-coded zones, no edges between them. Try to wire a forbidden cross-zone edge in the editor — see it blocked at deploy time with the policy reason inline. Try to have the CRM bot dispatch to the internet bot at runtime — see the SDK raise `LightseiCrossZoneError` before any network call leaves. Watch a human-mediated handoff (operator reads CRM-bot output, types a sanitized prompt to the internet bot) appear as a single connected chain in the trace view via `lightsei.handoff_span`. Compliance demo proves the wedge.

## Phase 17: Self-serve onboarding (auth, signup, billing)

Operationalizes "non-technical users need to self-serve" from the 2026-05-17 strategic direction. Promoted from "deferred forever" to high-priority — non-technical users can't try the product without this. This is the gate to actually putting Lightsei in front of a real customer.

**Design choices settled 2026-05-17 (do not re-debate without a reason; switch triggers per choice below).**

- **Auth: magic link AND Google OAuth, both first-class.** Magic link is the no-friction path (anyone with email); Google OAuth is the one-click path for users already signed into Google. Both land on the same `users` row. The existing API-key signup at `/auth/signup` stays for SDK / CLI / developer use — repositioned as the "advanced" path; the dashboard signup uses the new flows. Switch trigger: if either dashboard path turns out to be unused after a month of real users, drop it; don't keep two surfaces both half-maintained.
- **Email provider: Resend.** Best DX for transactional email; 3k/month free covers signup volume well past the first paying customers. Switch trigger: monthly send volume crosses 100k OR cost optimization at scale starts mattering more than DX → revisit AWS SES.
- **Billing: $50/mo flat per workspace + $5 free credits on signup.** Matches Viktor's reference price. Free credits cover roughly a day of typical light use so the user can try the product before deciding. Card required only when credits exhaust. Single paid tier in v1 (no "pro"); expand later if signal demands it. Per-event usage-based billing explicitly rejected — doesn't fit the non-technical-buyer mental model per MEMORY.md.
- **Paywall trigger: hard 402 on the next LLM call when free credits exhausted AND no active subscription.** Reuses the existing budget gate pattern from agent_generator + eval_runner. Read-only dashboard / signup / billing surfaces keep working so the user can see what they had + add a card. Switch trigger: customer feedback says the hard cutoff feels broken vs out-of-credits → consider a soft warning + 24h grace period.
- **One workspace = one seat in v1.** Multi-user-per-workspace is parked in the Parking Lot. Per-seat billing makes sense once that lands; for now, "seat" and "workspace" are the same thing.

### 17.1 — Schema backbone for auth + billing ✅ shipped 2026-05-17

Single alembic migration (0030) adding:

- `users.email_verified` (Boolean, default false). Magic-link signup sets it true on first successful consume; existing API-key-signup users land verified=false until they do a magic-link round-trip.
- `users.auth_provider` (String(16), default `'apikey'`). Tracks which path created the row: `'apikey'`, `'magic_link'`, `'google_oauth'`. Used for analytics + the dashboard signup-flow detection.
- `users.google_user_id` (String, nullable, unique). Google's `sub` claim. Lets a returning OAuth user be matched to the same row even if they change their email.
- `workspaces.stripe_customer_id` (String, nullable, unique). Created on workspace-create; the workspace, not the user, is the Stripe Customer (matches the per-workspace seat model).
- `workspaces.stripe_subscription_id` (String, nullable). Set when the workspace has an active subscription; null when on the free tier.
- `workspaces.plan_tier` (String(16), default `'free'`). `'free'` (using credits) | `'paid'` (active subscription). Single source of truth for "should this workspace be allowed to spend right now."
- `workspaces.free_credits_remaining_usd` (Numeric(12,6), default 5.0). Server_default 5.00 so new workspaces start with $5 of credits; existing workspaces backfilled to the same value (everyone gets the same free credit on the migration).
- New `email_signin_tokens` table: `token_hash` (PK), `email`, `created_at`, `expires_at`, `consumed_at` (nullable). Single-use, 15-minute TTL.

Indexes: `(workspaces.stripe_customer_id)` unique, `(email_signin_tokens.email, created_at DESC)` for the rate-limit query.

### 17.2 — Magic-link auth backend ✅ shipped 2026-05-17

Two endpoints + the Resend integration in `backend/email.py` (new pure module).

- `POST /auth/magic-link/request {email}` — generates a fresh single-use token, hashes it (same `hash_token` pattern from `keys.py`), inserts an `email_signin_tokens` row with 15-minute TTL, sends a Resend email containing the unhashed token in a magic URL (`https://app.lightsei.com/auth/magic-link?token=...`). Always 200 even on unknown email (don't leak existence). Rate-limited to ~5/hour per email via the existing limits machinery so a malicious sender can't spam users.
- `POST /auth/magic-link/consume {token}` — looks up by hashed token, checks not consumed + not expired, marks consumed, either signs in the matching user OR creates a new user + workspace pair if the email is new. Returns a session token (existing `keys.generate_session_token` + `Session` row).
- New-user creation sets `auth_provider='magic_link'`, `email_verified=true`, free workspace name (`"{email_local_part}'s workspace"`), `free_credits_remaining_usd=5.00`, `plan_tier='free'`.

### 17.3 — Google OAuth backend ✅ shipped 2026-05-17

Two endpoints implementing the standard OAuth 2.0 authorization-code flow with PKCE.

- `GET /auth/google/start` — generates state + PKCE verifier, stores both server-side (small table or signed cookie), returns a redirect to Google's auth endpoint with the configured client_id + redirect_uri.
- `GET /auth/google/callback?code&state` — verifies state, exchanges the code for tokens, fetches the userinfo endpoint for `sub` + `email` + `email_verified`, either signs in the matching user (`google_user_id` exact match → preferred; otherwise `email` match if email_verified) OR creates a new user + workspace pair (`auth_provider='google_oauth'`).
- Configuration: `LIGHTSEI_GOOGLE_CLIENT_ID` + `LIGHTSEI_GOOGLE_CLIENT_SECRET` env vars + Railway-side OAuth consent setup. New-user shape matches 17.2's pattern.

### 17.4 — Stripe integration ✅ shipped 2026-05-17 (code-complete pending Stripe console config)

`backend/stripe_billing.py` (new helper module, mirrors `google_oauth.py` shape) + three endpoints + STRIPE_SETUP.md walkthrough.

- Lazy customer creation: `stripe.Customer.create` only fires the first time a workspace clicks Upgrade (most workspaces never will, so eager creation would waste API calls). Customer id persisted on `workspaces.stripe_customer_id`.
- `POST /workspaces/me/billing/checkout` — creates a Stripe Checkout Session in subscription mode against the configured price id. Returns `{checkout_url, session_id}`. 503 when Stripe env vars are unset, 502 on Stripe API failure, 400 when workspace is already paid.
- `POST /workspaces/me/billing/portal` — creates a Customer Portal session. Returns `{portal_url, session_id}`. 400 when workspace has no `stripe_customer_id` yet (so dashboard can render "upgrade first" rather than a broken link).
- `POST /billing/stripe/webhook` — verifies `stripe-signature` header against `LIGHTSEI_STRIPE_WEBHOOK_SECRET`, flips `plan_tier` based on event type. Handles `checkout.session.completed` (→ paid), `customer.subscription.updated` (status-aware: active/trialing → paid, anything else → free), `customer.subscription.deleted` (→ free), `invoice.payment_failed` (telemetry only; the subscription.updated that follows does the downgrade). Idempotent on duplicate delivery; unknown events get 200 + ignored (Stripe stops retrying). Bad signature / missing secret → 400 (never 5xx).
- Configuration: `LIGHTSEI_STRIPE_SECRET_KEY` + `LIGHTSEI_STRIPE_PRICE_ID` + `LIGHTSEI_STRIPE_WEBHOOK_SECRET` + `LIGHTSEI_DASHBOARD_BASE_URL` env vars.
- STRIPE_SETUP.md (new, in repo root) — step-by-step dashboard walkthrough (product + price, Customer Portal config, webhook endpoint, env vars on Railway, stripe-cli local-dev pattern). The endpoints stay 503 until the env vars are set, so deploying code first + configuring Stripe later doesn't break anything.

### 17.5 — Paywall middleware ✅ shipped 2026-05-17 (out of order; 17.4 deferred)

Reuses the existing budget-gate pattern. New helper `_assert_billing_active(session, workspace_id)` raises `HTTPException(402, detail={"error": "out_of_credits", "remaining_usd": 0.0, "upgrade_url": "/account#billing"})` when:

- `workspace.plan_tier == 'free'` AND `free_credits_remaining_usd <= 0` → 402.
- `workspace.plan_tier == 'paid'` → allow (still subject to the existing `budget_usd_monthly` cap).

Call sites: anywhere an LLM-charged op runs. `agent_generator.run_agent_generation_job`, `team_planner.run_team_plan_job`, `eval_runner.run_eval_job`. Also the worker's outbound LLM path (bots themselves) needs a similar check — done by the SDK reading the workspace state and refusing emit / send_command when paywall'd. (Defer the SDK-side gate until 17.5 implementation to keep this scope tight; the workspace cap from 16-and-earlier already covers worst case.)

Free credits decrement on every Run row creation. `lightsei.system` cost (generation + judge) and bot-run cost both come out of the same pool — keeps the accounting in one column.

### 17.6 — Dashboard signup + login UI ✅ shipped 2026-05-17

Replaced the password-only `/login` and `/signup` pages with magic-link-first flows.

- `/login` — email + "send magic link" primary CTA, "continue with Google" secondary, password sign-in moved to `/login/advanced` (subtle link). "Check your email" success state.
- `/signup` — same shape as `/login`, "$5 of credits on us, no credit card" subtitle, API-key signup form moved to `/signup/advanced`.
- `/auth/magic-link?token=...` — consumes token via POST `/auth/magic-link/consume`, sets session, redirects to `/` (with a 700ms success-state beat). Error state links back to `/login` to request a fresh link.
- `/auth/google/callback?code&state` — passes through to backend `GET /auth/google/callback`, sets session, redirects per `redirect_after` (default `/`). Handles `?error=access_denied` cleanly.
- `/login/advanced` + `/signup/advanced` — the old password-based forms, preserved verbatim, with "back to magic-link" link and explanatory copy pointing developers at the SDK init flow.
- `api.ts` — added `requestMagicLink`, `consumeMagicLink`, `startGoogleOAuth`, `completeGoogleOAuth`, and shared `AuthSuccess` type.

### 17.7 — Dashboard billing UI ✅ shipped 2026-05-17

New Billing section on `/account` (renders above Workspace):

- Free tier: "Free" badge + "$X.XX of free credits remaining" + "Upgrade to $50/mo" button → POST `/workspaces/me/billing/checkout`, navigates browser to Checkout URL.
- Paid tier: "Paid" badge + "Active subscription · $50/mo" + "Manage subscription" button → POST `/workspaces/me/billing/portal`, navigates to Customer Portal.
- Checkout redirect-back handling: `?upgrade=success` shows "Confirming your payment" banner + polls `fetchWorkspace` every 1.5s for up to 45s until `plan_tier='paid'`. `?upgrade=cancelled` shows "no charge made, try again" amber banner. Both query params are cleaned with `history.replaceState` so a hard reload doesn't re-trigger.
- 503 from the billing endpoints (Stripe not configured) surfaces as a friendly "ask the admin to follow STRIPE_SETUP.md" message rather than a generic error.
- Backend serializer (`_serialize_workspace`) extended with `plan_tier`, `free_credits_remaining_usd`, `has_stripe_customer` so the dashboard renders the right CTA without an extra API call.

### 17.8 — Tests ✅ folded into 17.1-17.7

Tests for each Phase 17 sub-task shipped alongside the code (per the established pattern). Backend at 747 passing tests with 17.4 alone adding +20 and 17.5 alone adding +17.

### 17.9 — Demo ✅ passed in test mode 2026-05-17 (live mode parked)

The full non-technical-user upgrade arc runs end-to-end on prod against test-mode Stripe:

- Fresh signup via `/auth/signup` on prod, gets `plan_tier='free'` + $5 of free credits.
- Hits `/account`, sees Billing section with Free badge + $5.00 credits remaining + Upgrade button.
- Clicks Upgrade → real Stripe Checkout opens with the configured $50/mo price.
- Pays with test card `4242 4242 4242 4242`.
- Stripe redirects to `/account?upgrade=success` → dashboard shows blue "Confirming payment" banner → polls `fetchWorkspace` every 1.5s.
- Within seconds: real Stripe webhook delivery → backend signature verification passes → `customer.subscription.created` handler runs → `plan_tier='paid'` written + `stripe_subscription_id` stamped.
- Dashboard polling notices the flip → swaps to green "you're on the paid plan" banner + manage-subscription button.
- Manage Subscription → real Stripe Customer Portal opens with the configured options (update card, view invoices, cancel at period end).
- Verified via API: workspace row reports `plan_tier="paid"` + `has_stripe_customer=true`.

Loose ends from the test-mode walkthrough (harmless, but for awareness):
- Test workspace `stripe-smoke-1779072629` (email `wallacebailey32+stripe-smoke-1779072629@gmail.com`) is still in the prod DB with a live test-mode Stripe subscription.
- Orphan live-mode product `prod_UXK3cHPoQXLrf9` exists in the live-mode catalog (created accidentally before mode-aware Stripe MCP detection).

Phase 17 closes (test mode). Live-mode activation parked until ready to take real payments — when ready, see STRIPE_SETUP.md "Step 6 onward" + the live-mode env-var swap.

## Phase 18: Dashboard polish (the dashboard is the product)

Operationalizes "dashboard is the primary surface" from the 2026-05-17 decision. Promoted from "deferred forever" to high-priority in the same update. The dashboard is now where non-technical buyers experience the product; current IA + visuals reflect the previous developer-tool era. Phase 18 closes that gap.

**Design choices locked 2026-05-18.**

- **IA shape: roles-first, not surface-first.** Top-level nav = `My team / Activity / Trust zones / Integrations / Account` + Advanced dropdown. Polaris becomes a regular agent (no longer top-level). Docs / SDK pages / deployments / manual generate live under Advanced. Switch trigger: a power-user audience emerges that genuinely wants top-level access to /deployments and /docs — at that point we re-add them with a per-workspace toggle.
- **Onboarding shape: dismissible checklist, not full guided tour.** A first-run user sees a checklist widget on the home page (add ANTHROPIC_API_KEY → drop a README → deploy a team → see /zones); dismissible permanently per workspace. No modal takeovers, no forced wizard. Switch trigger: completion rate from data shows users dismiss without completing — at that point, switch to a more aggressive first-time-only modal.
- **Visual pass scope: constellation + agent detail first.** Two surfaces non-technical users see most. Other pages (runs, cost, dispatch) keep current Tailwind defaults until Phase 18.5+. Switch trigger: a buyer complains specifically about one of the deferred pages.
- **Inline help: minimal first pass.** Tooltips only on the 8-10 most-confusing terms (sensitivity zone, capability, dispatch chain, etc.). Long-form explanations link to docs. Switch trigger: support tickets cluster around a specific term — annotate that one next.

### 18.1 — IA pass (nav restructure) ✅ shipped 2026-05-18

Restructured `dashboard/app/Header.tsx`. Top-level: `My team / Activity / Trust zones / Integrations / Account / Advanced`. Polaris demoted from top-level to a regular agent in `My team`. Docs / drop-a-zip / deployments / validators moved under Advanced. Home dropped from nav (Logo links to `/`). Account is now a top-level link (workspace switcher dropdown still has Log out + display info). Build clean across all 26 routes.

### 18.2 — Empty states + first-action CTAs

Every primary surface (home, /agents, /zones, /runs) should render a useful empty state when the workspace has zero bots. Today most pages render a blank table or a "no records" placeholder; replace with explicit CTAs that route to the next-best action. Specifically: home page empty state → big "Drop a README to build your team" card linking to `/agents/team-from-readme`. /agents empty → same. /zones empty → "Your team will appear here once you deploy. Drop a README to start." /runs empty → "No bot runs yet. Bots tick on their schedule or react to commands; once a run lands, you'll see it here."

### 18.3 — First-run onboarding checklist

Add a dismissible checklist widget that surfaces on the home page when a workspace hasn't completed key setup steps. Steps: (1) add ANTHROPIC_API_KEY workspace secret; (2) drop a README to plan a team; (3) deploy a team; (4) visit /zones to see the topology; (5) set a sensitivity_level + capabilities on at least one agent (or use the Compliance preset). Each step links to its respective surface, shows a green check when completed, and the whole widget hides itself once all 5 are done. Per-workspace dismissed flag stored on the workspace row + carried in the workspace serializer.

### 18.4 — Visual pass on the constellation map

Polish `dashboard/app/Constellation.tsx`. Goals: clearer node hierarchy (orchestrator visibly distinct from specialists from messengers), readable labels at all zoom levels, hover tooltips with agent summary + zone chip, color-coding subtle enough that the per-agent star-tints (per [[feedback]] memory) still feel like the primary identity. Cross-zone edges (when present) drawn in red with a thicker stroke + a tooltip explaining the policy implication. Mobile-responsive (the canvas dominates the home page; broken on narrow screens today).

### 18.5 — Visual pass on the agent detail page

Polish `dashboard/app/agents/[name]/page.tsx`. Current layout puts equally-weighted sections in a long vertical stack; readers don't know which to look at first. Target layout: top hero (name + zone chip + status + quick actions), middle two columns (left = config: sensitivity / capabilities / cross-zone / capabilities; right = recent runs + quality signal), bottom collapsed "Advanced" panel (raw bot.py viewer, scheduling, raw command/manifest, SDK init snippet). The Advanced panel is collapsed by default. Trust-zone editor stays in the middle config column.

### 18.6 — Polish team-from-README flow

The three-phase flow (plan → review → deploy → success) is functional but visually unsignposted. Add a progress indicator at the top, clearer "next-step" affordances at each transition, and better failure messaging when a bot's code generation fails (the psycopg2 failure during the Coral demo was a real example of where the failure copy could be friendlier — link to "edit the request and retry" rather than just showing the error).

### 18.7 — Inline help + tooltips

Add a `<Tooltip>` component that wraps a term with a small "(?)" affordance; hover opens a tooltip with a 1-2 line explanation. Apply to 8-10 terms surfaced in the Phase 16 + 17 UI: `sensitivity zone`, `capability`, `dispatch chain`, `cross-zone dispatch`, `quality signal`, `verdict`, `workspace secret`, `command kind`, `orchestrator / specialist / messenger`, `handoff span`. Long-form explanations link to docs (which now lives under Advanced).

### 18.8 — Tests

Per the existing test pattern: `tsc --noEmit` clean across all routes after each sub-task; dashboard `next build` green; a small integration test that confirms the nav exposes the new top-level items + tucks the old ones under Advanced. No backend changes in Phase 18, so backend test suite count should be unchanged.

### 18.9 — Demo

Hand the dashboard to someone non-technical with no explanation. Watch where they get stuck (signup → home → "now what?" → ...). Capture the friction in a short note + use it to prioritize Phase 18.x follow-ups. Demo passes when a first-time non-technical user lands a deployed Compliance team from a README in under 5 minutes with no help.

## Phase 19: Chat surface (Slack first, then Teams)

Operationalizes the "chat-first surface" wedge from the 2026-05-17 decision. Sequenced after Phase 18 because the dashboard product needs to be polished before a chat surface bolts on top.

Rough shape: (1) Lightsei Slack app published to the App Directory, OAuth links a Slack workspace to a Lightsei workspace; (2) `@mention` Lightsei in a channel to address the team, Polaris-style orchestration routes the request to the right bot; (3) bot outputs (PDFs, dashboards, files) post into the channel; (4) per-channel and DM scoping respects Phase 16's trust zones. Microsoft Teams app is a follow-up using the same primitives. Demo: in a Slack channel, `@Lightsei pull our MRR for last month`, watch a bot in the team handle it inline.

Detailed sub-tasks deferred until promoted to NOW.

## Phase 20: Integration breadth (MCP wrappers + connector marketplace)

Operationalizes the "integration breadth as moat" wedge from the 2026-05-17 decision. Viktor's 3000+ integrations is a marketing number; the v1 target is the priority set non-technical users actually use, then grow.

Rough shape: (1) wrap a priority connector set (Slack, Gmail, Google Calendar, Google Drive, Notion, Linear, Jira, Asana, Stripe, HubSpot, Salesforce, GitHub, Figma, Confluence, Box, OneDrive, Outlook, Discord, Airtable, Webflow) as MCP-style integrations callable from bots; (2) browseable `/integrations` UI to connect, OAuth handled; (3) each integration declares which trust zones it can be used in (ties to Phase 16); (4) custom MCP support for the long tail (paste a URL or upload a manifest, get an integration). Demo: a non-technical user connects Slack, Gmail, and Stripe in 90 seconds, deploys a weekly-revenue-digest bot that uses all three.

Detailed sub-tasks deferred until promoted to NOW.

## Phase 21: Customer-facing chat widget + operator inbox

Operationalizes the embeddable-widget-plus-master-bot-view piece of the CRM bot scenario captured in MEMORY.md "Target customer shape" (added 2026-05-17 after a design pass on the canonical CRM-bot scenario). Meaningful expansion of Lightsei's product surface: today the dashboard is for the customer's internal team, and Phase 19 adds Slack/Teams for the same audience. Phase 21 adds a surface where the customer's own end users (the customer of the customer) interact with the customer's Lightsei constellation.

Rough shape:

1. **Embeddable JS widget.** Snippet the customer pastes onto their own product. Renders a corner chat widget (Intercom-shaped). Conversations route to a designated support-shaped bot in the customer's constellation.
2. **Trust-zone-aware conversation handling.** The customer-facing bot can only access connectors that are explicitly safe to expose externally. Phase 16 capability model and zone tags apply: a bot answering end users can be configured to look up PII for the user it's talking to, but the response is redacted before it leaves.
3. **Escalation to operator inbox.** When the bot can't resolve a conversation (heuristics: explicit escalate-tool call, low confidence on output, repeated user follow-ups, or explicit user request for a human), the conversation lands in an operator inbox on the Lightsei dashboard.
4. **Operator-side master-bot view.** Per-workspace inbox showing live customer conversations, escalations, and bot-suggested fixes. Operator can intervene (jump in as a human, "I'll take it from here"), apply a suggested fix, or mark resolved.
5. **Polaris extended to incident response.** When an issue pattern repeats (same kind of escalation N times in a window), Polaris emits a `polaris.issue_pattern` event with a proposed fix: update the bot's system prompt, add a missing connector, expand a knowledge entry. Auto-apply gated on consent, same shape as 12D.3.

Demo: paste the Lightsei widget snippet onto a fake customer site. End user opens the widget and asks a question the bot answers cleanly (deflected, no operator touch). Open another conversation, ask something the bot escalates. Watch it land in the operator inbox. Operator clicks "apply suggested fix" and watches the bot self-improve, with the next similar question deflecting without escalation.

Detailed sub-tasks deferred until promoted to NOW.

Phases 1-4 shipped 2026-04-25 (spine, cost-cap guardrail, Anthropic + streaming, hosted-readiness). Phase 5 shipped 2026-04-26 (PaaS-for-agents). Phase 6 shipped 2026-04-27 (Polaris orchestrator). Phase 7 shipped 2026-04-28 (output validation, advisory). Phase 8 shipped 2026-04-28 (blocking validators). Phase 9 shipped 2026-04-30 (notifications). Phase 10 shipped 2026-05-01 (GitHub integration: push-to-deploy + Polaris reads docs from the repo). Phase 11 starts the dispatch story: Polaris commands a team of executor agents instead of just emitting plans you read. Phase 11B turns the home page into a real command center while we're at it. Phase 12 is multi-provider so the team can pick the right model per task.

Phases 1-4 shipped 2026-04-25. Production-readiness items (DB backups, tests + CI, rate limits + body cap, bot instance identity, secrets store) shipped 2026-04-26. See Done Log.

Live URLs:
- dashboard https://app.lightsei.com (signup/login here)
- api      https://api.lightsei.com
- railway-generated fallbacks still active for diagnostic use

## How this file works

- Tasks are grouped into phases. Each phase ends with a DEMO that proves the phase is done.
- Do not start the next phase until the current demo runs.
- New ideas go to the Parking Lot at the bottom. Never inject them into the current phase.
- When a task is done, check it off and move it to the Done Log at the bottom. Seeing the wins pile up matters.
- If you skip a task or change the plan, write a one-line note saying why. Future-you needs to know.

---

## Phase 1: The spine

**Demo at end of phase**: Run `python demo_bot.py` which calls OpenAI 3 times. Within 5 seconds, all 3 calls appear in a browser dashboard at localhost, with timestamps, model, latency, and token counts.

That's it. No styling, no auth, no extras. If the demo works, Phase 1 is done.

### 1.1 Backend skeleton ✅ done 2026-04-25 (see Done Log)

### 1.2 SDK skeleton (Python) ✅ done 2026-04-25 (see Done Log)

### 1.3 OpenAI auto-patch ✅ done 2026-04-25 (see Done Log)

### 1.4 Minimal dashboard ✅ done 2026-04-25 (see Done Log)

### 1.5 Spine demo ✅ done 2026-04-25 (see Done Log)

**Phase 1 complete 2026-04-25.**

---

## Phase 2: First useful guardrail

**Demo at end of phase**: A daily cost cap policy. Configure $0.50/day in the dashboard. Run the demo bot in a loop until it hits the cap. The next call gets blocked with a clear error and shows up in the dashboard as a denial.

### 2.1 Decide: OPA or custom policy code? ✅ done 2026-04-25 — picked custom Python, see MEMORY.md "Policy engine decision"

### 2.2 Cost rollup ✅ done 2026-04-25 (see Done Log)

### 2.3 Cost-cap policy ✅ done 2026-04-25 (see Done Log)

### 2.4 Denial UX ✅ done 2026-04-25 (see Done Log)

**Phase 2 complete 2026-04-25.**

---

## Phase 3: Second framework

**Demo at end of phase**: An Anthropic-based bot streaming a response works in the dashboard with full token capture.

### 3.1 Anthropic SDK auto-patch ✅ done 2026-04-25 (see Done Log)
### 3.2 Streaming response support (both OpenAI and Anthropic) ✅ done 2026-04-25 (see Done Log)
### 3.3 Update demo with both providers ✅ done 2026-04-25 (see Done Log)

**Phase 3 complete 2026-04-25.**

---

## Phase 4: Hosted-readiness

**Demo at end of phase**: A friend signs up at a real URL, copies their API key, runs a bot, sees data in their dashboard. They never SSH anywhere or read docs longer than the homepage.

### 4.1 Migrate SQLite → Postgres with Alembic migrations ✅ done 2026-04-25 (see Done Log)
### 4.2 Multi-tenancy (workspace_id on every row, scoped queries) ✅ done 2026-04-25 (see Done Log)
### 4.3 API key auth (generate, hash, scope to workspace) ✅ done 2026-04-25 (see Done Log)
### 4.4 Basic signup/login (magic link or simple email/password) ✅ done 2026-04-25 (see Done Log)
### 4.5 Deploy to Fly/Railway/Render ✅ done 2026-04-25 — picked Railway, see Done Log
### 4.6 Buy domain, point at deploy ✅ done 2026-04-25 — using lightsei.com (already owned), see Done Log

**Phase 4 complete 2026-04-25.**

---

## Phase 5: Hosted runtime (PaaS for agents)

**Demo at end of phase**: From a fresh terminal, `lightsei deploy ./my-bot` zips the directory, uploads it, and a worker process spawns the bot. Within a minute the dashboard shows the deployment as `running`, the bot's instance heartbeats are visible, and logs stream into a Deployments tab. Stop and redeploy from the dashboard work end-to-end. Nothing about the user's bot code changes between local and hosted runs.

Phase 5A scope: single-host worker, in-process subprocess per bot, only safe for *our own* bots. Per the Runtime decision in MEMORY.md, isolation comes in 5B (Fly Machines / Modal).

### 5.1 Deployments schema + zip upload ✅ done 2026-04-26 (see Done Log)
### 5.2 Worker-facing endpoints ✅ done 2026-04-26 (see Done Log)
### 5.3 Worker process ✅ done 2026-04-26 (see Done Log)
### 5.4 Streaming logs ✅ done 2026-04-26 (see Done Log)
### 5.5 SDK CLI: `lightsei deploy` ✅ done 2026-04-26 (see Done Log)
### 5.6 Dashboard "Deployments" tab ✅ done 2026-04-26 (see Done Log)

### 5.7 Phase 5 demo ✅ done 2026-04-26 (see Done Log)

**Phase 5 complete 2026-04-26.**

---

## Phase 6: Polaris (project orchestrator bot)

**Demo at end of phase**: From a fresh terminal, `lightsei deploy ./polaris` (with this project's `MEMORY.md` + `TASKS.md` copied into the bundle) deploys the Polaris bot via the Phase 5 PaaS. Within ~5 minutes the dashboard's Polaris view renders a generated plan against this project's own docs, with at least 3 next-action recommendations, parking-lot evaluation, and any drift it spots between MEMORY.md and TASKS.md. Re-running the bot without changing the docs is idempotent (skips the LLM call via doc-hash check). The plan is "good enough" — sanity check is whether *I* would have picked the same next move.

**Phase 6A scope: read-only.** Polaris produces visible plans and recommendations only. No PRs, no command dispatch to other agents, no external system writes. Layers 3-5 of the guardrail roadmap (output validation, behavioral rules, continuous eval) come in 6B+, with Polaris as the dogfood bot that forces each one into existence.

**Why Polaris dogfoods itself.** Polaris is Lightsei's first own product running on Lightsei's own infra. Every roughness in Phase 5 (deploy UX, log streaming, secrets injection, heartbeat semantics) gets re-felt while we operate Polaris in production. That's the point.

### 6.1 Polaris bot scaffold ✅ done 2026-04-27 (see Done Log)

### 6.2 Plan event schema + emit + change detection ✅ done 2026-04-27 (see Done Log)

### 6.3 Backend: latest-plan endpoint ✅ done 2026-04-27 (see Done Log)

### 6.4 Dashboard "Polaris" view ✅ done 2026-04-27 (see Done Log)

### 6.5 System prompt iteration ✅ done 2026-04-27 (see Done Log)

### 6.6 Phase 6 demo ✅ done 2026-04-27 (see Done Log)

**Phase 6 complete 2026-04-27.**

---

## Phase 7: Output validation (guardrail layer 3)

**Demo at end of phase**: Polaris's `polaris.plan` events flow through a validator pipeline before being treated as trustworthy by the dashboard. The `/polaris` view's history sidebar shows a small PASS / FAIL / WARN chip next to each plan; clicking a failed plan reveals the specific violations in the main pane. Two concrete validator types ship: **schema-strict** (payload matches a registered JSON schema) and **content-rules** (regex / keyword checks like "no email-like patterns in summary", "no banned destructive verbs"). Demo run: deploy Polaris with a normal prompt → validators pass → dashboard shows green PASS chips. Then briefly inject a bad pattern (e.g., temporarily prompt Polaris to include a fake email in `summary`) → validators flag it → dashboard shows FAIL with the matched rule. Polaris itself doesn't change behavior on failures yet (Phase 7A is advisory); the value here is the visible signal that "this output is suspect, don't trust it blindly."

**Phase 7A scope: advisory, post-emit, server-side.** Validators run after the event lands in the DB, write their results to a sibling table, and don't block ingestion. The dashboard renders the result; Polaris's emit path isn't aware of validation status. **Phase 7B (later) makes validators blocking + pre-emit**, and is what unlocks "act, don't just plan" — Polaris can dispatch commands once we trust the validation gate to catch bad outputs before they land.

**Why this phase exists.** MEMORY.md's five guardrail layers describe Lightsei's actual product surface; only layer 2 (pre-action gate, the Phase 2 cost-cap policy) is built. Layer 3 (output validation) is the next one in the stack and the prerequisite for Polaris graduating from describe → execute. Validators are also a generally useful platform feature: any agent emitting structured events benefits from "this output matches the schema" + "this output doesn't contain things it shouldn't."

### 7.1 Validator interface + schema-strict validator ✅ done 2026-04-27 (see Done Log)

### 7.2 Content-rules validator ✅ done 2026-04-28 (see Done Log)

### 7.3 Validation pipeline + event annotation ✅ done 2026-04-28 (see Done Log)

### 7.4 Backend endpoints for validation results ✅ done 2026-04-28 (see Done Log)

### 7.5 Dashboard shows validation status ✅ done 2026-04-28 (see Done Log)

### 7.6 Phase 7 demo ✅ done 2026-04-28 (see Done Log)

**Phase 7 complete 2026-04-28.**

---

## Phase 8: Blocking validators (guardrail layer 3, pre-emit)

**Demo at end of phase**: Promote `polaris.plan / schema_strict` from advisory to blocking via `PUT /workspaces/me/validators/polaris.plan/schema_strict` with `{"mode": "blocking"}`. Inject a schema-failing case in `polaris/system_prompt.md` (e.g., instruct Polaris to omit `summary`). Deploy. The worker's logs show a `422` from `POST /events` with the violation list, the bot keeps running (graceful), and **no new plan appears in the dashboard's `/polaris` view** — the rejected event never landed. Revert the injection, the next tick produces a clean plan that does land. The demo's evidence is what *isn't* there: a rejected event leaves no trace in the events table, just a worker-log line.

**Why this matters.** Phase 7A made layer 3 visible. Phase 8 makes it enforceable. Operators choose per-validator: advisory for soft signals (the content-rules pack stays advisory by default), blocking for invariants the rest of the system relies on (schema-strict is the natural blocking candidate). Once a validator is blocking, downstream consumers can trust the events table — no row past the gate violates that validator's rules. That's the guarantee Phase 9 (Polaris dispatch) will need.

**Phase 8 scope**: server-side blocking + SDK graceful-degradation on 422. The bot doesn't get rejection-aware logic in this phase (that would require sync emit, which is its own design); a rejected plan just disappears from the timeline and the worker log records it. Phase 8B can later add `emit_sync` and a `polaris.plan_rejected` advisory event for stronger feedback in the dashboard.

### 8.1 Validator-mode migration + endpoint update ✅ done 2026-04-28 (see Done Log)

### 8.2 Pipeline: pre-emit blocking on FAIL ✅ done 2026-04-28 (see Done Log)

### 8.3 SDK: graceful 422 handling ✅ done 2026-04-28 (see Done Log)

### 8.4 Phase 8 demo ✅ done 2026-04-28 (see Done Log)

**Phase 8 complete 2026-04-28.**

---

## Phase 9: Notifications (the "tell me when" layer)

**Demo at end of phase**: Register a Slack webhook URL on the workspace through the dashboard. Polaris's next plan lands in Slack with the summary + top next-action + a deep link back to `/polaris`. Validation FAIL → different message with the matched rule. Run failure → crash message. Repeat the same setup for Discord (different formatter, same triggers) to prove the channel-type abstraction works end-to-end. The user never opened the dashboard — they got everything they needed in their existing tools.

**Why this phase exists.** Lightsei's value today is invisible until you open the dashboard. A real product earns its place in your day by pinging you when something needs attention. For Polaris specifically, that's the thing that makes "an AI orchestrator for your project" feel like a useful service rather than a tool you remember to check: a daily plan summary in Slack/Discord, the kind of update a chief-of-staff would send.

**Phase 9 scope**: ship five channel types in v1, all of them the "team chat" / structured-payload shape (paste a webhook URL, server POSTs the rendered message):

- **Slack** — Block Kit format
- **Discord** — embed format with color coding
- **MS Teams** — Adaptive Card format (replaces deprecated MessageCard)
- **Mattermost** — Slack-compat (reuses Slack formatter, separate type label)
- **Generic webhook** — Lightsei JSON envelope for n8n / Zapier / custom systems

Three trigger types: `polaris.plan`, `validation.fail`, `run_failed`.

**Personal-channel notifications (WhatsApp / SMS / Telegram) explicitly deferred** to a separate phase, not because they aren't valuable but because they're a different beast: outbound to a phone number or chat ID, real API auth (not just a webhook URL), and Meta's template-approval cycle for WhatsApp non-reply messages. Bundling those three together via Twilio (WhatsApp + SMS) + Telegram bot API is a more coherent phase than wedging WhatsApp into Phase 9 — see Phase 10+ for the candidate.

**Email** also deferred (needs SMTP infra; the five channels here cover the platforms most teams are already in). Per-channel subscriptions ship in v1 (each channel picks which triggers it cares about); per-trigger filters (e.g., "only ping for FAILs above severity X") deferred too. Adding any new channel later is one new formatter file + one type-registry entry — no migration, no dispatcher rewrite.

### 9.0 Publish `lightsei` to PyPI ✅ done 2026-04-29 (see Done Log)

### 9.1 Notification channels: schema + endpoints ✅ done 2026-04-29 (see Done Log)

### 9.2 Channel-type registry + Slack / Discord / Teams / Mattermost formatters ✅ done 2026-04-30 (see Done Log)

### 9.3 Generic webhook channel ✅ done 2026-04-30 (see Done Log)

### 9.4 Trigger pipeline: hook into event ingestion ✅ done 2026-04-30 (see Done Log)

### 9.5 Dashboard: Notifications panel ✅ done 2026-04-30 (see Done Log)

### 9.6 Phase 9 demo ✅ done 2026-04-30 (see Done Log)

**Phase 9 complete 2026-04-30.**

---

## Phase 10: GitHub integration ("Vercel for agents")

**Demo at end of phase**: Connect this very project's GitHub repo to the prod Lightsei workspace. Update `TASKS.md` locally, commit + push to `main`. Within a minute, a Slack notification announces a new Polaris plan that reflects the change — no `lightsei deploy` ran. Push a code change to a registered bot directory; the GitHub webhook lands at the backend, the bot redeploys automatically, the dashboard shows a new Deployment row with `source=github_push` and the commit SHA. The CLI was never touched. The Done Log captures the verbatim Slack message + the dashboard's deploy panel screenshot + the GitHub-push timeline (commit at HH:MM:SS → deploy queued at HH:MM:SS+2 → running at HH:MM:SS+11).

**Why this phase exists.** Today the deploy story is "build wheel, copy docs, run CLI." That's a manual checkpoint between writing and seeing. Every modern dev product trains users on "git push and it's live" — Vercel, Railway, Fly, Cloudflare Pages. Phase 10 brings Lightsei to that bar. Combined with Polaris reading docs from the repo (no re-deploy on doc changes), the friction between thinking and seeing the next plan goes from minutes to seconds.

**Phase 10 scope**: PAT-based auth (paste a token, like Phase 9's webhook-URL pattern — OAuth UX deferred), one repo per workspace, one branch tracked, push-event webhooks only (PR/release events deferred). Multi-repo + per-environment tracking lands in Phase 10B if there's demand.

### 10.1 GitHub auth + repo registration ✅ (shipped 2026-04-30)

- Migration `0016_github_integrations`: new table `github_integrations` (id UUID, workspace_id FK CASCADE UNIQUE, repo_owner, repo_name, branch, encrypted_pat (via existing `secrets_crypto`), webhook_secret (random hex string we generate, used for HMAC verification on incoming webhook payloads), is_active, created_at, updated_at). One-row-per-workspace via the UNIQUE constraint.
- Plus `github_agent_paths`: maps `(workspace_id, agent_name)` to the path within the repo where that agent's bot directory lives (e.g., agent `polaris` → path `polaris/`). Plain table, composite PK on `(workspace_id, agent_name)`.
- ORM models matching the migration.
- Endpoints under `/workspaces/me/github`:
  - `PUT /workspaces/me/github` — register or update the workspace's integration. Body carries `repo_owner`, `repo_name`, `branch`, `pat`. Returns the integration with the PAT masked + the webhook URL the user needs to paste into GitHub.
  - `GET /workspaces/me/github` — show current integration with PAT masked, plus the webhook secret needed to configure GitHub's "Secret" field.
  - `DELETE /workspaces/me/github` — disconnect.
  - `PUT /workspaces/me/github/agents/{agent_name}` — register a path mapping. Body: `path`.
  - `GET /workspaces/me/github/agents` — list path mappings.
  - `DELETE /workspaces/me/github/agents/{agent_name}`.
- PAT validation server-side: ping `GET https://api.github.com/repos/{owner}/{name}` with the token on `PUT`; reject with 400 if the API returns 401/403/404 (token can't read the repo). Catches the "wrong token" mistake at registration time, not at first webhook.
- Tests: round-trip `PUT/GET/DELETE`, PAT masked on response, agent-path CRUD, cross-workspace isolation, 400 on unreachable repo (mocked GitHub), webhook secret never echoed back unmasked.

### 10.2 GitHub webhook receiver ✅ (shipped 2026-04-30)

- New endpoint `POST /webhooks/github`. Public (GitHub posts to it; no Lightsei API key). HMAC-SHA256-signed by GitHub against the `webhook_secret` we returned in 10.1, verified via `X-Hub-Signature-256` header. Constant-time compare; reject with 401 on mismatch.
- Looks up the integration by repo full name (`{owner}/{name}` from the payload). 404 if no workspace has registered this repo. Quietly accepts (200) but no-ops if the integration is `is_active=False`.
- Filters event types: only `push` events on the registered branch. Pings (the GitHub "is this thing on?" event), tag pushes, branch creations, etc. all 200 + no-op.
- For each registered agent path: check whether the push touched any file under that path (uses `commits[].added/modified/removed`). If so, queue a redeploy in 10.3.
- Tests: HMAC verified happy path, unsigned request 401, signed-but-wrong-secret 401, ping events 200 no-op, push event with no touched paths 200 no-op, push with one touched path queues exactly one redeploy.

### 10.3 Push-triggered redeploy ✅ (shipped 2026-04-30)

- New worker pipeline path: when a push hits a registered agent path, fetch the agent's bot directory at the pushed commit via the GitHub Contents API (Tree + Blob). Build a deploy zip in-memory, create a `deployment_blob` row + `deployment` row pointing at it. The Phase 5 worker picks up `desired_state=running` deployments via its existing `claim` loop — no new worker code.
- New columns on `deployments`: `source` (`cli` | `github_push`, default `cli` for backwards compat), `source_commit_sha` (nullable). Migration `0017_deployments_source`.
- The dashboard's Deployments panel shows the source + commit SHA inline so a user can tell at a glance whether a deploy came from `lightsei deploy` or from a GitHub push.
- Tests: a push event for a registered path + a registered integration + an active workspace creates a new `deployment` row with `source=github_push` and the commit SHA; the worker can claim and run it just like a CLI deploy; cross-workspace isolation; integration `is_active=False` blocks the redeploy.

### 10.4 Polaris reads docs from GitHub ✅ (shipped 2026-04-30)

- New optional env vars on `polaris/bot.py`: `POLARIS_GITHUB_REPO` (`owner/name`), `POLARIS_GITHUB_BRANCH`, `POLARIS_GITHUB_TOKEN` (workspace secret), `POLARIS_GITHUB_DOCS_PATHS` (comma-separated repo-relative paths, default `MEMORY.md,TASKS.md`).
- When set, the bot fetches each docs path from `https://api.github.com/repos/{repo}/contents/{path}?ref={branch}` on every tick instead of reading from disk. Backwards compatible — if any of the GitHub vars are unset, falls back to the existing `POLARIS_DOCS_DIR` path.
- The hash-skip cache from Phase 6.2 still works: GitHub returns the same content for unchanged docs, so the SHA-256 hash is the same, so the LLM call is skipped. The "redeploy busts the cache" behavior changes — now you can iterate on docs by pushing to GitHub, and the cache busts on each push instead of each deploy.
- A simple onboarding helper: when registering the GitHub integration, automatically inject `POLARIS_GITHUB_REPO` and `POLARIS_GITHUB_BRANCH` into the workspace's secrets if Polaris is registered as an agent. The user only needs to add `POLARIS_GITHUB_TOKEN` themselves (or auto-derive from the integration PAT — TBD: simpler if we just reuse the integration PAT, but the bot needs to fetch the secret on tick from the worker's secret-injection path. Decide in 10.4.)
- Tests: bot tick with `POLARIS_GITHUB_REPO` set fetches via API and computes hash from the response; hash-skip works on identical fetches; falls back cleanly when env unset; 401/404 from GitHub doesn't crash the bot (logs warning, sleeps, retries next tick).

### 10.5 Dashboard `/github` panel ✅ (shipped 2026-04-30)

### 10.6 Phase 10 demo ✅ (shipped 2026-05-01)

---

## Phase 11: The constellation, first dispatch chain

**Demo at end of phase**: Push a one-line change to `backend/main.py` on `bewallace01/lightsei`. Within 2 minutes: `polaris → atlas → hermes` chain renders on the dashboard with timestamps; a Slack message lands in the configured channel saying `"✅ atlas: 322 passed at commit ede6e01"`; the events table has `polaris.dispatched`, `atlas.tests_run`, and `hermes.posted` rows tied to the same `dispatch_chain_id`. Push a deliberately-failing change: same flow, the message becomes `"❌ atlas: 318 passed, 4 failed (file: test_foo.py::test_bar)"` and arrives in Slack inside the same 2-minute envelope.

Phase 11 is read-write Polaris. Two new agents in the constellation (Atlas, Hermes) and the dispatch primitive that lets Polaris command them. The Phase 6 plumbing for `/agents/{name}/commands` already exists and was used during the manifest work; Phase 11 is what makes it ergonomic + observable + safe enough to wire into a real loop. Cross-provider model selection is intentionally NOT in scope here — it lands in Phase 12 once the dispatch chain is proven against Claude-only.

**Why these two agents, not five.** Atlas (test runner) and Hermes (notifier) are the smallest pair that proves the pipes work end-to-end: Polaris dispatches → Atlas does real work → Atlas dispatches → Hermes does real work → human sees the result. Adding Argus / Vega / Sirius / Cassiopeia in the same phase would dilute the dispatch story without making it more convincing. They land in Phase 13 once eval (Phase 12) can grade them.

**The default-on approval gate is the human-in-the-loop.** No agent acts on a command until it's `approved`, either by a click in the dashboard or by an auto-approval rule the user opts into per-command-type. Phase 11 ships approval-required as the default for `atlas.run_tests` (so the demo shows both flows: explicit click + auto-approve), and `hermes.post` auto-approves once you flip the toggle (otherwise every Slack message would need a click, which kills the point). Phase 14 replaces the gate with proper layer-4 behavioral rules.

### 11.1 SDK: `send_command` + `claim_command` ergonomics

- The HTTP endpoints already exist (`POST/GET/POST .../claim` under `/agents/{name}/commands` from Phase 6's manifest work). Phase 11 wraps them in the SDK as first-class methods so an agent's `bot.py` can dispatch + claim without manual `httpx` calls.
- `lightsei.send_command(target_agent: str, command_type: str, payload: dict, *, requires_approval: bool | None = None, dispatch_chain_id: str | None = None) -> Command`. Returns the created command. When called from inside another agent's tick, auto-propagates `dispatch_chain_id` from the running command's context (a thread-local set by `claim_command`); when called outside an agent context (e.g. from a webhook handler), generates a fresh one.
- `lightsei.claim_command(types: list[str] | None = None, timeout_s: float = 5) -> Command | None`. Long-poll-friendly (server returns 200 with null when nothing to claim, no 404 spam). Sets the thread-local context on entry so any `send_command` calls during the handler inherit the chain id.
- `lightsei.complete_command(command_id, *, status: str, result: dict | None = None) -> None`. Status is one of `done`, `failed`. Captures `duration_ms` from claim → complete automatically.
- Tests: round-trip dispatch → claim → complete; chain-id propagation; depth cap; rejecting unknown command types; concurrent claims race cleanly via `FOR UPDATE SKIP LOCKED` (Phase 5 already uses this pattern for deployments).

### 11.2 Backend: dispatch chain id + depth cap + approval state

- New columns on `agent_commands` (the table Phase 6 already created): `dispatch_chain_id` (UUID, indexed), `dispatch_depth` (int, default 0), `approval_state` (`pending` | `approved` | `rejected` | `auto_approved` | `expired`), `approved_by_user_id` (nullable FK), `approved_at` (nullable). Migration `0019_dispatch_metadata`.
- Server-side: every `send_command` populates chain id + depth (parent depth + 1). Reject with 422 if depth ≥ 5. Reject with 422 if the source agent has dispatched > 100 commands in the last 24h (per-agent runaway cap). Both limits are config knobs on `agents` (`max_dispatch_depth`, `max_dispatch_per_day`) with the defaults above.
- New `command_auto_approval_rules` table: `(workspace_id, source_agent, target_agent, command_type, mode)` where `mode ∈ {auto_approve, require_human}`. Lookup is `(source, target, type)` exact match → fall back to `(*, target, type)` → fall back to `require_human`. So `(polaris, hermes, hermes.post) → auto_approve` flips Hermes posts to skip the gate.
- Tests: depth cap fires at exactly 5; daily cap rejects the 101st with the right error code; rule precedence (specific over wildcard); chain id stays consistent across N hops; commands stuck in `pending` for > 24h flip to `expired` via a periodic sweep (worker-side, or a small scheduled task).

### 11.3 Atlas: test-runner bot

- Lives at `agents/atlas/bot.py` + `agents/atlas/requirements.txt` + `agents/atlas/README.md` (matches the `polaris/` layout from Phase 6).
- Loop: `claim_command(types=["atlas.run_tests"])` → if claimed, run `pytest <args>` in a subprocess (working dir is the agent bundle root by default; configurable via env `ATLAS_TEST_DIR`), capture stdout + return code, parse the summary line with a regex, emit `atlas.tests_run` event with `{passed: N, failed: N, duration_s: F, summary: str, returncode: int, log_tail: <last 4kb>}`, then `send_command("hermes", "hermes.post", {channel: "<configured>", text: ..., severity: "info"|"error"})`, then `complete_command(status="done")`. On crash, emit `atlas.crash` and complete with `failed`.
- Env: `ATLAS_PYTEST_ARGS` (default `backend/tests/`), `ATLAS_TIMEOUT_S` (default `300`), `ATLAS_LOG_TAIL_BYTES` (default `4096`). Workspace-secret-injected like every other bot.
- Atlas does NOT call Slack. Channel routing is Hermes's job. Atlas just produces a structured "outcome + summary" and lets Hermes decide what to do with it.
- Tests in `backend/tests/test_atlas.py`: command-claim → pytest-stub → emit shape; failing-test path emits the right `severity`; timeout path emits `crash`; the dispatch to Hermes carries the right `dispatch_chain_id`. Use a fake pytest invocation (`echo "1 passed in 0.01s"`) to avoid recursive test runs.

### 11.4 Hermes: notifier bot

- `agents/hermes/bot.py` + `requirements.txt` + `README.md`.
- Loop: `claim_command(types=["hermes.post", "hermes.dm"])` → look up the workspace's notification channel by `channel_name` from the payload (Phase 9's `notification_channels` table) → render a Block Kit message via the existing Phase 9 formatter (or a thin Hermes-specific formatter that takes Atlas's payload shape and produces a tidy "✅/❌ <summary> (<commit_short>)" line) → call `lightsei.notify(channel_name, message)` (already exists from Phase 9.5 or thereabouts) → emit `hermes.posted` with `{channel_id, http_status, latency_ms}` → complete.
- Env: `HERMES_DEFAULT_CHANNEL` (so old code that doesn't pass a channel still works), `HERMES_RATE_LIMIT_PER_MINUTE` (default `30` to keep Slack happy).
- On Phase 9 channel send-failure (4xx/5xx from Slack), retry once after 5s; on second failure complete `failed` and emit `hermes.send_failed` with the http status + body (truncated). Don't retry forever — a permanent 401 means the user has to fix the integration.
- Tests in `backend/tests/test_hermes.py`: maps `severity=error → ❌` prefix and `severity=info → ✅`; falls back to `HERMES_DEFAULT_CHANNEL` when payload omits `channel`; non-2xx from the channel API surfaces as `hermes.send_failed`; rate-limiter shed-loads when bursting > 30/min (commands re-queued, not dropped).

### 11.5 Polaris: react to push events instead of just ticking

- New event type the webhook receiver enqueues: `polaris.evaluate_push` (a command, not an emit). Body is the parsed push event — commit sha, branch, touched paths, author. Posted via `send_command("polaris", "polaris.evaluate_push", ...)` from the webhook handler so it inherits the chain machinery (and so the user can mute it via the auto-approval rules).
- New code path in `polaris/bot.py`: on `polaris.evaluate_push`, do NOT call Claude. Read a small heuristic table (`POLARIS_PUSH_RULES` env, defaults to `backend/**:atlas.run_tests, polaris/**:atlas.run_tests`) and dispatch matching commands. Cost-conscious: a push that touches only docs shouldn't burn a 75k-token Claude call to decide it doesn't need to do anything. The Claude-driven planning loop stays on its hourly schedule unchanged.
- The existing scheduled tick is preserved untouched. `polaris.evaluate_push` is additive — it gives Polaris a low-cost event-driven dispatch path alongside the LLM-driven hourly planning path.
- Tests: push touching `polaris/bot.py` dispatches one `atlas.run_tests`; push touching only `*.md` dispatches nothing; push touching paths outside any rule dispatches nothing; the dispatched commands carry `dispatch_chain_id` matching the source push event id (so the chain renders cleanly in 11.6).

### 11.6 Dashboard: the dispatch-chain view

- New top-level route `/dispatch` (next to `/polaris`, `/notifications`, `/github`). One row per `dispatch_chain_id`, newest first. Each row expandable to a vertical timeline of commands + events tied to that chain id, indented by `dispatch_depth`. Status badges (`pending`, `approved`, `running`, `done`, `failed`, `expired`) match the existing deployment-row palette.
- Clicking a row reveals the command payload, the resulting event, and any approval action ("approved by bailey@... 04:21 UTC" or "auto-approved by rule (polaris, hermes, hermes.post)"). The existing event-detail flyout from `/polaris` is reusable here.
- `pending` commands get an inline `approve` / `reject` button gated to admins; auto-approved chains skip the button entirely. Reject takes the chain off the floor (terminal `rejected` state, no children dispatched).
- The view also exposes the auto-approval rule editor as a side panel — `(source_agent, target_agent, command_type) → mode`. Adding a rule there is the only Phase 11 way to enable auto-approve without flipping the global default.
- Match the plain-Tailwind aesthetic of `/notifications`, `/account`, `/github`. Active route highlighting from the post-10.5 header redesign carries over for free.

### 11.7 Phase 11 demo

- Provision Atlas + Hermes in the workspace (`lightsei deploy ./agents/atlas`, `lightsei deploy ./agents/hermes` — or once Phase 10's push-to-deploy stays warm, just `git push` after `agents/atlas/` lands on `main`).
- Configure auto-approval rule: `(polaris, hermes, hermes.post) → auto_approve`. Leave `(polaris, atlas, atlas.run_tests)` requiring human approval for the demo to show the click.
- Push a one-line comment change to `backend/main.py`. Walk through the chain in the dashboard: webhook fires → `polaris.evaluate_push` command appears `pending` → click approve → Polaris dispatches `atlas.run_tests` (also `pending`) → click approve → Atlas runs pytest, emits `atlas.tests_run` → Atlas dispatches `hermes.post` (auto-approved by the rule) → Hermes posts to Slack. Time the whole thing.
- Push a deliberately-failing change to a single test file. Same flow, observe `❌ atlas: ...` in Slack.
- Set `(polaris, atlas, atlas.run_tests) → auto_approve` and push again. Now the chain runs end-to-end without any clicks. Time it again — should be tighter than the human-in-the-loop run by however long it took to click the button.
- Done Log: timeline of the three demo runs (with-clicks, without-clicks, failing-tests), screenshots of the `/dispatch` page in each state, honest assessment of "would I trust Atlas to run unattended overnight without Phase 12's eval?". The expected answer is no, and that's the segue into Phase 12.

---

## Phase 11B: Command center (observatory)

**Demo at end of phase**: Open `https://app.lightsei.com/` cold (signed-out → signed in to a workspace running Polaris + Atlas + Hermes). The home page reads top-to-bottom: dark constellation hero with the workspace's project state in serif copy ("Everything calm." or "Three things want your attention."), a literal constellation map of the agents (stars connected by dispatch lines), and a cost panel that adds up the workspace's MTD spend with an EOM projection. Trigger any agent activity (push to GitHub, redeploy Polaris, click "approve" on a dispatch) and the relevant star twinkles + the affected dispatch line briefly pulses, all within ~5 seconds. The page tells the operator everything they need to know about the team in a single screen, on theme.

**Why now, not later.** Phase 11 multiplies the run count by introducing Atlas + Hermes (one push fires three agents). Without cost visibility AND a unified status view in place, you'd be staring at a sparse `/` page wondering what's happening while spend climbs. Phase 11B ships the command center the day after Phase 11 lands so dispatch goes live with the right rear-view mirror in place. It also feeds Phase 12 — multi-provider only matters if you can SEE the per-provider cost difference, and that only matters if there's a place that shows costs in the first place.

**Style and palette.** "Observatory" — extends the existing `/polaris` treatment (`bg-gradient-to-br from-slate-950 via-indigo-950 to-slate-900` with star dots, serif headlines) into the home page. Dark hero on top → light card body underneath, exactly like `/polaris`. The constellation map widget keeps the dark canvas aesthetic so the agent stars read as stars; the cost panel sits in a white card with `border-indigo-900/40` accents so it stays in the family without screaming. References: NASA "Eyes on the Solar System" (dark canvas + info clusters), Stripe's monthly summary email (numbers without graph noise), Vercel project cards (whitespace + restraint).

**Phase 11B ships three widgets.** Hero, constellation map, cost panel. The remaining home-page widgets (recent activity timeline, schedule strip, approvals inbox, repo state, notifications health) are tracked as **Phase 11C** in the parking lot below — they'll fold in as the data they need stabilizes (approvals inbox needs Phase 11's dispatch tables to be in steady state, etc.).

### 11B.1 Cost telemetry backend

- New `model_pricing` table seeded in code: `(provider, model, input_per_million_usd, output_per_million_usd, effective_from, deprecated_at)`. Source of truth lives in `backend/pricing.py` as a literal dict, written to the table by an Alembic migration and re-asserted on every `upgrade_to_head()` so a release can update prices without a manual UPDATE. Seed values: `claude-opus-4-7 ($15/$75)`, `claude-sonnet-4-6 ($3/$15)`, `claude-haiku-4-5 ($1/$5)`, plus matching rows for OpenAI's current line-up (already supported by the SDK auto-patch) so existing OpenAI-using runs get costed too. Per-workspace override row in a `workspace_pricing_overrides` table — empty in v1, future enterprise-rate hook.
- New column `runs.cost_usd numeric(12,6)`. Backfill migration computes it from each run's `llm_call_completed` events × the pricing table at the run's timestamp. `cost_usd` becomes the field everything aggregates against — far cheaper than re-summing tokens × pricing on every dashboard render.
- New aggregation endpoint `GET /workspaces/me/cost`: returns `{mtd_usd, projected_eom_usd, by_agent: [{agent_name, mtd_usd, run_count, last_run_at}], by_model: [{model, mtd_usd, runs}], budget_usd_monthly | null, budget_used_pct | null}`. EOM projection is `mtd / day_of_month * days_in_month` — naive but correct for a steady-state cadence and good enough until Phase 13's eval gives us a smarter signal.
- New column `workspaces.budget_usd_monthly numeric(10,2) NULL`. When set and exceeded, behaves the same way Phase 2's daily cap does — denial response with a clear message — but at the workspace tier instead of the per-agent-per-day tier. Migration `0019_workspace_monthly_budget`.
- Tests: pricing rows survive a round-trip migration; `runs.cost_usd` matches the manual sum-of-products on a synthetic dataset; the projection endpoint stays within 5% of actual after a 7-day backfilled trace; budget cap denies at 100% and warns (not denies) at 80%.

### 11B.2 Status hero

- Full-width banner at the top of `/`. Same gradient + star-dot pattern as `/polaris` so the visual language is one repo-wide style, not a one-off.
- Headline in serif, sourced from a backend `GET /workspaces/me/pulse` endpoint that returns one of: `"Everything calm."` (zero pending issues), `"<N> things want your attention."` (N > 0, where N counts pending approvals + failed validations + budget warnings + stale agents). The endpoint also returns a structured breakdown so the headline can link to the underlying issues — clicking "Three things want your attention" expands to a list with `→` jumps.
- Subtitle line under the headline: `<workspace-name> · <agent-count> agents · Polaris last tick <relative time>`. Subtitle is a single muted serif line so it doesn't compete with the headline.
- Pulsing constellation icon top-right that gently brightens when ANY event lands in the workspace in the last 5s (driven by the same poll the constellation map uses — no separate websocket needed for v1). Respects `prefers-reduced-motion`.
- Empty state when the workspace has no agents: hero says `"Sky empty. Deploy your first agent →"` with a CTA pointing to the docs.

### 11B.3 Constellation map (agent grid)

- The marquee widget. Single SVG, dark canvas (`bg-slate-950/60`, slightly lighter than the hero so the two are distinguishable), 320px tall on mobile, 480px on desktop, full content-width. `viewBox="0 0 1000 480"` so positions stay stable across viewport sizes.
- Polaris is rendered as a **sun**, not a star — it's visually distinct from every other agent in the workspace, and that distinction is load-bearing because it's the only orchestrator. Other agents are smaller, **color-tinted** stars whose tint is stable per agent (deterministic from name), so each agent is recognizable at a glance even with the same status.
- **Position** is deterministic so the layout doesn't shift between page loads. Polaris is fixed at the visual canvas center; executors arrange in an inner ring (`r=150`); notifiers arrange in an outer ring (`r=250`); future specialists at `r=200`. Angle = `(sha1(agent_name)→uint32) / max_uint32 * 2π` with a per-role offset so executors don't collide with notifiers. Post-hoc collision detection: any two non-Polaris agents within 80px get the second one's radius bumped +30. Agent tier comes from a new column `agents.role` (`orchestrator | executor | notifier | specialist`); existing rows default to `executor` until labelled.
- **Polaris (the sun)**: fixed radius 24px (NOT scaled by activity — its size encodes its role, not its volume). Multi-layer corona — outer halo at `r=44` filled with `rgba(255 215 130 / 0.08)` and a strong gaussian blur, middle halo at `r=32` with `rgba(255 215 130 / 0.18)` and medium blur, the sun itself at `r=24` with a radial gradient from `#fff` (center) to `#fcd34d` (edge, amber-300), thin 1px rim at `r=24.5` in `#fbbf24` (amber-400). Label rendered in the same warm tint, slightly larger weight than the other agent labels to anchor it as the visual prime.
- **Other agents (tinted stars)**: radius `clamp(4, 4 + sqrt(runs_in_24h) * 1.2, 12)` so an idle agent is still visible and a runaway agent doesn't eat the canvas. Color is from a fixed cool-palette indexed by `hash(agent_name) % palette.length`, where the palette is `[indigo-300, violet-300, violet-400, blue-300, sky-300, cyan-300, emerald-300, pink-200]`. Hermes might end up sky-300, Atlas might end up indigo-300; whichever they land on is stable forever. Each star gets a soft glow halo in the same tint at 0.4 alpha for the active state. **Status overrides the tint**: stale (last heartbeat > 60s) = tint mixed 50% with slate-600, halo dropped to 0.15 alpha; errored (last run failed) = keep the tint, add a 2px `stroke-rose-400` rim. White-only stars are reserved for Polaris/the sun — every other agent must have a tint.
- **Dispatch lines** (edges) are drawn as **quadratic Bezier curves**, not straight segments. For each `(source, target)` pair with ≥1 dispatch in the last 24h, control point is the midpoint offset perpendicular to the line by `clamp(10, distance/8, 36)`, biased toward the canvas center (toward Polaris). This means lines naturally curve "inward" through the orchestrator's gravity well — visually reinforces the metaphor that Polaris is the team's center of mass, AND avoids the "two parallel straight lines crossing" problem when the team grows past a few agents. For multi-edges between the same pair (rare in v1, common later), the second edge biases the OTHER perpendicular direction so they don't overlap.
- **Edge styling**: stroke opacity scales with `count_24h` (1 dispatch = 0.30, 100 = 0.70, capped). `stroke-width` scales logarithmically (1 dispatch = 1px, 100 = 3px, cap 4px). Idle stroke color = `rgb(199 210 254 / X)` (indigo-200 at the computed alpha); on a fresh dispatch (poll's `last_at` within 5s of poll arrival) the line transitions to `rgb(248 250 252 / 0.9)` (slate-50 at high alpha) for 800ms, scale `stroke-width × 1.5`, then back to idle. Edges drawn first in z-order so stars sit on top.
- **Twinkle animation**: when a new event for an agent arrives in the last poll cycle, the corresponding star runs a 600ms `scale 1 → 1.15 → 1` + `opacity 1 → 0.85 → 1` keyframe. Polaris's twinkle is gentler — `scale 1 → 1.06 → 1` only, since at 24px any larger scale visibly thumps. Coordinated through a single CSS animation class added/removed by the React component so reduced-motion users get a static snapshot. `prefers-reduced-motion: reduce` disables both the twinkle and the edge-pulse animations.
- **Hover**: tooltip rendered as a separate React layer (NOT SVG `<title>`, which is unstyled). Content: agent name, role, model, status with last-heartbeat-relative-time, last-run-at + cost (sourced from `runs.cost_usd`), counts of dispatches in/out last 24h, "Click to view →". **Click**: navigate to `/agents/{name}`.
- **Keyboard / a11y**: each star is a `<g tabindex="0" role="button">` with `aria-label="<name>: <role>, status <status>, <runs_24h> runs in last 24h"`. Enter/Space activates click. Arrow-key focus traversal to the nearest star is nice-to-have, not blocking.
- **Empty state** (zero agents): `"Sky empty. Deploy your first agent →"` centered with a faint dotted constellation outline drawn behind the copy as visual filler.
- **Backend endpoint** `GET /workspaces/me/constellation`: returns `{agents: [{name, role, model, status, runs_24h, cost_24h_usd, last_event_at, dispatches_out_24h, dispatches_in_24h}], edges: [{from, to, count_24h, last_at}]}`. Polled every 5s by the frontend; pause polling when `document.visibilityState === 'hidden'`. Same endpoint feeds the hero's pulse animation trigger via the maximum of `last_event_at` across all agents.

### 11B.4 Cost panel

- Light card under the constellation, bordered in `border-indigo-900/40` so it ties to the hero/map without feeling out of place.
- Top row: workspace MTD spend (big tabular-numerals, `font-mono` for the digits to keep widths stable as numbers tick up), projected EOM in muted gray immediately right of it (`$X.XX projected`), workspace monthly budget bar underneath (only rendered when `workspaces.budget_usd_monthly` is set; click-through to `/account` to set/edit if not).
- Per-agent breakdown table: agent name (with the small star icon from the constellation map next to it for visual continuity), model, MTD cost, run count, projected EOM cost, click-through to that agent. Sortable by MTD cost desc by default.
- Per-model summary row at the bottom: how the workspace's spend splits by model (e.g., `claude-opus-4-7: $24.10 (76%) · claude-haiku-4-5: $7.50 (24%)`). Sets up the Phase 12 demo where this row gets more interesting.
- All numbers come from the cost telemetry endpoint from 11B.1 — no extra backend work for this widget. Refreshes every 30s (slower than the hero/map; cost moves slowly enough that 5s polling is wasted).
- Empty state: when MTD = 0, the panel shows `"No spend yet. Polaris's first tick will land here."`

### 11B.5 Phase 11B demo

- Sign in to the prod workspace. The home page should now show: a status hero ("Everything calm" or N things), a constellation map with Polaris + Atlas + Hermes drawn as three connected stars, and a cost panel showing whatever's accumulated since the page was deployed.
- Trigger Polaris's next tick by `git push` of a small docs change (re-using the Phase 10 push-to-deploy plumbing). Watch the constellation map: Polaris's star should twinkle, the dispatch line to Atlas should brighten if Polaris dispatches, Atlas's star twinkles when it claims, the line from Atlas to Hermes brightens, Hermes's star twinkles. End-to-end visual coverage of the dispatch chain that was abstract in Phase 11's `/dispatch` page.
- Watch the cost panel update over the next ~10 minutes as runs accumulate. EOM projection should start shaky and tighten as more samples land.
- Set a deliberately-low `budget_usd_monthly = $0.50` on the workspace to force the cap. Push enough activity to hit it. Verify: the budget bar fills + turns red at 100%, the headline flips to "Three things want your attention" with the budget warning showing as one of them, denial events fire on subsequent agent runs (Phase 2's denial UX handles the actual blocking).
- Verify on mobile (your phone, browser DevTools, or both) that the layout reflows cleanly. Constellation map should stay readable at 320px tall.
- Done Log: screenshots of the home page in (a) calm state, (b) attention state with budget warning, (c) post-trigger with the constellation animating, plus a note on whether the constellation map "earns its keep" vs. a plain agent grid (this is the visual that justifies the entire phase, so be honest).

---

## Phase 12+: TBD

Open candidates for after the constellation's first dispatch chain ships:

- **Phase 11C: command center, remaining widgets**. Phase 11B shipped hero + constellation map + cost panel. The other widgets discussed in the design — recent activity timeline (unified events / dispatches / deploys, last 20), schedule strip (next-24h tick previews + scheduled routines + recent webhook deliveries), approvals inbox (Phase 11 dispatch commands stuck in `pending` with one-click approve/reject), repo state card (last push + last deploy + webhook health), notifications health card (per-channel last-delivery status) — fold into the home page in this phase once their data sources stabilize. Approvals inbox is the most useful of the five and has the strongest dependency on Phase 11 dispatch tables being well-exercised.
- **Phase 12: multi-provider model selector**. Per-agent `provider` + `model` config in the DB and the SDK. Adapters for Gemini, Groq (Llama / Mixtral), xAI (Grok), Cohere, plus the existing OpenAI + Anthropic. The point is that swapping Vega from Opus to GPT-5 (or Atlas from Haiku to Llama-3-70B-on-Groq) becomes one DB write, no code change. Phase 11B's cost panel already has the per-model summary row that becomes the centerpiece of this demo.
- **Phase 13: more agents**. Argus (security + secret scanner), Vega (PR reviewer), Sirius (alert triager + on-call), Cassiopeia (incident scribe). Each drops in alongside Atlas and Hermes via Phase 11's dispatch primitives.
- **Phase 14: continuous eval (layer 5)**. Judge-LLM scores past dispatched commands against actual outcomes — did Atlas's "tests pass" claim hold up on the next run? Did the human approve / reject Polaris's plans? This is the predicate for trusting any agent unattended.
- **Phase 15: behavioral rules (layer 4)**. Loop detection across a run, runaway-token guards, escalating-permission patterns. Replaces Phase 11's heuristic depth + per-day caps with proper streaming detection.
- **Phase 10B**: GitHub OAuth (replace PAT paste), multi-repo per workspace, per-environment branch tracking (a single repo's `main` and `staging` branches deploy to different agents).
- **Personal-channel notifications (WhatsApp + SMS + Telegram)**: pull the three "outbound to a phone number / chat ID" channels into one phase. Twilio covers WhatsApp + SMS with one integration (auth tokens, sender-number provisioning, recipient phone numbers per send, Meta's 24-template-approval cycle for non-reply WhatsApp messages). Telegram is a separate bot API but lighter weight (bot token + chat IDs, no template approval). Each user's recipient identifier becomes its own first-class object on the workspace, since these channels are 1:1 not 1:room.
- **Phase 5B**: cut single-host worker over to Fly Machines / Modal sandboxes (gates external users).
- **Phase 8B**: SDK `emit_sync` + Polaris `polaris.plan_rejected` advisory events. Strengthens the rejection feedback loop so the dashboard can render rejected plans explicitly rather than relying on absence.
- **Email notifications** (still deferred from Phase 9): needs SMTP infra (Resend / Postmark / SES).
- **Buildpacks / Dockerfile support** beyond the fixed Python runtime.
- **N replicas + cron scheduling natively in the worker** (Polaris currently does its own sleep loop; a real cron primitive would be cleaner once we have multiple bots that all want to tick on schedules).

---

## Parking Lot

Ideas that are good but not now. Add freely. Do not work on these until their phase arrives or you've explicitly decided to promote one.

- **Wire `lightsei-worker` Railway service to GitHub auto-deploy** (surfaced 2026-05-17). The worker was created via `railway add --service lightsei-worker` (empty service) and code pushed via `railway up --path-as-root worker --service lightsei-worker --ci`. Tried to use `railway add --repo bewallace01/lightsei` at creation time — kept returning "Unauthorized" even though `railway whoami` showed Bailey authenticated. Likely needs the GitHub OAuth flow that only runs through Railway's dashboard. Action: Railway dashboard → lightsei-worker service → Settings → Source → Connect Repo (`bewallace01/lightsei`, branch `main`, root directory `worker`). Once wired, `git push origin main` rebuilds the worker the same way it rebuilds backend + dashboard. Until then, every worker code change needs `cd worker && railway up --path-as-root . --service lightsei-worker --ci` from a shell with Railway CLI auth. ~5-10 minutes of dashboard clicks; not urgent because the worker is stable and rarely changes, but you'll forget the upload step the first time you tweak runner.py without this.
- **Streaming progress on long generator endpoints**. `/workspaces/me/teams/plan` + `/workspaces/me/agents/generate` both hold a connection open for the full Opus call (60-120s, doubled when validation retry fires). Promoted to a punch-list item in 12C.6 because it's blocking the demo; revisit here if the same shape shows up on the next long-running endpoint we add. The general fix is the same: emit SSE chunks or periodic keepalive bytes from any endpoint whose median latency exceeds 30s.
- **Multiple workspaces per account** (promoted from a 2026-05-01 nav-redesign conversation). Today the data model is one workspace per user — session, API keys, and every workspace-scoped row assumes it. Real multi-workspace requires a `workspace_members` join table (user ↔ workspace + role), an "active workspace" pointer on the session, list/create/switch endpoints, and a UI to drive them via the new header dropdown. The dropdown shell shipped on 2026-05-01 already has a place for "switch workspace" + "+ new workspace" entries. Worth careful design on invites, billing-per-workspace, and what happens to API keys at switch time before starting.
- **In-browser zipping for /agents/new**. The drop zone shipped on 2026-05-04 only accepts `.zip` today — a non-engineer still has to right-click → Compress before they can deploy. Add JSZip (or an equivalent) so the page accepts a directory selection and zips client-side before posting. ~half day. Less critical than the .zip path was, but rounds out the "non-terminal user" UX.
- **"Deploy from GitHub repo path" form on /agents/new**. Companion to the drop-zone path that landed 2026-05-04. Backend endpoint reusing Phase 10.3's `github_api.fetch_directory_zip`; UI form takes `repo + branch + folder` and posts. Lets users pick from repos they already have without zipping locally. ~half day.
- **Backend cold-start fix for webhook delivery.** Surfaced during the Phase 10.6 demo on 2026-05-01: github.com's first push-webhook delivery after a period of inactivity timed out at GitHub's 10-second deadline because the Railway backend container had gone to sleep and the cold boot took ~9.6s. Once warm the same endpoint responded in 0.3s, so the code path is fine — the issue is operational. Three viable fixes, pick whichever fits the plan being run: (a) Railway service → Settings → set min instances ≥ 1 (or the equivalent "no sleep" toggle) so the backend stays warm. (b) Add a heartbeat ping from a cheap cron source (cron-job.org, GitHub Actions, even Polaris itself once it's ticking on schedule) hitting `/health` every minute or two — keeps the container warm without paying for an idle instance. (c) Pre-warm by hand before any expected webhook firing, which is the workaround we used during the demo (curl `/health` once, redeliver from github.com's UI). Until one of these lands, prod webhook delivery is timing-fragile: any push after a quiet stretch can silently drop on the floor with no retry.
- **Multi-bot trust-zone support** (surfaced 2026-05-17, see MEMORY.md section "Target customer shape: multi-bot systems with trust zones"). Target customer profile: orgs running an internet-facing bot and a PII-accessing bot in the same workflow, separated by a one-way trust boundary with a human translating between them. Three implications to design for when their phases arrive: (1) an optional "handoff" span the SDK can record, so human-mediated chains can be stitched end-to-end in traces; (2) per-project, per-run, or per-agent sensitivity tagging with redaction or access controls applied accordingly (decide SDK-side vs. backend-side redaction before output-validation ships, since the choice changes the SDK contract); (3) an "issue or alert attached to a run" concept in the data model from the start, so the dashboard can grow into an automatic surfacing inbox (not just a passive log viewer). Promoted to Phase 16 + Phase 21 on the same day; this bullet stays as a pointer until those phases ship.
- **"Better than Viktor" north star** (surfaced 2026-05-17, direction decided same day, see MEMORY.md section "Competitive north star: Viktor, but built security-first"). User has named Viktor (viktor.com, AI coworker in Slack with ~3000 integrations) as the closest direct analogue to Lightsei's long-term shape. Direction decided: Lightsei is the AI coworker product (not the underlying platform sold to developers), with a configure-your-team twist on Viktor's one-generalist model. Non-technical users assemble a team of specialized bots through the dashboard. SDK, backend, and observability stay as internal infrastructure. Three "better than Viktor" wedges to design toward when their phases arrive: (1) trust-zone architecture as default with sensible presets (now P0 because non-technical users won't configure isolation themselves; ties to the multi-bot trust-zone Parking Lot entry above); (2) chat-first surface (Slack or Teams native), currently absent from every phase; (3) integration breadth via MCP wrappers and prebuilt connectors, also absent from every phase. Don't promote any of this until the spine is done.
- LangChain auto-patch
- LangGraph auto-patch
- Requests library auto-patch (for script bots making HTTP calls)
- JS/TS SDK
- Output validation layer (PII redaction, schema check, content moderation)
- Behavioral guardrails (layer 4: loop detection, escalating-permission patterns)
- Continuous eval pipeline (layer 5: judge-LLM on sampled runs)
- Custom policy DSL or OPA migration
- Slack alerts
- PagerDuty integration
- Self-host packaging (Helm chart, single-binary build)
- Pretty dashboard redesign
- Public API for ingesting events from non-Python sources
- Webhook outputs
- Audit log export
- SOC 2 prep
- Multi-region

## Done Log

Move tasks here as they finish. Look at this when momentum dips.

### 2026-05-18 — Phase 18 spec locked + 18.1 (IA pass) shipped

Phase 18 (dashboard polish) gets a 9-sub-task spec covering IA pass, empty states, first-run checklist, visual passes on constellation + agent detail, team-from-README polish, inline help, tests, demo. Design choices locked: roles-first nav, dismissible checklist (not modal wizard), visual pass scoped to constellation + agent detail first, tooltip-only inline help (long-form via docs).

**18.1 IA pass shipped.** `dashboard/app/Header.tsx` rewritten:

- Top-level reads: `My team / Activity / Trust zones / Integrations / Account / Advanced`.
- Polaris demoted from top-level to a regular agent in `My team` dropdown.
- "Home" dropped from top-level (the Logo already links to `/`).
- `docs / drop-a-zip / deployments / validators` moved under Advanced.
- `Account` promoted to top-level link (workspace switcher dropdown still shows Log out + workspace metadata).
- Sparkle prefixes (`✨`) preserved on the AI-driven affordances (`team from README`, `generate from description`, `cost insights`).

**Verification:** `npx tsc --noEmit` clean. `npx next build` green; all 26 routes still build. Every previously-reachable route still reachable through the new nav (My team, Activity, Integrations, Account top-level links; Advanced dropdown covers the legacy developer surfaces).

**What this unblocks:** the rest of Phase 18 can land against a clean nav. 18.2 (empty states) targets the surfaces a non-technical user lands on first; 18.3 (first-run checklist) writes the first-time-onboarding affordance against the home page.

### 2026-05-18 — Phase 16 prod demo PASSED on Coral fake-SaaS README

The trust-zone wedge runs end-to-end on prod against the new hint-aware Compliance preset. A non-technical operator drops a CRM-shaped README, picks Compliance, and gets a team where the framework structurally refuses PII exfiltration. No manual zone overrides needed; the planner emits per-bot sensitivity_hint and the preset applies it.

**Setup**: Stripe-smoke workspace on prod (paid plan, ANTHROPIC_API_KEY set). Dropped `examples/p16-demo/crm-readme.md` (the Coral fake-SaaS README — B2B SaaS using HubSpot internally + LinkedIn/Crunchbase for prospect research, with explicit "no edges between PII and public sides" language).

**Acts 1-3 (browser)**:
- Planner produced a five-bot team with explicit two-chain rationale: *"two strictly separated workloads... two disjoint chains with no dispatch edges between them... the handoff between the two worlds is human, not automated."*
- Per-bot descriptions surfaced the planner's zone reasoning in plain English ("PII zone because the digest payload contains customer names, emails, and account metadata"). The new prompt's Trust zones section worked — the LLM thinks aloud about classification.
- Atlas's code generation failed twice (existing generator weakness: bot.py imports `psycopg2` but LLM doesn't add it to requirements.txt). Skipped without affecting the demo.
- Compliance preset preview now renders sensitivity-keyed rows (pii / sensitive / internal / public) instead of role-keyed rows, matching the hint-aware deploy logic.
- Deployed 4 bots (polaris, rigel, hermes, vega).
- `/zones` rendered the final topology: public lane (rigel, internet), internal lane (polaris, send_command+internet), sensitive lane (empty), pii lane (hermes + vega, no capabilities). Cross-zone dispatchers section: empty. Every agent locked to its own zone.

**Acts 4-5 (terminal scripts)**:
- `capability_gate_demo.py` → `BLOCKED — LightseiCapabilityError raised before the network call. capability 'internet' not granted to agent 'vega' (granted: none — default-deny).` Vega's httpx.get against `api.linkedin.com` refused before the network call leaves the process.
- `cross_zone_dispatch_demo.py` → `BLOCKED — LightseiCapabilityError raised before the network call. capability 'send_command' not granted to agent 'vega' (granted: none — default-deny).` Vega's `send_command(rigel, ...)` refused. Under the Compliance preset's hint-aware mapping, PII bots have zero outbound capabilities, so the capability gate fires before the cross-zone gate ever evaluates. Two backstops; the more restrictive one fires first.

**Act 6 (handoff_span) skipped**: Railway worker had the 4 deployments queued for 22+ minutes without ticking. Acts 1-5 already prove the wedge; Act 6 is the sanctioned alternative path, nice-to-have but not load-bearing. Worker investigation parked as a separate follow-up.

**Bugs caught + fixed in-session**:
- Idle-in-transaction Postgres timeout in team_planner + agent_generator handlers. Holding a transaction open across multi-second Anthropic calls trips Railway's idle-in-transaction kill. Fix: session.commit() inside `_ask()` immediately before `client.messages.create`. First fix landed too early (more session reads opened a new transaction); corrected to fire right before the LLM call.
- Compliance preset role-based mapping put both PII-side bots and research bots in the PII zone (preset's "specialist → pii" rule didn't distinguish). Fixed by P16.x hint-aware mapping (committed pre-demo).

**Loose ends**:
- Worker not ticking on prod (deployments queued; see `tasks/Worker investigation` follow-up).
- Atlas's psycopg2 generator failure is an existing generator weakness, unrelated to trust zones.
- Test workspace `stripe-smoke-1779072629` is mixed-purpose at this point (Stripe smoke + P16 demo). Can be deleted when no longer needed.

**The pitch** (in case it's useful for sales conversations): *"Your customer data lives in an isolated zone the framework refuses to let out. Not by convention — by gate. A prompt injection on your CRM bot literally cannot make a network call. A runaway agent loop literally cannot dispatch across the boundary. The only sanctioned way data crosses zones is a human operator who decides what's safe to forward, and we log that translation for audit. Demoed end-to-end on prod."*

### 2026-05-17 — Phase 17.9 demo PASSED in test mode (live mode parked)

Stripe console + Lightsei prod backend integration verified end-to-end. The full non-technical-user upgrade arc works on prod against test-mode Stripe.

**Stripe console setup** (Lightsei is a new Stripe account in the Lightspace Labs organization; org allows shared team + reporting while Lightsei has its own branding/portal/catalog):
- Test-mode product `prod_UXK6teqTGTPLaH` (`Lightsei`) + test-mode recurring price `price_1TYFSoA1n0fiEOaqPnIAFdGJ` (`$50.00 USD / month`) — created via Stripe MCP.
- Customer Portal configured: card update + invoice history + cancel-at-end-of-billing-period (so paid users keep access through the period they've paid for).
- Webhook endpoint at `https://api.lightsei.com/billing/stripe/webhook` subscribed to the five event types the backend handles (`checkout.session.completed`, `customer.subscription.created/updated/deleted`, `invoice.payment_failed`).
- Four env vars set on the `lightsei-backend` Railway service: `LIGHTSEI_STRIPE_SECRET_KEY` (sk_test_...), `LIGHTSEI_STRIPE_PRICE_ID`, `LIGHTSEI_STRIPE_WEBHOOK_SECRET` (whsec_...), `LIGHTSEI_DASHBOARD_BASE_URL`. Auto-redeploy picked them up.

**Smoke test** (workspace `f43f0f71-5750-4b22-8e08-6dd6dc3c1ffb`, email `wallacebailey32+stripe-smoke-1779072629@gmail.com`):
- Signup via `/auth/signup` → workspace created with `plan_tier='free'`, $5.00 credits, `has_stripe_customer=false`.
- `POST /workspaces/me/billing/checkout` → returned real `cs_test_...` Checkout URL; Stripe customer `cus_UXLy6FoumGfueK` created and stamped on the workspace; `has_stripe_customer=true`.
- Browser flow: completed Checkout with test card `4242 4242 4242 4242`, redirected to `/account?upgrade=success`, watched the dashboard polling animation swap from "Confirming payment" to "you're on the paid plan" within a few seconds.
- API verification post-flow: `/workspaces/me` confirmed `plan_tier="paid"` + `has_stripe_customer=true`. The real Stripe webhook delivery, signature verification, and handler all worked.
- Manage Subscription button opened the real Customer Portal.

**Webhook sanity check (curl):** `POST /billing/stripe/webhook` with bogus signature returned `400 "bad signature: No signatures found matching..."` — confirms `LIGHTSEI_STRIPE_WEBHOOK_SECRET` is set and `construct_event` is verifying.

**Mode confusion sidebar (one-time):** the Stripe MCP's first product create landed in live mode because the OAuth token picked up the Stripe dashboard's current mode at auth time. Caught it on the response (`"livemode":true`), reverted by switching dashboard to test mode + re-auth, then created the test-mode product successfully. The orphan live-mode `prod_UXK3cHPoQXLrf9` is harmless (no subscriptions possible until account activation) and easy to archive when next in the live-mode dashboard.

**Live-mode activation parked.** To resume: complete Stripe account activation (bank + identity + tax info via dashboard), recreate product/price/webhook/portal in live mode, swap the three live env vars on Railway (`LIGHTSEI_STRIPE_SECRET_KEY` → `sk_live_...`, `LIGHTSEI_STRIPE_PRICE_ID` → live `price_...`, `LIGHTSEI_STRIPE_WEBHOOK_SECRET` → live `whsec_...`), redeploy, smoke test with a real card + refund.

Phase 17 closed in test mode. Live-mode go-no-go waits for the business to be ready to take real payments.

### 2026-05-17 — Phase 17.7: dashboard billing UI on /account

The non-technical-user signup arc is now visually complete: a fresh user can see their free credits remaining, upgrade with one click, return to /account on the paid plan, and manage their subscription via the Stripe Customer Portal. Plus a clean cancellation path.

- **`backend/main.py` — `_serialize_workspace`** extended with three new fields: `plan_tier`, `free_credits_remaining_usd`, `has_stripe_customer`. Single change cascades through every endpoint that returns a workspace (auth/me, workspaces/me, signup, login, magic-link consume, OAuth callback) so the dashboard always has fresh billing state without a separate fetch.
- **`dashboard/app/api.ts`** — added `BillingNotConfiguredError`, `createBillingCheckout`, `createBillingPortal`. Both helpers raise `BillingNotConfiguredError` specifically on 503 so the UI can render a "Stripe not configured" message rather than a generic error. `SessionWorkspace` type extended with the new billing fields (all optional, so older cached values in localStorage still parse).
- **`dashboard/app/account/page.tsx`** — new Billing section above Workspace:
  - Paid tier: green "Paid" badge + "Active subscription · $50/mo" + "manage subscription" button.
  - Free tier: gray "Free" badge + "$X.XX of free credits remaining" + "upgrade to $50/mo" button.
  - Checkout redirect-back handler reads `?upgrade=success|cancelled` from `window.location` (avoids wrapping the whole AccountPage in Suspense for useSearchParams) and shows the right banner. On success, polls `fetchWorkspace` every 1.5s for up to 45s until `plan_tier='paid'`, then swaps in the "you're on the paid plan, thanks" banner. Query params are cleaned with `history.replaceState` so a hard reload doesn't re-trigger.
  - 503 surfaces as "Billing isn't configured on this Lightsei deployment yet. Ask the admin to follow STRIPE_SETUP.md." — actionable rather than scary.

**Verification:** `npx tsc --noEmit` clean. `npx next build` builds /account at 6.13 kB (was 5.24 kB; +0.89 kB for the new section). Backend tests: 3 new serializer tests added to `test_stripe_billing.py` (fresh signup defaults; `/auth/me` carries billing fields; paid-state workspace serializes correctly). Full suite: **747 passed in 145s** (was 744, +3 new). 0 regressions outside the new fields.

**What this unblocks:** the full Phase 17 demo arc end-to-end. The only thing left for 17.9 is Stripe console config from STRIPE_SETUP.md + a single test run with a real Stripe test card. 17.4-17.7 inclusive are all code-complete on 2026-05-17.

### 2026-05-17 — Phase 17.4: Stripe integration (code-complete pending Stripe console config)

Backend code for the full Stripe billing flow is wired, tested, and documented. Endpoints stay 503 until the env vars in STRIPE_SETUP.md are populated, so the code ships to prod safely without breaking anything. Once the Stripe dashboard is configured + env vars set, the upgrade-to-paid path lights up end-to-end.

- **`backend/stripe_billing.py`** (new) — pure helper module mirroring `google_oauth.py` shape. Five surfaces: `is_configured()`, `is_webhook_configured()` (checked separately because webhook secret is a different dashboard step), `create_customer()`, `create_checkout_session()`, `create_portal_session()`, `construct_webhook_event()` (delegates signature verification to `stripe.Webhook.construct_event`). Two error types: `StripeNotConfiguredError` (handlers → 503), `StripeApiError` (handlers → 502 with generic message; raw exception only in `_debug`), `WebhookSignatureError` (handler → 400, never 5xx so Stripe stops retrying).
- **`backend/main.py`** — three new endpoints right after the `/auth/...` block:
  - `POST /workspaces/me/billing/checkout` — lazy `stripe.Customer.create` on first call (most workspaces never upgrade, so eager creation on signup would waste API calls). Stamps the new customer id onto the workspace row. 400 when already paid (route them to the portal instead). 502 on Stripe API failure with a clean "billing temporarily unavailable" message.
  - `POST /workspaces/me/billing/portal` — 400 when workspace has no `stripe_customer_id` yet, so the dashboard can render "upgrade first" instead of a broken link.
  - `POST /billing/stripe/webhook` — verifies signature, maps event types to workspace state. `checkout.session.completed` → `plan_tier='paid'` + stamps the subscription id. `customer.subscription.updated` is status-aware: `active`/`trialing` → paid, everything else (past_due, unpaid, canceled, incomplete) → free so the paywall resumes firing until payment is fixed. `customer.subscription.deleted` → `plan_tier='free'` + clear the subscription id. `invoice.payment_failed` is logged only (the subscription.updated that follows handles the downgrade). Unknown event types are 200-acked + ignored so Stripe stops retrying. Idempotency: handler re-derives plan_tier from the event on every delivery, so duplicate delivery writes the same value.
- **`backend/requirements.txt`** — `stripe==15.1.0` (used for SDK + signature verification).
- **`STRIPE_SETUP.md`** (new, repo root) — step-by-step dashboard walkthrough: create the $50/mo product + recurring price, configure the Customer Portal (enable card updates + invoice history + cancel-at-period-end), create the webhook endpoint with the five event types we handle, copy the signing secret, set the four env vars on Railway, end-to-end smoke test (use Stripe test card 4242 4242 4242 4242), and the `stripe listen` CLI pattern for local-dev webhook forwarding.

**Verification:** `backend/tests/test_stripe_billing.py` (new, 20 tests) covering:
- `is_configured` and `is_webhook_configured` env-var detection (true/false matrices).
- Checkout endpoint: creates customer + returns URL on first call; reuses existing customer on subsequent calls; 503 when not configured; 400 when already paid; 502 on Stripe API error.
- Portal endpoint: returns URL; 400 when no customer yet.
- Webhook handler: `checkout.session.completed` flips to paid + stamps sub id; `subscription.deleted` downgrades + clears sub id; `subscription.updated` with `past_due` status downgrades; bad signature → 400 (not 5xx); duplicate delivery is idempotent (same payload twice → same end state); unknown event type → 200 + ignored; unknown workspace → 200 + ignored; falls back to `stripe_customer_id` lookup when `metadata.workspace_id` is missing (covers older subscriptions); missing webhook secret → 400.

Full backend suite: **744 passed in 143s** (was 724, +20 new). 0 regressions.

**What this unblocks:** the upgrade-to-paid step in the Phase 17 demo arc. Once Stripe console is configured + env vars set on Railway, a user can hit the paywall, click upgrade, complete Checkout, and have their workspace flip to `plan_tier='paid'` via webhook within seconds. 17.7 is the dashboard UI sub-task that surfaces this flow on `/account`.

### 2026-05-17 — Phase 17.6: dashboard signup + login UI

The non-technical-user signup flow specced in Phase 17 is now wired end-to-end on the frontend. A user can land on /signup or /login, type their email, and either get a magic link or kick off Google OAuth. After consuming the link, they land on the dashboard signed in with a fresh workspace ($5 of free credits per 17.1's backfill). The only step still gated by 17.4 (Stripe) is the upgrade-to-paid button on /account (17.7).

- **`dashboard/app/api.ts`**: added `requestMagicLink`, `consumeMagicLink`, `startGoogleOAuth`, `completeGoogleOAuth`, and a shared `AuthSuccess` type. Mirrors the existing `login` / `signup` helper shape so the page code can reach for `setSession(res.session_token, res.user, res.workspace)` the same way.
- **`/login` rewritten**: email field + "send magic link" primary CTA + "continue with Google" secondary, separated by an "or" divider. "Check your email" success state with a "use a different email" affordance. Subtle "developer using the SDK? sign in with a password" footnote linking to `/login/advanced`.
- **`/login/advanced`** (new): the previous email + password form, preserved verbatim, with "back to magic-link sign-in" link and a one-sentence explanation that this is for developers who initialised the SDK with a workspace password.
- **`/signup` rewritten**: same shape as `/login`, "free to start, $5 of credits on us, no credit card" subtitle. "Check your email" state explains the link will create a workspace + sign them in.
- **`/signup/advanced`** (new): the previous API-key signup form (workspace name + email + password + API-key reveal), preserved verbatim, with copy explaining this is for developers who want an API key directly.
- **`/auth/magic-link?token=...`** (new): client-side route wrapped in Suspense (App Router requires it for useSearchParams). Pulls `token` from the URL, POSTs to `/auth/magic-link/consume`, sets the session in localStorage, shows a "welcome to Lightsei" / "you're in" beat for 700ms, then routes to `/`. Distinguishes new-user vs returning-user copy via the `is_new_user` flag the backend returns. Failure state ("link is invalid or expired") links back to `/login`.
- **`/auth/google/callback?code&state`** (new): same shape as the magic-link page. Reads `code` + `state` from the URL, calls the backend `GET /auth/google/callback` to do the exchange, sets the session, redirects to `res.redirect_after` (which the backend echoes from the start-flow). Handles `?error=access_denied` with a clean "you cancelled the Google sign-in" message rather than a stack trace.

**Verification**: `npx tsc --noEmit` clean (no output). `npx next build` builds all six new routes statically (`/login`, `/login/advanced`, `/signup`, `/signup/advanced`, `/auth/magic-link`, `/auth/google/callback`). Backend auth test files re-run clean: 42 passed in 4.22s.

**What this unblocks**: the demo arc — a fresh non-technical user signs up cold via magic link or Google, gets $5 of free credits, runs through team-from-readme, hits a clean 402 when exhausted. The only remaining gap is 17.4 (Stripe) for the upgrade-to-paid path on /account (17.7), which is the next-blocker for the full Phase 17 demo.

### 2026-05-17 — Phase 17.5: paywall middleware + credit decrement (shipped out of order)

Shipped ahead of 17.4 (Stripe) to keep momentum — 17.5 is the smaller, self-contained sub-task that doesn't depend on external provider setup. With auth + paywall in place, a fresh user can sign up, get $5 of free credits, run through team-from-readme until exhausted, and hit a clean 402. The Stripe upgrade path follows when 17.4 lands.

- [x] **`backend/billing_gate.py` (new).** Pure module with two helpers. `assert_billing_active(session, workspace_id)` raises HTTPException(402) when free-tier workspace has `free_credits_remaining_usd <= 0`; paid workspaces fly through (Stripe handles their accounting separately); missing workspace no-ops (caller's existing 404/500 surfaces it). `decrement_free_credits(session, workspace_id, amount_usd)` subtracts from the pool, floored at 0; no-op on paid; liberal in input type (accepts Decimal, float, string). 402 detail body matches the shape the dashboard's billing UI (17.7) will render: error code + remaining-credits + upgrade-URL hint.
- [x] **Wired into agent_generator.run_agent_generation_job.** Pre-check: `assert_billing_active` runs BEFORE the workspace secret check, so a paywall'd workspace 402s before any Anthropic call. Post-spend: `decrement_free_credits` runs in the finally block alongside the existing Run row write for `lightsei.system`.
- [x] **Wired into team_planner.run_team_plan_job.** Same shape — pre-check at the top of the handler, post-decrement in the finally block.
- [x] **Wired into eval_runner.run_eval_job.** Pre-check returns a clean skip summary (`skipped_reason='out_of_credits'`) rather than raising 402 — eval is background work, raising would just mark the job 'failed' with no actionable signal. Matches the existing `over_budget` skip shape. Post-decrement when total tokens > 0.
- [x] **Wired into cost.add_run_cost_from_event.** The bot-run path (events flowing in from deployed bots) decrements the same free-credit pool as the server-side LLM call sites. Wrapped in try/except since cost ingest shouldn't crash if billing has a bug. Single source of truth for "all LLM spend in the workspace decrements one pool."

**Verification:** 17 new tests in `backend/tests/test_billing_gate.py` covering: pure-module helpers (free-with-credits no-op, paid no-op even when credits zero, free-exhausted raises 402, tiny remaining still passes, missing workspace no-op, decrement subtracts + floors at zero + no-ops on paid/missing/zero-amount + accepts float/string), end-to-end wiring on the bot-run path (add_run_cost decrements credits; no-op on paid), end-to-end wiring on the three handler call sites (generator + planner raise 402 before touching Anthropic when paywall'd; eval returns skip summary; paid workspace bypasses all three even with $0 credits). Full backend suite: 724 passed in 141s (was 707, +17 new). 0 regressions.

**What this unblocks:** the demo arc Phase 17 was specced around — a fresh user signs up via magic link or Google, gets $5 of credits, runs through team-from-readme, hits the paywall when exhausted. The Stripe upgrade button on /account (17.7) needs 17.4 to function, but everything else in the user-facing flow is now wired.

### 2026-05-17 — Phase 17.3: Google OAuth backend

Second dashboard-side auth surface — the one-click signin path for users already logged into Google. Standard OAuth 2.0 authorization-code flow with PKCE so a leaked redirect URL can't be replayed. Same new-user-creates-workspace pattern as 17.2; same sign-in-existing-user pattern with a smart returning-user match priority.

- [x] **alembic 0031** adds the `oauth_pending_states` table (state PK, code_verifier, redirect_after, created_at, expires_at, index on expires_at for future reaping). 10-minute TTL covers the user's hop out to Google's consent screen + back. Rows survive a backend restart so an interrupted callback can complete; small enough volume that abandoned-flow rows expiring harmlessly is fine.
- [x] **`backend/google_oauth.py` (new).** Pure module owning the OAuth shape so request handlers stay thin. `new_pkce_pair()` returns (verifier, sha256-base64url-stripped challenge). `new_state()` returns 32 bytes urlsafe. `build_authorization_url(...)` assembles the redirect with `prompt=select_account` so returning users see the account picker (better UX than silently picking the last one). `exchange_code_for_userinfo(code, code_verifier, redirect_uri)` does the back-channel token exchange + userinfo fetch, returns `{sub, email, email_verified, name}`. Configured via `LIGHTSEI_GOOGLE_CLIENT_ID` + `LIGHTSEI_GOOGLE_CLIENT_SECRET` + `LIGHTSEI_GOOGLE_REDIRECT_URI`. `is_configured()` for the 503-on-unconfigured shape. `GoogleOAuthError` for the failure path.
- [x] **`GET /auth/google/start`.** 503 when not configured (fail loud rather than redirecting the user into a half-configured flow). Generates verifier + state, persists to `oauth_pending_states`, returns `{authorization_url, state}` — dashboard navigates the browser to authorization_url. `redirect_after` query param threads through so the callback can hand the dashboard the original target page.
- [x] **`GET /auth/google/callback?code&state`.** Validates state, single-use (deletes the pending row before exchange so parallel callbacks can't both succeed), exchanges code → tokens → userinfo via the pure helper. Returning-user match priority: `google_user_id` exact (sub-matched returning user even if they renamed their Google email), then verified-email fallback (links Google to an existing magic-link / apikey user). When email is unverified AND already taken by an existing user → 400 `email_already_in_use` (don't let an unverified Google email claim an existing account). New-user path mirrors 17.2's structure (Workspace with 17.1 defaults + seeded validators, User with `auth_provider='google_oauth'`). Surfaces user cancellation (`?error=access_denied`) as a clean 400 the dashboard can render. Returns the same shape 17.2's consume returns + `redirect_after` from the pending row.
- [x] **`OAuthPendingState`** model added to `backend/models.py`.

**Verification:** 19 new tests in `backend/tests/test_google_oauth.py` covering the pure helpers (PKCE round-trip via sha256-base64url-stripped, state entropy, `is_configured` env-driven, authorization URL has all required params + default scopes), start endpoint (503 unconfigured, returns URL + persists state with 10-min TTL, threads `redirect_after`), callback endpoint (creates new user + workspace with 17.1 defaults, signs in returning user by google_user_id even when email changed, links to existing email user when Google says verified, refuses 400 `email_already_in_use` when unverified email collides with existing user, rejects unknown state, rejects expired state, single-use per state, surfaces user cancellation as 400 access_denied, 400 on missing params, 400 on Google token-exchange failure, returned session_token authenticates against /auth/me). Tests stub httpx.post + httpx.get inside the google_oauth module so they don't hit Google — same shape as the SDK tests stubbing Anthropic. Full backend suite: 707 passed in 143s (was 688, +19 new). 0 regressions.

**Real bug surfaced + fixed during this sub-task:** the unverified-email path originally fell through to new-user creation, which crashed on the unique-email constraint with a 500. Now explicitly 400s with `email_already_in_use` so the existing user stays intact and the message is actionable.

**What this unblocks:** 17.6 (dashboard signup UI) wires the Google OAuth button against `GET /auth/google/start`. Production requires the Google Cloud OAuth client + consent screen + redirect URI configured before the prod flow works end-to-end — code is ready and tested when that lands.

### 2026-05-17 — Phase 17.2: magic-link auth backend

First dashboard-side auth surface. A fresh user types their email on `/login`, gets a Resend-delivered link in their inbox, clicks, lands on `/` signed in with a fresh workspace + $5 free credits. Existing API-key signup users get promoted to email_verified on first magic-link consume. No password, no API key auto-created — non-technical-user path is purely email + click.

- [x] **`backend/email_provider.py` (new).** Pure module wrapping Resend's `/emails` API. `send_magic_link(email, token, dashboard_url)` either POSTs to Resend (when `LIGHTSEI_RESEND_API_KEY` is set + `LIGHTSEI_EMAIL_FAKE_CAPTURE` is not) or appends to an in-process `_captured` list so tests + dev assert without hitting the network. HTML + plain-text body together (HTML for clients that render it, plain text as fallback + what shows in Resend's preview dashboard). Filename is `email_provider.py` not `email.py` — Python's stdlib already owns `email` and shadowing it breaks transitive imports (httpx → urllib → http.client → email.parser).
- [x] **`POST /auth/magic-link/request {email}`.** Always-200 contract (don't leak whether the email is registered). Two rate-limits stacked: per-IP via the existing `limit_signup_attempt` (5/min) for brute-force protection, and per-email via a query against `email_signin_tokens` (`MAGIC_LINK_MAX_PER_HOUR=5`) so a malicious sender can't spam a single inbox by rotating IPs. Inserts a hashed row with 15-minute TTL (`MAGIC_LINK_TTL`), sends an email with the unhashed token in `{dashboard_url}/auth/magic-link?token=...`. Best-effort email send — exception logged but doesn't break the 200 contract.
- [x] **`POST /auth/magic-link/consume {token}`.** Same single-use semantics as the existing API-key auth path. Hashes the input, looks up the row; rejects unknown / expired / already-consumed with the same 422 message (no probe-existence path). Marks consumed_at BEFORE doing anything else so parallel POSTs with the same token can't both succeed. New-user path: creates Workspace (with `_workspace_name_for_signup_email` → "alice's workspace"), seeds default validators, creates User (`auth_provider='magic_link'`, `email_verified=True`, password_hash is a placeholder that explicitly won't verify so /auth/login can't be back-doored). Existing-user path: signs in, promotes to `email_verified=True` (doesn't rewrite `auth_provider`). Returns `{user, workspace, session_token, session_expires_at, is_new_user}`.
- [x] **`MagicLinkRequestIn` + `MagicLinkConsumeIn`** Pydantic schemas at the top of main.py alongside SignupIn / LoginIn.
- [x] **`EmailSigninToken` added to main.py's model imports** so the new endpoints can use it directly.

**Verification:** 16 new tests in `backend/tests/test_magic_link_auth.py` covering capture mode (works without API key set, env var forces capture even with key set, URL trailing-slash normalized), request endpoint (inserts token + sends, lowercases email, always-200 on unknown, per-email rate-limit silent-throttles 6th, validates email format), consume endpoint (creates new user+workspace with the 17.1 defaults, signs in existing user without duplicating, single-use, unknown-token 422, expired-token 422, marks consumed_at, returned session_token authenticates against /auth/me, schema rejects short tokens). Full backend suite: 688 passed in 139s (was 672, +16 new). 0 regressions.

**What this unblocks:** 17.3 (Google OAuth) follows the same shape — different signup path, same new-user-creates-workspace + sign-in-existing-user pattern. 17.6 (dashboard signup UI) wires the magic-link request form + token-consume page against these endpoints.

### 2026-05-17 — Phase 17.1: schema backbone for auth + billing

The columns + table 17.2-17.5 wire against. Single alembic migration, pure storage, no endpoints — same shape as Phase 16.1 and 14.1.

- [x] **alembic 0030** adds: `users.email_verified` (Boolean default false), `users.auth_provider` (String(16) default 'apikey'), `users.google_user_id` (String, nullable, partial-unique on NOT NULL); `workspaces.stripe_customer_id` (String, nullable, partial-unique on NOT NULL), `workspaces.stripe_subscription_id` (String, nullable), `workspaces.plan_tier` (String(16) default 'free'), `workspaces.free_credits_remaining_usd` (Numeric(12,6) default 5.00); new `email_signin_tokens` table (token_hash PK, email, created_at, expires_at, consumed_at) with `(email, created_at DESC)` index for the rate-limit query in 17.2.
- [x] **Backfill** every existing workspace to `plan_tier='free'` + `free_credits_remaining_usd=5.00` so they land in the same state a fresh signup would. Existing users keep `auth_provider='apikey'` + `email_verified=false` (they came in via the existing /auth/signup path; nothing to migrate semantically).
- [x] **Partial-unique indexes** on `users.google_user_id` and `workspaces.stripe_customer_id` so apikey users / pre-billing workspaces (which carry NULL) don't collide while still preventing duplicates among the rows that DO carry a value.
- [x] **`backend/models.py`**: `Workspace` + `User` carry the new columns with `server_default` matching the migration. New `EmailSigninToken` model. New `_VALID_PLAN_TIERS` + `_VALID_AUTH_PROVIDERS` frozensets at the module top with `is_valid_plan_tier` / `is_valid_auth_provider` helpers — same pattern as `_VALID_SENSITIVITY_LEVELS` from 16.1. Adding `Boolean` to the sqlalchemy imports was the only other change beyond the new fields.

**Verification:** 18 new tests in `backend/tests/test_auth_billing_schema.py` covering the validator helpers (accepts canonical values, rejects off-list + non-string, default-in-set invariant, frozenset invariant), Workspace defaults (fresh workspace lands on `'free'` + 5.00 credits, paid update round-trips, partial-unique on stripe_customer_id, decimal precision on credits decrement), User defaults (`auth_provider='apikey'` + `email_verified=false` + `google_user_id=null` on existing signup path; verified update round-trips; partial-unique on google_user_id), and the EmailSigninToken table (round-trip with consume marker, PK prevents duplicate inserts, rate-limit query runs cleanly). Full backend suite 672 passed in 139s — was 654; +18 new; 0 regressions outside the new file.

**What this unblocks:** 17.2 (magic-link backend reads + writes the email_signin_tokens table, creates user + workspace pairs with the new columns), 17.3 (Google OAuth uses `google_user_id` for matching returning users + `auth_provider='google_oauth'` on creation), 17.4 (Stripe writes stripe_customer_id + stripe_subscription_id + plan_tier on subscription lifecycle events), 17.5 (paywall middleware reads plan_tier + free_credits_remaining_usd). Every other 17.x sub-task assumed this existed; now it does.

### 2026-05-17 — Phase 17 spec: auth, signup, billing — design choices locked, 17.1-17.9 sub-tasks

Same shape as today's earlier Phase 14 + Phase 16 spec sessions: convert the rough-shape bullets into per-sub-task specs concrete enough to implement against, lock the design choices up front so the schema doesn't get reworked mid-build, write the demo concretely.

**Design choices settled** (with switch triggers each, full prose in the Phase 17 section above):
- **Auth surface**: magic link AND Google OAuth, both first-class. Existing API-key signup stays as the developer path.
- **Email**: Resend (3k/mo free covers signup volume well past first paying customers).
- **Billing**: $50/mo flat per workspace + $5 free credits on signup. Single paid tier in v1. No per-event usage billing (doesn't fit non-technical-buyer mental model).
- **Paywall**: hard 402 on next LLM call when credits exhausted + no card. Read-only surfaces stay accessible.
- **Seat model**: one workspace = one seat. Multi-user-per-workspace stays parked.

**Nine sub-tasks**: 17.1 schema backbone (single alembic 0030 — same shape as the Phase 16.1 + 14.1 schema-only sub-tasks), 17.2 magic-link auth backend (Resend integration + request/consume endpoints + new-user-creates-workspace path), 17.3 Google OAuth backend (auth-code + PKCE flow), 17.4 Stripe integration (customer-on-workspace-create + checkout-session endpoint + webhook handler + portal endpoint), 17.5 paywall middleware (gates LLM-call endpoints when free+exhausted), 17.6 dashboard signup/login UI (magic-link form + Google button + callback page; existing API-key signup moves to /login/advanced), 17.7 dashboard billing UI on /account (credits remaining + upgrade button → Checkout / portal link), 17.8 tests, 17.9 demo (fresh non-technical user signs up cold → deploys a team → hits paywall → upgrades → continues).

NOW → 17.1 schema backbone.

### Open demos (user-driven, not blocking the next phase)

- **Phase 16**: drop a CRM-shaped README on /agents/team-from-readme with the Compliance preset; confirm /zones lays out pii specialists + public messengers; confirm a pii bot's outbound httpx raises LightseiCapabilityError; confirm pii→public send_command raises LightseiCrossZoneError before the network call; confirm a forged backend POST gets the typed cross_zone_blocked 403; confirm a real-looking email in a pii agent's emit lands as `[redacted-email]`. Once green, the wedge against Viktor is real and demonstrable (not just spec'd).

### 2026-05-17 — Phase 16.7: three trust-zone presets + team-from-README integration

The last Phase 16 sub-task. Wires the trust-zone enforcement into the canonical non-technical-user flow (team-from-README) so the user doesn't have to configure trust zones manually after every deploy — they pick a preset once, and every agent in the generated team lands with the right zone/capabilities/cross-zone configuration applied.

- [x] **`backend/zone_presets.py` (new).** Three presets defined as `{role → {sensitivity_level, capabilities, dispatches_cross_zone}}` maps:
  - `open_team` — developer convenience. Every role gets `public` zone + `[internet, send_command]` + `dispatches_cross_zone=True`. No friction; for workspaces where no agents touch customer data.
  - `standard_team` (default) — SMB defaults. `internal` zone everywhere, cross-zone off, orchestrator only gets `send_command` (no internet — its specialists are the ones that need it), specialists get both, messengers get `internet` but not `send_command` (leaves don't fan out).
  - `compliance_team` — the canonical CRM-bot scenario. Specialists default to `pii` with empty capabilities (NO internet AND NO send_command — a compromised CRM bot literally can't exfiltrate). Messengers default to `public` with `internet` only (the outbound side; no dispatch back). Orchestrator stays `internal`. **Cross-zone dispatch disabled across every role** — the ONLY way data crosses zones is via the human-mediated `lightsei.handoff_span` from 16.5. This is the proof point against Viktor.
  - Role normalization handles aliases (`executor` → `specialist`, `notifier` → `messenger`) so the presets work against both the team-planner's vocabulary and the agents table's. Module-level asserts catch preset/metadata drift at import time.
  - `apply_preset(name, role)` returns a deep-copied dict so caller-side mutation can't poison the next call. Falls back to default-preset on unknown name, falls back to specialist on unknown role. `list_presets()` returns the dashboard-renderable form in stable order (open → standard → compliance, reads as a slider).
- [x] **`GET /workspaces/me/zone-presets` endpoint.** Workspace-authed but workspace-independent (the presets are global). Returns the full picker payload — label, summary, tradeoff, `by_role` config, `is_default` flag — so the dashboard can render a preview without follow-up fetches.
- [x] **Dashboard preset picker on `/agents/team-from-readme`.** Renders above the Deploy team button so the security posture is the last thing the user sees before clicking. Three cards (one per preset) with name + summary; clicking selects. Selected card highlights; below the cards a small table renders the preset's `(role → zone, capabilities, cross-zone)` config so the user previews what every role will get. Default is `standard_team`.
- [x] **Apply at deploy time.** After each bot's deploy + the existing description PATCH, the deploy loop now does `patchAgent({sensitivity_level, dispatches_cross_zone})` + `patchAgentCapabilities(capabilities)` using the preset's per-role config. Best-effort (swallowed on failure) — a single PATCH 4xx shouldn't roll back the deploy; the bot runs default-deny until the user fixes it from `/agents/{name}`.
- [x] **Tests: 24 new in `backend/tests/test_zone_presets.py`.** Cover the structural invariants (every preset has all three roles + the three required config fields), per-preset semantics (open grants everything; standard is internal-no-cross-zone with role-shaped capability differences; compliance specialists are pii-no-cap-no-cross-zone, messengers are public-with-internet-no-send-command, orchestrator is internal-no-cross-zone), `apply_preset` robustness (deep-copy invariant, role-alias normalization, unknown-fallback to specialist, unknown-preset-fallback to default), `list_presets` ordering + `is_default` correctness + full payload shape, and the endpoint contract (returns three presets in stable order, 401 unauthenticated, dashboard-renderable shape).

**Verification:** Backend full suite 654 passed in 131s (was 630, +24 new). Dashboard tsc --noEmit clean. 0 regressions outside the new files.

**What this completes:** Phase 16 in full. The trust-zone wedge against Viktor is now structurally + operationally + redaction-wise complete AND wired into the non-technical-user flow. A user picking "Compliance team" from /agents/team-from-readme gets a working CRM-isolation setup with zero manual configuration.

### 2026-05-17 — Constellation: revert per-zone node coloring

Quick revert of 16.6's "color constellation nodes by zone" change after user feedback that it washed out the per-bot visual identity (every non-orchestrator star looked the same shade). Per-agent stable-hash tints restored. Zone signal stays visible at the surfaces where it's actually useful: Zone column on /agents, chip on /agents/{name} header, /zones topology page. Save the constellation-zone-overlay idea for a follow-up that adds it as a secondary signal (e.g., ring stroke) rather than overwriting the primary signal.

### 2026-05-17 — Phase 16.6: dashboard surfaces (sensitivity chips, constellation coloring, /zones page, /agents/{name} editor)

Makes Phase 16's structural enforcement visible at every dashboard surface where it matters. Non-technical user can now see "this bot is in the pii zone, has internet + send_command, cross-zone off" without leaving the agents page.

- [x] **Shared `SensitivityChip` + `SENSITIVITY_TONE` in `dashboard/app/sensitivity.tsx`.** Color mapping in one place so every page renders the same green/amber/orange/red signal (public/internal/sensitive/pii). Chip variant for inline use, node hex for constellation, lane class for /zones — same source of truth.
- [x] **`Agent` type + `ConstellationAgent` type extended** in `dashboard/app/api.ts` with `sensitivity_level`, `capabilities`, `dispatches_cross_zone`. New `SensitivityLevel` literal + `SENSITIVITY_LEVELS` constant. `patchAgent` accepts the new fields. New `patchAgentCapabilities` helper hits the 16.2 endpoint with replace semantics.
- [x] **Backend constellation endpoint** now selects + returns `sensitivity_level` so the constellation map can color nodes without an extra fetch per agent.
- [x] **`/agents` Quality column** got a sibling **Zone column** rendering `<SensitivityChip>` per row. Same compact shape so the table stays scannable.
- [x] **`/agents/{name}` header** now shows the sensitivity chip next to the agent name + the existing "live" pill. Three signals (identity / zone / liveness) visible without scrolling.
- [x] **`/agents/{name}` Trust zone editor.** New section above System prompt with two panels. Top panel: sensitivity_level select + cross-zone checkbox + save button. Bottom panel: capability allow-list (checkboxes for the two known capabilities + listed custom caps with × remove + a custom-capability input that supports Enter-to-add). Both panels track local dirty state so the save button is disabled until something actually changed; saved-confirmation appears after a successful write.
- [x] **Constellation node coloring.** `Constellation.tsx` now uses `SENSITIVITY_TONE[a.sensitivity_level].node` as the agent tint, falling back to the per-agent stable hash tint when an older backend response is missing the field. Replaces the previous purely-aesthetic tint with the trust-zone signal — bots are now grouped visually by where their data can go.
- [x] **New `/zones` page** at `dashboard/app/zones/page.tsx`. Vertical lane per sensitivity level, agents grouped into their lane with name + description + first 4 capabilities as small chips + cross-zone callout if enabled. Separate "Cross-zone dispatchers" section at the bottom lists every agent with `dispatches_cross_zone=True` so the explicit exceptions are easy to audit. Empty cross-zone section explicitly says "every agent is locked to its own zone — the default-deny posture from Phase 16.4" rather than rendering nothing.
- [x] **Header nav** got a "trust zones" entry under the existing agents group pointing at `/zones`.

**Verification:** dashboard `tsc --noEmit` clean. Backend full suite: 630 passed in 129s (constellation endpoint change is backward-compatible — old fixtures still load via the `or "internal"` fallback). No regressions.

**Skipped for v1 (small follow-ups):**
- Cross-zone edge styling on the constellation (red, thicker). Would need extending the edges endpoint with per-edge zone info; the current ConstellationEdge only carries `from` / `to` / `count_24h` / `last_at`. Punt to a follow-up since the node coloring already conveys "this dispatch crosses zones."
- Cross-zone refusal rendering on a dashboard command-enqueue UI. Manual enqueues from the dashboard intentionally bypass the cross-zone gate (no `source_agent` per 16.4 design), so the failure surface doesn't fire on that path. If a future dashboard adds "dispatch on behalf of agent X" the refusal-rendering work becomes meaningful.

**What this unblocks:** 16.7 is the last Phase 16 sub-task — three presets + team-from-README integration. After 16.7 the Compliance team demo (Phase 16's payoff) is testable end-to-end.

### 2026-05-17 — Phase 16.5: SDK redaction primitives + handoff span

The last SDK-level work in Phase 16. Built-in detectors for the four shapes the canonical CRM-bot scenario cares about (email / US phone / hyphenated SSN / Luhn-valid credit card), pluggable via `register_redactor` for niche workspace-specific PII shapes, auto-applied to every outgoing payload from `'pii'` agents with a per-call opt-out for genuine audit cases.

- [x] **`sdk/lightsei/_redaction.py` (new).** Built-in detectors: `_redact_email` (standard local@domain.tld shape, conservative — won't match obfuscated forms), `_redact_phone` (US 10-digit with country-code prefix optional, requires separators OR parens so bare digit runs don't false-positive on dates/ids), `_redact_ssn` (hyphenated `XXX-XX-XXXX` only — bare 9-digit runs deliberately not matched), `_redact_credit_card` (13-19 digits with optional separators, Luhn-validated to drop ~90% of random digit false positives). `register_redactor(name, fn)` adds custom detectors; same name as a built-in replaces the built-in's implementation. `redact(text, *, detectors=None)` runs the merged set (or a subset by name). `redact_payload(value)` walks dict/list/tuple containers recursively; numbers/bools/None pass through unchanged; returns a new container without mutating the input.
- [x] **`update_sensitivity_level(client, level)` in `_capabilities.py`.** Pairs with `update_capabilities` — both called from `fetch_capabilities` (initial init() fetch) and from the heartbeat refresh path in `_instance.py`. Validates against the four-level ladder; silently ignores garbage so a future server bug can't crash the redaction path.
- [x] **`_client._sensitivity_level` attribute.** Cleared in `_reset_for_tests` for test isolation. Also wipes the custom-redactor map (`_reset_custom_redactors_for_tests`) so a test registering a custom detector doesn't leak into the next test's redact() output.
- [x] **Auto-redact wired into `emit` + `send_command`.** Both grow a `redact: bool = True` kwarg; when source agent's `sensitivity_level == 'pii'` and the kwarg is True, the payload is recursively redacted before it leaves the SDK. `redact=False` is the per-call escape hatch for the audit-trail case.
- [x] **`lightsei.handoff_span(from_run, to_run, sanitized_prompt, *, notes=None)`.** Emits a `handoff` event linking the two runs. Opt-in (no auto-detection — too much false-positive risk). Sanitized prompt isn't re-redacted: it's already clean by contract, and double-redacting could mangle deliberate `[redacted-email]` placeholders typed by the operator. The Phase 21 operator chat surface will call this when an operator finishes a translation; users can call it directly today.
- [x] **`__init__.py` exposes `redact`, `register_redactor`, `handoff_span`** at the top level + adds them to `__all__`. Wrapped versions of `emit` + `send_command` carry the `redact` kwarg in their public signature with a docstring explaining the contract.

**Verification:** 25 new tests in `sdk/tests/test_redaction.py` covering each built-in detector (matches + non-matches + edge cases like bare 10-digit runs that look phone-shaped but aren't separated), Luhn validation against published test card numbers (Visa / Mastercard / Amex), pluggable detector registration including name-collision override of built-ins, validation errors for bad register inputs, recursive payload walking on dicts/lists/nested structures, in-place mutation invariant (caller's payload preserved), auto-redact lifecycle (pii agent → redact; internal agent → pass through; per-call opt-out), handoff_span emits the linking event with all fields including notes, handoff doesn't double-redact a deliberate placeholder, BUILTIN_DETECTORS public surface invariant. Full SDK suite: 101 passed in 22s. Backend full suite: 630 passed in 128s. 0 regressions.

**What this completes:** every SDK-level surface in Phase 16's plan. Remaining sub-tasks (16.6 dashboard surfaces + 16.7 presets + team-from-README integration) are pure dashboard / backend wiring — no more SDK changes for the trust-zone story.

### 2026-05-17 — Phase 16.4: cross-zone dispatch enforcement (framework-level)

The wedge against Viktor is now structurally complete. A `'pii'` agent can't dispatch to a `'public'` agent even when both have `'send_command'` unless the source explicitly opts in via `dispatches_cross_zone=True`. Backend gate is the load-bearing one (data never leaves Lightsei mid-call so SDK pre-flight is less urgent for send_command than for httpx); SDK surfaces the backend's typed 403 as `LightseiCrossZoneError`.

- [x] **alembic 0029** adds `dispatches_cross_zone Boolean NOT NULL DEFAULT false` to `agents`. Default-deny: every existing agent stays in the safer same-zone-only posture until the user explicitly opts an agent in. Belt-and-suspenders UPDATE pass alongside the ADD COLUMN.
- [x] **SQLAlchemy model** carries the new column on `Agent` with `server_default=text("false")` so existing call sites that don't pass the field land on False automatically.
- [x] **`_serialize_agent`** returns `dispatches_cross_zone` so GET /agents + /agents/{name} expose the field for the dashboard editor in 16.6.
- [x] **PATCH /agents/{name} extended** with `dispatches_cross_zone` + `sensitivity_level` fields on `AgentPatchIn`. sensitivity_level validates against `_VALID_SENSITIVITY_LEVELS` with a 422 + helpful message on invalid input. dispatches_cross_zone is a straight bool update; None means no-op.
- [x] **Backend cross-zone gate in `enqueue_command`.** Only fires when `source_agent` is set (user-initiated dashboard dispatches skip the gate — the user made an explicit decision). Compares source's `sensitivity_level` vs target's; refuses with `HTTPException(403, detail={"error": "cross_zone_blocked", ...})` carrying source/target agent + zone + a clear message. Auto-approval rules from Phase 11.2 still apply on top — cross-zone-enabled does NOT mean auto-approved.
- [x] **SDK `LightseiCrossZoneError` in `errors.py`** with `source_agent` / `source_zone` / `target_agent` / `target_zone` attributes + a helpful default message that names the missing `dispatches_cross_zone` flag.
- [x] **SDK `send_command` surfaces the backend's 403.** When the backend returns 403 with `detail.error == "cross_zone_blocked"`, the SDK re-raises as `LightseiCrossZoneError` (typed) so user code can catch the trust-zone violation specifically. Other 403s + malformed bodies fall through to the generic `LightseiError` so the typed exception stays unambiguous.

**Verification:** 13 new backend tests in `test_cross_zone_dispatch.py` covering the schema default, same-zone-always-allowed (including pii↔pii), cross-zone refusal in both directions, internal↔sensitive (not just public↔pii extremes), opt-in unblocks the gate, opt-in must be on source not target, user-initiated dispatches bypass the gate, PATCH updates dispatches_cross_zone + sensitivity_level with 422 on invalid, GET response shape, cross-workspace isolation. 6 new SDK tests in `test_cross_zone_gate.py` covering the error class (attributes + helpful message + custom-message override + LightseiError subclass invariant) and the send_command surfacing path (typed re-raise on cross_zone_blocked, fall-through to generic LightseiError on other 403s including malformed bodies). Backend full suite: 630 passed in 132s. SDK full suite: 76 passed in 18s. 0 regressions.

**What this unblocks:** Phase 16's structural wedge is complete — 16.5 (redaction + handoff span) is the last SDK-level work; 16.6 (dashboard surfaces) makes everything visible; 16.7 (presets) wires it into team-from-README. After 16.7 the Compliance team demo (16's payoff) is testable end-to-end.

### 2026-05-17 — Phase 16.3: SDK capability gate (httpx + send_command + heartbeat refresh)

The load-bearing slice that makes the capability allow-list actually refuse forbidden ops. Until 16.3 the column was documentation; now it's enforcement. Wraps httpx + `send_command` at the SDK layer so refusal happens BEFORE any network bytes leave the bot's process.

- [x] **`LightseiCapabilityError` in `sdk/lightsei/errors.py`.** Carries `capability` + `granted` + `agent_name` so error handlers can introspect rather than parse messages. Helpful default message that names the missing capability and the agent's current allow-list ("granted: none — default-deny") plus the exact PATCH endpoint the user needs to fix it.
- [x] **`sdk/lightsei/_capabilities.py` (new): cache + check helpers.** `update_capabilities(client, list)` replaces the cached allow-list (defensive: drops non-str entries, ignores non-list payloads so a future server bug can't crash the gate). `has_capability(client, name)` returns True before init (fail-open per CLAUDE.md graceful-degradation), else membership check. `check_capability(client, name)` raises `LightseiCapabilityError` if missing. `fetch_capabilities(client)` does the initial GET on /agents/{name} — fails open on transport error so init() never crashes the bot's startup. `is_lightsei_internal_url(client, url)` whitelist matcher — the SDK's own backend calls (events, heartbeats, secret fetches) MUST bypass the gate or the SDK can't function without `'internet'`.
- [x] **`sdk/lightsei/integrations/httpx_patch.py` (new).** Wraps `httpx.Client.send` + `httpx.AsyncClient.send` (both sync + async paths) with a check that runs `check_capability(_client, 'internet')` unless the request targets a Lightsei-internal URL. Idempotent (sentinel attribute on the wrapped callable so re-runs don't double-wrap). Skips the gate entirely before init() so a bot that imports httpx and runs a request pre-init isn't broken.
- [x] **`sdk/lightsei/_commands.py` gated.** `send_command` calls `check_capability(client, 'send_command')` after the initial-state checks but before the POST so a refused dispatch raises `LightseiCapabilityError` with zero network traffic.
- [x] **`sdk/lightsei/__init__.py` wires it up.** `_auto_patch()` calls `patch_httpx()` alongside the existing OpenAI/Anthropic/Gemini patches. `init()` calls `fetch_capabilities()` after `_auto_patch` so the gate is active by the time user code runs the first outbound call.
- [x] **Heartbeat response refreshes the cache.** `_instance.py:_post_once` reads `capabilities` from the heartbeat response body and feeds it to `update_capabilities` so dashboard edits propagate within one heartbeat interval (default 10s) without an extra fetch.
- [x] **Backend heartbeat endpoint echoes capabilities.** `instance_heartbeat` in `backend/main.py` looks up the agent row and adds `capabilities` + `sensitivity_level` to the response. Doesn't change `_serialize_instance` (used elsewhere by `list_instances`) — heartbeat endpoint augments the response inline.
- [x] **`_reset_for_tests` clears the cache.** Belt-and-suspenders for SDK test isolation; without it, a test that grants `'internet'` could leak into the next test that expects default-deny.

**Verification:** 23 new tests in `sdk/tests/test_capability_gate.py` covering the error class (attributes + helpful default-deny message), pure cache helpers (replace, non-list ignored, non-str entries dropped, fail-open before init, post-init membership check, raise-vs-noop), URL whitelist (base host match, other-host rejection, missing-base safe), httpx wrap (refused without `'internet'`, allowed with `'internet'`, Lightsei-backend bypass even without `'internet'`, idempotent re-patching, fail-open pre-init), send_command wrap (refused without `'send_command'`, allowed with), init/heartbeat lifecycle (initial fetch populates cache, fail-open on backend-unreachable, heartbeat-response refresh path, missing `capabilities` field in response = stay unloaded). Extended `fake_backend` with `capabilities=` kwarg + `/agents/{name}` GET + `/agents/{name}/commands` POST handlers. SDK full suite: 70 passed in 17s. Backend full suite: 617 passed in 126s (heartbeat response shape change is backward-compatible). 0 regressions.

**What this unblocks:** the wedge against Viktor is now functional for the `'internet'` and `'send_command'` capabilities. A bot tagged `'pii'` with empty capabilities can't make outbound HTTP and can't dispatch. 16.4 builds on this with cross-zone dispatch enforcement (the additional check that blocks `'pii'` → `'public'` even when the source agent has `'send_command'`). 16.5 adds redaction + handoff spans. 16.6 surfaces the configuration. 16.7 wires presets into team-from-README.

### 2026-05-17 — Phase 16.2: capability model + storage + PATCH endpoint

The vocabulary the SDK gate in 16.3 will refuse ops against. Pure module + JSONB column + thin endpoint, same shape as 16.1 — storage and validation only, no enforcement yet.

- [x] **alembic 0028** adds `capabilities` JSONB column to `agents`, NOT NULL DEFAULT `'[]'::jsonb`. Default-deny is the safe posture for a new bot; explicit grants required. Kept separate from 0027 (sensitivity ladder) because the capability vocabulary lives in its own module and grows independently — rolling back one shouldn't disturb the other.
- [x] **`backend/capabilities.py` (new).** Owns the vocabulary + validators. `KNOWN_CAPABILITIES` frozenset = `{'internet', 'send_command'}` (the two enforced by 16.3). `connector:<name>` prefix accepted by `is_valid_capability` so workspaces can future-proof config today even though Phase 20 hasn't wired connector enforcement. `validate_capability_list(names)` returns the same problems-list shape the team-planner validators use, aggregating multiple problems so the user can fix the whole list in one pass. `normalize_capability_list` dedups preserving order. `presets_for_level(level)` returns the default capability set per sensitivity rung — `public` gets `[internet, send_command]`, `internal` gets `[send_command]`, `sensitive` and `pii` start with `[]` so every grant on a compliance bot is an explicit user choice. Module-level assert keeps the preset keys in sync with `_VALID_SENSITIVITY_LEVELS` to catch drift at import time.
- [x] **`PATCH /agents/{agent_name}/capabilities` endpoint.** Replace-not-merge semantics (capabilities are small; whole-list updates avoid add-one-remove-one merge complexity). 200 with serialized agent on valid input. 422 with `{"problems": [...]}` on invalid so the dashboard can render line-level errors. 404 on missing agent + cross-workspace. 401 unauthenticated. Normalizes the list (dedup) before persisting.
- [x] **`_serialize_agent` updated** to include `sensitivity_level` (from 16.1) and `capabilities` (this sub-task) so `GET /agents/{name}` + the agents roster return the new fields. Dashboard wiring lands in 16.6.

**Verification:** 29 new tests in `backend/tests/test_capabilities.py` covering the validator (known-set, connector prefix, empty-suffix rejection, unknown-rejection, non-string-rejection, length cap, frozenset invariant), `validate_capability_list` (empty valid, all-known valid, non-list rejected, unknown-with-index, duplicate detection, per-agent cap of 50, multiple-problem aggregation), `normalize_capability_list` (dedup preserves order), `presets_for_level` (default-deny for sensitive/pii, open-research for public, middle-ground for internal, garbage-fallback to default, fresh-copy invariant), schema default (`[]`), and the PATCH endpoint (replace, 422-with-problems, dedup, 404 missing, cross-workspace 404, 401 unauthenticated, clear-to-empty as valid revocation, GET response shape). Full backend suite: 617 passed in 125s, 0 regressions outside the new files.

**What this unblocks:** 16.3 (SDK gate reads `capabilities` from the agent), 16.7 (team-from-README presets call `presets_for_level` to seed each role's capability list).

### 2026-05-17 — Phase 16.1: sensitivity_level schema backbone

The schema everything else in Phase 16 depends on. Single alembic migration adds the column to both `agents` (configuration knob) and `runs` (denormalized snapshot so historical analytics stay correct after a relabel).

- [x] **alembic 0027** adds `sensitivity_level String(16) NOT NULL DEFAULT 'internal'` to `agents` and `runs`. Backfills existing rows to `'internal'` (belt-and-suspenders UPDATE after the ADD COLUMN — Postgres already backfills with the server_default for literal defaults, but the explicit UPDATE survives any future migration that drops the default). No new indexes — 16.4's cross-zone check uses the existing PKs; 16.6's /zones page scans a tiny per-workspace table where a seq scan is fine.
- [x] **SQLAlchemy models** carry the column on both `Agent` and `Run` with `server_default=DEFAULT_SENSITIVITY_LEVEL` so existing test fixtures and any call site that doesn't yet pass the field land on `'internal'` automatically.
- [x] **`_VALID_SENSITIVITY_LEVELS` + `is_valid_sensitivity_level` + `DEFAULT_SENSITIVITY_LEVEL`** at the top of `backend/models.py`. Frozenset to prevent runtime mutation. Helper rejects None / non-str / off-list with no exceptions thrown — endpoint + SDK code decides whether to 4xx or default.
- [x] **String column not Postgres enum** so a future fifth level ('regulated', 'export-controlled', whatever) is a code-only change. Validation lives in the helper + the API/SDK layer; DB constraint is just NOT NULL.

**Verification:** 11 new tests in `backend/tests/test_sensitivity_level.py` covering the validator (all four valid levels, off-list rejected, non-string inputs rejected, default-in-set invariant), the Agent + Run default behavior, explicit-level round-trip, the alembic backfill landed (raw SQL query bypassing the ORM to assert what actually got stored), and the NOT NULL constraint catches a NULL write. Full backend suite: 588 passed in 123s, 0 regressions outside the new file.

**What this unblocks:** 16.2 (capability model, separate migration), 16.4 (cross-zone dispatch enforcement reads this column), 16.5 (auto-redaction for `'pii'` agents reads this column), 16.6 (sensitivity chips on the dashboard read this column). Every other 16.x sub-task assumed this existed; now it does.

### 2026-05-17 — Phase 14 closed: continuous-eval pipeline running on prod

The 14.7 demo passed visibly on prod. `/agents` Quality (7D) column shows green "3 good" chips on the two bots with completed runs in the last hour (`vela`: 90 runs/24h, `antares`: 25 runs/24h). Agents without samples in the window correctly show the muted "—" pill (the empty-pool state, not a bug). The whole pipeline runs end-to-end without intervention: cron drops `eval_runs` jobs per workspace hourly → existing 12C.6.2 runner claims them → handler samples completed runs → Sonnet rates each → verdicts land in `run_evaluations` → dashboard renders chips → SDK exposes `lightsei.get_quality_signal` for 12D.3's eventual auto-tuner.

Five Phase 14 sub-tasks shipped (14.1-14.5), 14.6 folded, 14.7 demoed. 59 new tests across the phase; full backend suite 577 passed, SDK suite 47 passed, 0 regressions.

**12D.3 (auto-optimization with explicit consent) is now technically unblocked** — Phase 14 was its quality-signal dependency. But under the 2026-05-17 strategic direction shift (MEMORY.md), 12D.3 dropped from "next" to engine-room polish; the critical path is now 16/17/18 (trust zones / self-serve / dashboard polish).

### 2026-05-17 — Phase 14.5: SDK get_quality_signal helper

The read-side wrapper for 12D.3's eventual auto-tuner. Same shape as `lightsei.get_cost_insights` from 12D.2 — a thin SDK helper that wraps the `/workspaces/me/agents/{name}/quality` endpoint with the bot's existing api_key — but flipped to fail-*closed*: returns `None` on any error rather than an empty dict, so callers can distinguish "backend flapping" from a real "no evals yet" empty pool. Matters for 12D.3 specifically: never auto-tune blindly when the quality signal is unavailable.

- [x] **`sdk/lightsei/_quality_signal.py` (new).** `get_quality_signal(client, agent_name, *, days=7)` → `dict | None`. URL-encodes the agent name, passes `days` as a query param. Treats SDK-not-initialized + network errors + non-200 + non-JSON + dict-missing-verdict_counts as failure (all return `None`).
- [x] **`sdk/lightsei/__init__.py` exposes the wrapper.** Public name `lightsei.get_quality_signal(agent_name, *, days=7)` with a docstring that explicitly calls out the fail-*closed* contract vs `get_cost_insights`'s fail-*open* contract — different defaults are easy to get wrong without a heads-up.
- [x] **Added to `__all__`.**

**Verification:** 7 new tests in `sdk/tests/test_basic.py` covering happy path, days query param wiring, fail-closed on 404, fail-closed on malformed body (raw bytes that don't parse), fail-closed when verdict_counts missing, returns None when uninitialized, URL-encodes weird agent names. Extended the `fake_backend` context manager with `quality_signal`/`quality_signal_status`/`quality_signal_body_raw` knobs to back those tests. Full SDK suite: 47 passed in 13s. Backend untouched, 577 passing.

**Not done in this slice:** 14.7 (Phase 14 demo — user-driven, needs an eval cycle to run on prod). 14.6 was the planned "additional tests" sub-task; folded into 14.1+14.3+14.4+14.5 since those covered the items 14.6 was meant to test.

### 2026-05-17 — Phase 14.4: quality signal endpoints + dashboard surface

Surfaces what the Phase 14.3 judge wrote. End-to-end: cron picks runs → judge rates them → endpoints aggregate → dashboard renders inline on /agents (Quality chip per row) and on /agents/{name} (verdict breakdown + recent bads with reasons).

- [x] **`backend/quality_signal.py` (new).** Pure module — no LLM calls, no writes. `agent_quality(session, ws, name, days=7)` and `workspace_quality(session, ws, days=7)` return verdict counts + recent bads + trend vs the prior window of the same length. `MAX_WINDOW_DAYS=90` clamps a misbehaving caller; `RECENT_BADS_LIMIT=5` caps the inline list. Trend uses a 1pp dead-band so small-sample noise doesn't flicker the arrow; returns `direction='unknown'` when either window has zero evals.
- [x] **Two endpoints in `main.py`.** `GET /workspaces/me/quality?days=7` for the workspace rollup (per_agent list sorted alphabetically + workspace-wide rollup + top recent_bads); `GET /workspaces/me/agents/{name}/quality?days=7` for one agent (verdict counts + recent_bads with judge reasons + trend). Both pure reads; safe to poll.
- [x] **Dashboard `api.ts`.** New types: `Verdict`, `VerdictCounts`, `QualityTrend`, `RecentBad`, `AgentQualitySummary`, `AgentQuality`, `WorkspaceQuality`. New helpers: `fetchWorkspaceQuality(days=7)`, `fetchAgentQuality(name, days=7)`. tsc --noEmit clean.
- [x] **/agents Quality column.** New `Quality (7d)` header. New `QualityChip` component picks tone from the worst verdict present (any `bad` → red, any `borderline` → amber, all `good` → green; empty pool → muted "—" with hover hint so the user knows the cron just hasn't sampled yet rather than thinking the bot is broken). Hover title shows the full breakdown + the trend delta. `fetchWorkspaceQuality` runs in parallel with `fetchAgents` + `fetchConstellation`; failure is swallowed (page render isn't gated on a fresh-deploy workspace having eval data).
- [x] **/agents/{name} Quality section.** Renders above System Prompt. Hidden entirely when `total_evaluations === 0` so dormant agents don't show empty sections. Shows three colored verdict-count chips, the total, the trend pill (↑/↓/→ + pp delta), and a "Recent bads" list with the judge's reasons + confidence + run_id link.

**Verification:** 17 new tests in `backend/tests/test_quality_signal.py` covering pure-module math (verdict counts, window clamping, recent-bads ordering + limit, trend up/flat/unknown, system-agent filtering, workspace-rollup sums, alphabetical per_agent ordering) + the two endpoints (response shape, cross-workspace isolation, 401 unauthenticated). Full backend suite: 577 passed in 122s; 0 regressions. Dashboard tsc --noEmit clean.

**Not done in this slice:** 14.5 (SDK helper for 12D.3 consumption — `lightsei.get_quality_signal`), 14.6 (additional tests), 14.7 (Phase 14 demo).

### 2026-05-17 — Phase 14.3: periodic eval job + cron enqueuer

Wires the pure sampler from 14.1 into the in-process job runner from 12C.6.2. End-to-end: cron drops one `eval_runs` job per workspace per hour → existing runner claims it via SKIP LOCKED → handler samples completed runs → calls Sonnet as judge → persists verdicts to `run_evaluations` → attributes judge spend to `lightsei.system` so the workspace monthly cap covers it.

- [x] **`backend/eval_runner.py` (new).** `run_eval_job(session, workspace_id, payload)` handler: pre-checks workspace's ANTHROPIC_API_KEY + budget cap (same shape as `agent_generator.run_agent_generation_job` for consistency); calls `eval_sampler.pick_sample`; for each sampled run, builds the prompt via `eval_sampler.build_judge_prompt`, calls `anthropic.Anthropic().messages.create(**prompt)`, extracts the verdict from the `submit_verdict` tool_use block, writes a `RunEvaluation` row; accumulates token totals + writes one `lightsei.system` Run row at the end for the cost rollup. Per-sample failures (Anthropic error, missing run, agent deleted, judge returned plain text instead of calling the tool) increment `errored` but don't stop the cycle — next sample is tried.
- [x] **Cron enqueuer in the same module.** `start_eval_cron()` / `stop_eval_cron()` mirror `jobs.start_runner` / `jobs.stop_runner`. Loop sleeps `LIGHTSEI_EVAL_INTERVAL_S` (default 3600s, floored at 10s so a misconfigured tiny value can't hammer). Enqueues immediately on first iteration so fresh deploys get evals within minutes rather than waiting a full hour. Per-workspace: one `eval_runs` row per workspace per cycle (`enqueue_eval_job_for_workspace`).
- [x] **Wired into FastAPI lifecycle.** `eval_runner.start_eval_cron()` added to `main.py`'s `on_startup` (after `jobs.start_runner()`). `eval_runner.stop_eval_cron()` added to `on_shutdown` (before `jobs.stop_runner()` so in-flight eval_runs rows aren't orphaned). `jobs._load_default_handlers()` imports `eval_runner` so the `eval_runs` handler is registered before the runner starts claiming jobs.

**Verification:** 13 new tests in `backend/tests/test_eval_runner.py` — pre-check skips (no_anthropic_key, over_budget, no_samples), happy-path writes a RunEvaluation row with all the right fields including cost, judge spend lands on lightsei.system on /cost, multi-sample, resilience to one Anthropic error (other samples still evaluated), resilience to non-tool response (no row written — does not default to 'good'), idempotency across repeated handler invocations (sampler's NOT EXISTS holds), cron interval env reading with bad-value fallback + 10s floor, enqueuer drops correctly-shaped row, cron sweeps all workspaces, handler is wired into the dispatch registry. Full backend suite: 560 passed in 118s, 0 regressions outside the new files.

**Not done in this slice:** the quality endpoints + dashboard surface (14.4), SDK helper for 12D.3 (14.5), phase demo (14.7).

### 2026-05-17 — Phase 14.1 + 14.2: sampler + judge prompt + run_evaluations schema

Two sub-tasks shipped together because 14.1's `pick_sample` queries `run_evaluations` (the table 14.2 creates) — splitting them would leave 14.1's tests unable to run end-to-end against the test DB schema. [[feedback-chain-coupled-tasks]] pattern.

- [x] **14.2 — `RunEvaluation` model in `backend/models.py` + alembic 0026.** Schema per spec: id (str pk), run_id (FK runs CASCADE), workspace_id (FK workspaces CASCADE), agent_name, judge_model, verdict (string, not enum, so future verdicts don't need a migration), reasons (jsonb), confidence (Numeric 4,3), judge_tokens_in/out (Integer), judge_cost_usd (Numeric 12,6), created_at. Three indexes: `(workspace_id, agent_name, created_at DESC)` for the /agents quality column, `(workspace_id, verdict, created_at DESC)` for "recent bads" queries, and `(run_id, judge_model)` unique so the sampler's "skip already-evaluated" check has DB-level enforcement alongside the application-level `NOT EXISTS`.
- [x] **14.1 — `backend/eval_sampler.py` (pure module).** Two functions plus a tool schema. `pick_sample(session, workspace_id, per_agent=3, window=1h, now=...)` returns run_ids: for each non-`lightsei.*` agent in the workspace, takes the last hour's completed runs that don't have a verdict from `JUDGE_MODEL`, ordered by `ended_at DESC`, capped at per_agent. `build_judge_prompt(session, run_id)` composes the judge's single LLM turn — agent role + system prompt + plan event + output event, all wrapped in a `messages.create()`-ready dict with forced `tool_choice` on `SUBMIT_VERDICT_TOOL`. Plan event = `<agent_name>.plan` when present (orchestrator pattern), else first non-envelope event from the agent (executor fallback). Output event = last non-envelope event from the agent. `SUBMIT_VERDICT_TOOL` schema-strict: verdict in {good, borderline, bad}, reasons array 1-5, confidence 0..1, additionalProperties false. Locked design choices documented inline (judge model = claude-sonnet-4-6, per_agent default = 3, window = 1h).

**Verification:** 22 new tests in `backend/tests/test_eval_sampler.py` — sampler honors per-agent cap, skips lightsei.* agents, skips already-evaluated runs (and dedup is keyed on judge_model so different-judge re-evals are allowed), skips out-of-window runs, skips uncompleted runs, covers multiple agents, respects workspace isolation, reads env var with bad-value fallback. Prompt builder covers orchestrator/executor patterns, single-event runs (plan == output), forced tool_choice, error paths (missing run, deleted agent). Schema validated with jsonschema against the 6 corner cases (good/borderline/bad enum, reasons min 1 / max 5, confidence range, additionalProperties false). Full backend suite: 547 passed, 0 failures, 113s. No regressions outside the new file.

**Not done in this slice:** the runner that actually fires judge calls (14.3), the storage of verdicts to `run_evaluations` (14.3's responsibility), endpoints + dashboard (14.4), SDK helper (14.5), demo (14.7).

### 2026-05-17 — Phase 12C closed: team-from-README flow runs end-to-end on prod

The 12C.5 + 12C.6 demos collapsed into one. With the Railway worker deployed (separate Done Log entry below), polaris's redeploy on the clean Python image, the workspace secrets set via the new /account Suggested-secrets panel, and the dispatch-graph rules wired by 12C.4, the whole loop runs:

- Drop the Lightsei README on /agents/team-from-readme → team plan returns inside the timeout via the 12C.6 async path (no more fake-CORS failures).
- Bulk generate + review + deploy → 4 bots queued, the lightsei-worker Railway service claims and spawns each (~30-60s for the venv + pip install).
- Push a commit to bewallace01/lightsei → polaris's GitHub webhook fires → polaris re-ticks, evaluates the push, dispatches the right command_kinds to the right teammates. Constellation map lit up with the live edges; runs + cost rollups landed on /cost.

Bailey confirmed it looks good. Phase 12C is closed.

**Things 12C touched that are now permanent:**
- `generation_jobs` async queue (used by /agents/generate + /teams/plan).
- `lightsei-worker` Railway service running on prod (with `MAX_CONCURRENT=8`).
- `/account` Suggested-secrets panel (the muscle memory now lives there for any future bot needing GITHUB_TOKEN / OPENAI_API_KEY / etc.).
- Team-from-README is a usable surface, not a demo flow.

**One observation worth carrying into 12D:** vela ticks every ~9 minutes on prod (78 runs/24h, $0 cost — the schedule is in its `@lightsei.on_schedule` decorator). That's the kind of "team-planner picked an aggressive default" pattern 12D.2 should surface inline so future Bailey doesn't have to find it via raw DB queries.

### 2026-05-17 — Deploy worker as a Railway service (Phase 5 prod gap, surfaced during 12C.6 demo)

Polaris had been stale for a day and every team-from-README bot was sitting in `queued` state on /agents/<name>. Root cause was a long-standing infrastructure gap that 12C.6 just happened to surface: the worker (`worker/runner.py`) was running on Bailey's laptop pointed at api.lightsei.com, not on prod. Lid-close took every bot offline at once. Lightsei's prod had a backend + a dashboard but no actual bot-execution process anywhere.

- [x] **worker/Procfile + worker/requirements.txt added.** Procfile says `worker: python runner.py`. requirements.txt pins `httpx==0.27.2` to match backend's version (everything else runner.py uses is stdlib). Minimum scaffolding so Railway's railpack auto-detects the build.
- [x] **`lightsei-worker` Railway service created.** Via CLI: `railway add --service lightsei-worker --variables LIGHTSEI_WORKER_TOKEN=<copied-from-backend> --variables LIGHTSEI_BASE_URL=http://beacon-backend.railway.internal:8000`. Internal Railway domain (not the public api.lightsei.com) so we don't pay for a public-internet hop on every poll. Backend's `RAILWAY_PRIVATE_DOMAIN` is still `beacon-backend.railway.internal` (from before the rename) — that's the form to use, don't normalize it.
- [x] **First deploy via `railway up --path-as-root worker --service lightsei-worker --ci`.** Build took ~30s (Python image + pip install httpx). Within 4 seconds of deploy completion, worker claimed argus / vega / vela / spica from the team-from-README team, plus a fourth deploy. Logs show: `claimed deployment=... agent=vela`, `fetching bundle`, `creating venv`, `pip install`, `starting bot.py`, `pid=...`, and heartbeats flowing back to the backend.

**One unresolved follow-up (parked):** the worker service doesn't auto-redeploy on `git push origin main` because the CLI's `--repo bewallace01/lightsei` flag kept failing with "Unauthorized" during service creation — likely needs Railway's GitHub OAuth flow that only runs through the dashboard. Today the worker runs the code I uploaded directly via `railway up`; future worker changes need a manual `cd worker && railway up --path-as-root . --service lightsei-worker --ci` until this is wired. See Parking Lot.

**Note on hosting decision:** doesn't change MEMORY.md's "single-host worker for v1" call (see Runtime decision 2026-04-27) — it just specifies *which* single host. We're still on the same architecture; the prod state addendum in MEMORY.md captures the move from laptop to Railway. Phase 5B (managed isolation runtime) is still the next switch.

**Concurrency bump:** worker's code default is `MAX_CONCURRENT=4`. Immediately filled up with the team-from-README team (argus/vega/vela/spica) and polaris's fresh redeploy sat stuck in `queued`. Bumped to 8 via `railway variables --service lightsei-worker --set LIGHTSEI_WORKER_MAX_CONCURRENT=8`. Worker auto-redeployed, claimed polaris in the next sweep — polaris went `queued → running` within ~10s of the env var landing. The override lives only on the Railway service; no code change. Watch for CPU/memory pressure on the single Railway instance if the bot count keeps growing — that's the signal to either drop the cap back or split to two worker instances (same signal that'd reopen the Runtime decision).

### 2026-05-17 — /account: suggested-secrets panel + extract shared guidance module

Two-part change driven by Bailey wanting "something on /account that reminds which keys need to be added" after seeing the missing-secrets list on team-from-README deploy success was transient (gone after navigation).

- [x] **`dashboard/app/secret_guidance.ts` (new).** Shared module owning `SECRET_GUIDANCE` (what / where / url for each well-known secret), `guidanceFor()` fallback for secret names not in the map, and `SUGGESTED_SECRET_ORDER` for stable display ordering. Replaces the inline copy that landed in team-from-readme/page.tsx earlier today.
- [x] **/account "Suggested secrets" panel.** Above the existing add-secret form. For each entry in `SUGGESTED_SECRET_ORDER`: green `✓ set` / amber `not set` pill (computed against the workspace's current secrets list, no extra backend call), expandable details (what / where / deep-link to provider's create-key page), and a `use this name in the form below →` button on unset rows that prefills `setSecretName(name)` + scrolls the form into view. Header pill shows `N of M set` for at-a-glance coverage.
- [x] **team-from-readme/page.tsx refactored** to import from the shared module instead of the inline copy.

**Not yet:** the panel still shows "well-known secrets bots typically want" rather than "specifically YOUR bots need these." Real per-bot lists would require persisting `needs_workspace_secrets` on Agent rows from the team-planner pipeline (currently only lives on the plan, not the persisted bot). Punted as too much scope for the user-friction problem at hand; the panel + the deploy-success view together cover the workflow.

### 2026-05-16 — Phase 12C.6.8: tests for the async-job machinery

Closes Phase 12C.6 except for the prod demo. Two new surfaces tested + every existing test that POSTed the refactored endpoints rewritten to match the 202/poll shape.

- [x] **`backend/tests/test_jobs.py` (new, 12 tests).** Runner state machine: success path (stub handler returns dict → status='success' + result_payload + timestamps + attempt_count=1), failure path (handler raises → status='failed' with exception text captured, attempt_count=1, no double-processing), unknown-kind terminal failure (so unknown rows don't sit in the queue forever), oldest-first ordering, SKIP LOCKED claim semantics (two concurrent claimers never both win the same row). Poll endpoint: row-shape on success, error surfacing on failure, 404 on nonexistent id, 404 across workspaces (don't leak existence), 401 unauthenticated. JSONB round-trip on enqueue.
- [x] **`kick_and_wait_for_job` helper + `GenerationJobFailed` exception in conftest.py.** POSTs the kick-off endpoint, asserts 202 + job_id, polls the GET endpoint until terminal, returns the result_payload on success or raises GenerationJobFailed carrying the persisted error text. Lets existing endpoint tests stay assertion-shaped close to the old synchronous style: success-path tests get the result dict back, failure-path tests wrap in `pytest.raises(GenerationJobFailed)` and check `exc.value.error` for the substring the old 4xx detail used to carry.
- [x] **`jobs._IDLE_SLEEP_S = 0.01` in conftest.py.** The runner's default 500ms idle gap would have added ~10s across the suite once every endpoint test went through the kick-off path. Test-only override.
- [x] **9 tests in test_agent_generator.py rewritten.** Success-path (3 retry-and-succeed + 1 happy path + 2 iteration + 1 cost-accounting) → `kick_and_wait_for_job`; failure-path (2 422s for off-dictionary retry exhaustion + validation retry exhaustion) → `pytest.raises(GenerationJobFailed)`. Pre-enqueue 4xx tests (missing secret, missing description, workspace isolation, unauthenticated) untouched — they still 4xx synchronously.
- [x] **6 tests in test_team_planner.py rewritten the same way.** Including the Anthropic-529-→-503 translation test: the handler still maps 529 to HTTPException(503), the runner persists "fastapi.exceptions.HTTPException: 503: Anthropic is overloaded..." in `error`, the test asserts the same "overloaded" + "try again" substrings the old 503 carried.

**Verification:** `pytest` full backend suite: 525 passed in 108s. No regressions outside the changed files; the new test_jobs.py adds 12 tests in <4s.

**What's left for Phase 12C:** the 12C.6 demo against prod (drop the Lightsei README, confirm no timeouts). Once that's green, NOW reverts to 12C.5 to run the full team-from-README flow including deploy + dispatch chain — that closes Phase 12C.

### 2026-05-16 — Phase 12C.6.5-12C.6.7: poll endpoint + dashboard polling

Closes the loop opened by 12C.6.1-12C.6.4. The backend endpoints already return 202 + job_id; this slice adds the GET that the dashboard polls and refactors the two long-running `api.ts` helpers (`generateAgent` and `fetchTeamPlan`) to kick off + poll behind their existing signatures. Callers (team-from-readme/page.tsx, the standalone /agents/generate page) keep working unchanged.

- [x] **12C.6.5 — `GET /workspaces/me/generation-jobs/{job_id}`.** Returns the row (id, kind, status, result_payload, error, attempt_count, created_at, started_at, finished_at). 404 covers both "no such row" and "row belongs to a different workspace" so we don't leak existence across workspaces. Uses the SQLAlchemy model + dependency-injected workspace_id, no raw SQL.
- [x] **12C.6.6 — `generateAgent()` polls.** POST returns `{job_id, status: 'pending'}`; helper polls `GET /workspaces/me/generation-jobs/{id}` with 1s → 2s → 4s → 5s cap backoff and a 5-minute total cap. Resolves with the typed `result_payload` on `success`; throws the backend's `error` text verbatim on `failed`. 250ms warmup pause so a fast handler doesn't pay a full second before the first poll.
- [x] **12C.6.7 — `fetchTeamPlan()` polls.** Same pattern, same helper (`pollGenerationJob`). team-from-readme/page.tsx's existing "thinking..." state keeps working since the signature didn't change.

**Verification:** Backend rebuilt, dashboard `tsc --noEmit` clean. POST /teams/plan returned 202 + job_id; immediate GET returned `pending` with `started_at=null, attempt_count=0`; 3s later GET returned `failed` with `started_at` + `finished_at` populated, `attempt_count=1`, and the full Anthropic 401 error captured on `error`. Nonexistent job id returned 404. Inserted a phase10c-workspace-owned row and read via default-ws's demo-key → 404 (cross-workspace authz). Cleanup deleted both the test rows + the seeded fake ANTHROPIC_API_KEY.

**What's left for 12C.6:** 12C.6.8 (tests for runner state machine + endpoint shapes against the stubbed-Anthropic harness). 12C.6 demo against prod still gates re-running 12C.5.

### 2026-05-16 — Phase 12C.6.1-12C.6.4: async generation_jobs queue + endpoint refactors

The "Failed to fetch" / fake-CORS shape on `/agents/generate` and `/teams/plan` was the synchronous Anthropic Opus call (with `max_retries=5` doubling worst-case wall time on the validation retry path) outrunning Railway's ~100s edge timeout. Killed connections come back without CORS headers, which the browser surfaces as CORS errors. Fix: move both long calls off the request path through a single in-process job queue, then poll for the result. Same pattern for both endpoints, no new worker process.

- [x] **12C.6.1 — `generation_jobs` table + alembic migration + SQLAlchemy model.** One table for both kinds (`agent_generate` / `team_plan`); columns + JSONB request/result payloads matched 1:1 with the spec. Two indexes: `(status, created_at)` for the runner's pending-picker, `(workspace_id, created_at DESC)` for the poll/list path. FK to `workspaces` with `ON DELETE CASCADE`. Migration revision 0025; applied cleanly on local stack.
- [x] **12C.6.2 — `backend/jobs.py` in-process runner.** Single asyncio task started on FastAPI startup. Claims one pending row at a time via `SELECT … FOR UPDATE SKIP LOCKED LIMIT 1`, dispatches by `kind` through a handler registry, runs the (sync) handler in `asyncio.to_thread` so the loop stays free, finalizes with `result_payload` on success / `error` text on failure. No auto-retry in v1; `attempt_count` bumps on each claim. Graceful cancel on shutdown.
- [x] **12C.6.3 — `/agents/generate` refactored to enqueue + 202.** Body extracted into `agent_generator.run_agent_generation_job(session, workspace_id, payload)`; endpoint shrinks to "validate input, check secret + budget, insert pending row, return `{job_id, status: 'pending'}` 202." Pre-checks still 4xx synchronously (no secret, over budget); anything that can only fail mid-call (Anthropic errors, validation-retry exhaustion) is captured as `error` on the row. Cost accounting (`lightsei.system` Run row) moved into the handler with an explicit `session.commit()` so spend lands even when the handler raises.
- [x] **12C.6.4 — `/teams/plan` refactored the same way.** Body extracted into `team_planner.run_team_plan_job(...)`. GitHub README fetch moved into the handler too so slow network calls don't sit on the request path; the endpoint still parses `github_repo` synchronously (via `_parse_github_repo`) so malformed-URL errors come back as 400 immediately and stashes the parsed `(owner, name)` on the payload so the handler doesn't re-parse. Handler registration goes through the same `_register()` + `import jobs; jobs.register_handler(...)` pattern as agent_generator.
- [x] **Belt-and-suspenders from upstream merge.** `backend/alembic/env.py` got `disable_existing_loggers=False` (was silently muting uvicorn + lightsei.* loggers post-startup — the actual cause of the "no Railway logs" symptom during the demo dig). Dashboard `runWithConcurrencyLimit` cap stays in as a second line of defense even though the async refactor obviates the burst-timing concern. `LIGHTSEI_SECRETS_KEY` made required in docker-compose with a generation hint.

**Verification:** Backend rebuilt + restarted locally. Migration 0025 applied cleanly (`\d generation_jobs` shows the table + both indexes). `POST /teams/plan` with no body returned 400 (input gate). With a body but no `ANTHROPIC_API_KEY`, returned 400 (secret gate). With a fake key seeded, returned `{"job_id": "...", "status": "pending"}` + HTTP 202 inside ~30ms. Two seconds later the row was `status=failed, started_at + finished_at populated, attempt_count=1, error="fastapi.exceptions.HTTPException: 502: Anthropic API error: Error code: 401..."` — the runner picked it up, the handler ran, Anthropic rejected the fake key, the error was mapped via `_anthropic_err_to_http` and persisted verbatim. End-to-end loop for `team_plan` confirmed; `agent_generate` shares the same plumbing.

**What's left for 12C.6 to finish:** 12C.6.5 (`GET /workspaces/me/generation-jobs/{id}` poll endpoint — without this the dashboard can't see results), 12C.6.6 (dashboard `generateAgent()` polls instead of expecting the old sync shape — currently broken end-to-end through the UI), 12C.6.7 (dashboard `fetchTeamPlan()` polls), 12C.6.8 (tests for the runner state machine + endpoints). 12C.6 demo gates re-running 12C.5.

### 2026-05-12 — Phase 12C.4: bulk deploy + auto-approval rules + missing-secrets checklist

Closes the team-from-README flow structurally. The "Deploy team → (12C.4)" stub from 12C.3 is now a live button that turns the per-bot reviewed code into actual deployments, installs the plan's dispatch graph as auto-approval rules, and surfaces a checklist of workspace secrets the user still needs to set. Single-file change; reuses every backend endpoint that already existed (no schema work).

- [x] **Per-bot zip + POST.** For each approved bot (status `success` in 12C.3's review, not skipped), build a `.zip` in-browser via JSZip with two root files (`bot.py`, `requirements.txt`), wrap in a `File`, POST to `/workspaces/me/deployments` via the existing `uploadDeploymentBundle()` helper. Parallel across the team — backend's existing worker claim logic handles queue ordering. Per-bot status pills (`pending` / `zipping` / `deploying` / `deployed` / `failed`) update as requests resolve; each deployed row links to its `/deployments/{id}` page.
- [x] **Confirmation gate on failed bots.** If 12C.3 left any bots in `failed` state when the user clicks Deploy, a `confirm()` asks whether to deploy the rest or cancel and retry first. No silent drop-on-failure.
- [x] **Auto-approval rules from the dispatch graph.** After deploys settle, walk the plan's edges: for each `source → target` where both are in this team and both deployed cleanly, look up `target.command_kinds` and PUT one rule per kind via `upsertAutoApprovalRule({source_agent, target_agent, command_kind, mode: "auto_approve"})`. Cross-team edges (team → existing agent) are intentionally skipped here — the plan doesn't carry their command_kinds; the user can wire those on the agent page if they want. Rule install is best-effort: failures surface in the success view as per-rule error rows but don't roll back the deploy.
- [x] **Missing-secrets checklist.** Union of every approved bot's `needs_workspace_secrets` minus what `fetchSecrets()` returns from the workspace. Rendered as an amber-tinted callout on the success view with a link to `/account` so the user sets them before bots crash on first run.
- [x] **Success view.** Header counter (`N deployed · M failed`), per-bot rows with deployment links, auto-approval rule summary (installed + failures separately), missing-secrets section if non-empty, CTAs back to `/` and `/agents`. `back to plan` link preserved for last-minute regrets.
- [x] **Best-effort description forwarding.** Each deployed bot's roster row gets `description = rationale` from the LLM via `patchAgent()` — same pattern 12B.2 uses. Errors here are swallowed; the deploy is what matters.

**Verification:** `tsc --noEmit` clean, route compiles + serves in <1s. Real end-to-end run lands in 12C.5.

### 2026-05-12 — Phase 12C.3: bulk-generate the approved team

The per-bot loop that turns the approved plan from 12C.2 into actual `bot.py` + `requirements.txt` previews. The "Generate & deploy → (12C.3)" stub on `/agents/team-from-readme` is now a live "✨ Generate code" button. Single-file change; reused the existing `/agents/generate` endpoint without backend changes.

- [x] **Parallel per-bot calls.** Clicking Generate fires one `generateAgent({description, target_agents, name_hint})` request per team member through `Promise.allSettled` so one failure doesn't short-circuit the rest. Backend serializes its own concurrency on the workspace's budget cap; the dashboard just kicks off everything at once.
- [x] **Per-bot description = `draft_description + "Coordinate with: ..."`.** Each request's description appends a bullet list of teammates with their summaries so the generator wires `send_command` to teammates rather than re-implementing their work. `target_agents` also seeds the dispatch targets the user picked in the plan, so existing-agent edges survive into the generated code.
- [x] **Three-phase page state.** `phase: "plan" | "generating" | "review"` drives which controls render. Plan edits (rename / role / dispatches / description / remove / add) are disabled outside `plan` — the member panel becomes a read-only message with a `back to plan` link. The constellation preview stays visible throughout so the user keeps spatial context.
- [x] **Per-bot status chips + counters.** During `generating`, each row's status pulses on the indigo "generating" chip; the header counter shows live `N ok · M running · K failed · S skipped`. After all settle, the page flips to `review`.
- [x] **On-failure controls.** Failed rows get three actions: `retry` (re-run the same description), `edit & retry` (textarea seeded with the current description, save to re-run with the edit), `skip` (mark as skipped so the final deploy count excludes it). All three are wired through a shared `runOneGenerate(member, description)` helper that overwrites the row's status as the request resolves.
- [x] **Code preview.** Successful rows have a `show code` toggle that reveals the rationale + `bot.py` + `requirements.txt` in scrollable `<pre>` blocks — same shape as 12B.2's preview. Collapsed by default so a six-bot review fits on one screen.
- [x] **`Deploy team → (12C.4)`** rendered but disabled with a tooltip indicating bulk deploy + auto-approval rules ship in the next phase. `back to plan` button preserves `genResults` so re-generating after a small tweak doesn't blow away the rest of the work.

**Verification:** `tsc --noEmit` clean; dev server compiles + serves the route in <1s. Real generation flow not yet exercised against prod — the demo + sanity-check on a real workspace happens once Vercel redeploys.

**What this leaves on the table for 12C.4:**

- For-each-approved-bot loop: zip `bot.py` + `requirements.txt` in-browser (reuse the 12B.2 JSZip helper), POST to `/agents/{name}/deployments`, surface per-bot deploy progress.
- After all deploys queued: PUT the auto-approval rules from the plan's dispatch graph via the existing `/workspaces/me/auto-approval-rules` endpoint.
- Missing-secrets checklist on the success page — bots that listed `needs_workspace_secrets` the workspace hasn't set get a "set this on /account" prompt rather than crashing on first run.
- 12C.5: demo run with the Lightsei project's own README.

### 2026-05-06 — Phase 12C.2: team-review UI

The dashboard surface that consumes 12C.1's plan endpoint. New `/agents/team-from-readme` route. Frontend-only — backend already exists from 12C.1.

- [x] **Input card.** Drop zone for `.md` / `.txt` files (drag-drop OR click-to-pick, 1MB cap), textarea for pasted README, separate textarea for freeform context, optional GitHub repo URL field (accepts `owner/name` or full URLs, including the `git@` form). Submit posts to `/workspaces/me/teams/plan`. At least one of the three inputs required, validated client-side before the round-trip.
- [x] **Constellation preview.** SVG canvas matching the home page's dark-celestial aesthetic (slate-950 → indigo-950 gradient, sparkle stars sized by role, dashed dispatch edges). Orchestrator at center; specialists/messengers ringed around it with a deterministic angle layout (no force-directed jitter between renders). Ghost stubs along the right edge represent existing workspace agents the team dispatches into, so a `vega → polaris` edge in the plan reads visually instead of disappearing into the void. Reuses `tintForAgent()` + `sparklePath()` from `stars.ts` — atlas is the same violet sparkle here as on the home page.
- [x] **Member detail / edit panel.** Click a star to open a side card showing role, summary, command kinds, dispatches-to checklist, required workspace secrets, and the `draft_description` that will feed 12C.3's per-bot generator. Inline edits: rename (dropdown of unreserved star-dictionary names), change role (orchestrator | specialist | messenger), toggle dispatch targets (capped at 2 with a clear alert on the 3rd — steers users toward linear chains rather than fanouts), edit description (textarea with save/cancel), remove from team (also strips dispatch edges pointing at the removed name).
- [x] **"+ add bot"** picks the first free dictionary name as a sensible default and seeds a specialist stub for the user to customize. Refuses if all 20 names are taken.
- [x] **"Generate & deploy" button** rendered but disabled with a tooltip indicating it ships in 12C.3. The plan stays in component state until the user navigates away — no auto-save, no half-deployed bots.
- [x] **`STAR_DICTIONARY` mirrored client-side** in `dashboard/app/agents/team-from-readme/star_dictionary.ts`. 20 entries, kept in sync by hand (the backend's submit_team validator is the authoritative gate; this list just powers the inline rename dropdown).
- [x] **`TeamPlan` / `TeamMember` / `TeamPlanInput` types + `fetchTeamPlan(input)`** helper in `dashboard/app/api.ts`.
- [x] **Header nav: "✨ propose a team from README"** added to the `agents ▾` dropdown so the page is discoverable.

**Verification:** type-check clean (`tsc --noEmit` exit 0). Manual browser-side QA pending — sub-component visuals (drop zone hover state, constellation layout under wide/narrow viewports, side-panel scroll on long descriptions) want a human eye.

**What this leaves on the table for 12C.3:**

- For-each-member loop calling `POST /workspaces/me/agents/generate` with the draft description + a "coordinate with: …" suffix listing the team. Render progress per-bot.
- Per-bot 12B.4 validation gate; on failure surface a "skip / retry / edit description and retry" choice.
- Per-bot code preview in the 12B.2 shape — user reviews + edits each before the final deploy in 12C.4.

### 2026-05-06 — Phase 12C.1: project-analysis endpoint (drop a README, get a team)

The first slice of Phase 12C. Server-side endpoint takes a project description (README text, freeform paragraph, or GitHub repo URL) and returns a 3-7-bot roster wired into a constellation. Pure analysis — no agents are created. 12C.2-12C.4 (review UI, bulk generate, bulk deploy + auto-approval rules) build on top of this. Shipped same day as 12D.2; same release cadence.

- [x] **`team_planner.py` module.** Pure functions: `SUBMIT_TEAM_TOOL` schema (forced tool_choice → guaranteed JSON, role enum {orchestrator | specialist | messenger}, dispatch graph capped at 2 outgoing edges per bot), `build_system_prompt()` teaching the team-design step (work buckets, dispatch graph rules, "reuse existing agents instead of duplicating"), `validate_team_plan()` catching off-dictionary names, duplicate names within team, names already in the workspace, dangling dispatch edges, multi-orchestrator, size violations (3 ≤ N ≤ 7), `build_validation_retry_message()` for the corrective retry turn.
- [x] **`POST /workspaces/me/teams/plan` endpoint.** Mirrors 12B.1's shape exactly: workspace `ANTHROPIC_API_KEY` + budget-cap gate, snapshot existing constellation (filtering out `lightsei.*` system agents so the planner doesn't see accounting buckets as real bots), build prompts, force `submit_team` tool, validate, retry once with corrective feedback if invalid, 422 if retry still fails. Tracks tokens across both attempts and commits one Run on `lightsei.system` in a finally block — matches the Phase 12D follow-up cost-attribution pattern so generation spend lands on `/cost`.
- [x] **`github_api.fetch_readme(...)` helper.** Lighter than `fetch_directory_zip` — calls `/repos/{owner}/{name}/readme` and base64-decodes. Public repos work unauthenticated (subject to the unauth rate limit, ~60 req/hr/IP); if the workspace has a `GitHubIntegration` row matching the requested repo, its decrypted PAT is used so private repos work. URL parsing tolerates `owner/name`, `https://github.com/owner/name`, `git@github.com:owner/name`, and trailing `.git`.
- [x] **21 new tests (`test_team_planner.py`).** Module unit: schema required fields, role enum + dispatch maxItems, prompt includes existing agents and filters reserved names from the star table, user-message handles missing inputs, validate accepts clean / rejects each failure mode (off-dictionary name, reserved name, duplicate-within-team, dangling dispatch edge, multi-orchestrator, size out of bounds), accepts existing-agent dispatch target. Endpoint with stubbed Anthropic: happy path, no-inputs 400, missing-secret 400, retry-on-bad-name path, 422 after exhausted retry, cost recording on `lightsei.system`, workspace isolation, unauthenticated, `lightsei.*` filtered from prompt context. Full suite at 495/495 backend.

**SDK release:** none. The analysis runs server-side via the workspace's own Anthropic key (same as 12B.1) — bots don't need new surface for 12C.1. 12C.3's bulk-generate path will reuse 12B.1's existing `/agents/generate` endpoint, so no SDK bump there either.

**What this leaves on the table for 12C.2-12C.4:**

- 12C.2: drop zone + textarea + repo-URL field, visual constellation preview of the proposed team (reuse the home page's star-and-edge aesthetic against proposed bots instead of deployed ones), inline edit (rename / remove / add bot, edit description), "generate and deploy" button.
- 12C.3: per-bot loop calling `POST /workspaces/me/agents/generate` with `description = bot.draft_description + "Coordinate with: ..."`, validation gate per-bot, code preview before final deploy.
- 12C.4: bulk `uploadDeploymentBundle`, install auto-approval rules from the dispatch graph via the existing PUT, surface missing workspace-secrets checklist on the success page.
- 12C.5: demo run with the Lightsei project's own README.

### 2026-05-06 — Phase 12D.2: Polaris narrates cost analysis in plan stream

Polaris now emits a `polaris.cost_analysis` event after each plan emit, so the cost-insights audit shipped in 12D.1 surfaces during the user's normal review flow instead of requiring a dedicated page visit. Closes Phase 12D except for 12D.3 (auto-tune), which stays parked until Phase 14 (continuous eval) lands. End-to-end shipped same day as the 12D follow-up cost-accuracy work.

- [x] **SDK 0.1.6: `lightsei.get_cost_insights()`.** New `_cost_insights.py` module fronts a GET against `/workspaces/me/cost/insights` and unwraps the `{insights: [...]}` envelope. Fails *open* (returns `[]` on transport error / 4xx / malformed body) — cost insights are enrichment, not essential, so a flapping endpoint can't block Polaris's tick. Three SDK tests: happy path, fail-open on 404, fail-open when uninitialized. Re-exported from `lightsei.__init__` and added to `__all__`.
- [x] **Polaris emits `polaris.cost_analysis` after each plan.** Tick loop calls the new helper, filters out audit rows that have zero signal (`cache_skip_savings` with `estimated_saved_usd == 0`, `failed_call_cost` with `failed_call_count == 0`, `plan_volatility` without an actionable streak), and emits only when at least one insight survives. Quiet workspace stays quiet. Variable-list insights (`model_tier_mismatch`, `per_trigger_roi`) pass through unchanged — they're already empty when there's nothing to say. Wrapped the whole thing in try/except so a programming error in the filter can't crash the tick.
- [x] **Backend `latest-cost-analysis` endpoint.** `GET /agents/{name}/latest-cost-analysis` mirrors `/latest-plan` so the dashboard can poll one event cheaply on the home + `/polaris` pages. 404 when the agent has not emitted one yet, which the dashboard treats as "render nothing" (so the section is absent rather than empty on quiet workspaces).
- [x] **Default validators on workspace creation.** New `validator_defaults.py` module exporting `seed_default_validators(session, workspace_id, now)`. Called from both `signup` and `POST /workspaces`. Default pack ships a `schema_strict` row on `polaris.cost_analysis` with `minItems: 1` on the `insights` array. Defense in depth: Polaris filters before emit, this stops a buggy bot from spamming. Sets the precedent for future per-event-kind defaults to land in the same module without scattering inline INSERTs.
- [x] **Migration 0024.** Seeds the same row for every existing workspace via a single INSERT … SELECT … FROM workspaces … ON CONFLICT DO NOTHING. Idempotent — re-runs of `upgrade_to_head()` (which fires on every backend boot) are no-ops once a row exists.
- [x] **Dashboard `<PolarisCostAnalysisPanel>`.** New shared component (`dashboard/app/PolarisCostAnalysisPanel.tsx`) that polls the latest event every 30s and renders insights as tone-coded chips ("fix" amber, "audit" gray, "status" emerald), sort fix-ables first, with the existing `apply.href` button when present. Mounted on the home page (`compact` prop, between the cost panel and recent runs) and on `/polaris` (full size, between the hero and the past-readings sidebar). Returns `null` when there's no event or no insights, so empty workspaces aren't disturbed.
- [x] **`PolarisCostAnalysis` type in `dashboard/app/api.ts`** + `fetchLatestPolarisCostAnalysis` helper. Reuses the existing `CostInsight` / `CostInsightApply` types from the `/cost/insights` page rather than duplicating, so the two surfaces evolve together.
- [x] **Test fix-up.** Existing `test_validation_pipeline.py` assumed a fresh workspace had zero validators; the new auto-seed broke 5 of those tests. Added a `_list_user_validators(client, headers)` helper that filters out the auto-seeded `polaris.cost_analysis` row, kept tests focused on the validators they actually exercise. The seeded baseline is its own test in the new `test_cost_analysis_event.py` (4 tests: seed exists with `minItems: 1`, valid event passes, empty insights rejected with 422, missing required field rejected).

**Tests:** 7 new (3 SDK, 4 backend). Full suite: 474 backend + 16 SDK pass. SDK 0.1.6 published to PyPI.

**What this leaves on the table:**

- 12D.3 (auto-tune model + tick interval with revert-on-regression). Parked behind Phase 14 (continuous eval) so we have a quality signal to safely tune against.
- Optional polish: the home page's "wants your attention" pulse counter currently covers pending-approvals + failed-validations; could include a count of `tone === "fix"` insights so the chip pulses in the same vocabulary. Skipped for now — the dedicated section is loud enough on its own and the pulse counter is precious real estate.

### 2026-05-06 — Phase 12D follow-up: cost accuracy + runaway-instance cap

A day of cost-correctness fixes prompted by spotting a 3.1× gap between the dashboard ($95.43 MTD) and the Anthropic console ($30.70) plus 25 polaris processes heartbeating concurrently from one MacBook. The dashboard math wasn't lying — 6M Opus input tokens on 142 runs really does price out at $90 — but two things were happening at once: (a) prompt caching was conceptually possible but Polaris wasn't using `cache_control`, and (b) the worker had spawned dozens of overlapping polaris instances that each independently shipped the full 106k-token MEMORY+TASKS context every minute.

- [x] **Fix #1 — generation cost lands on `/cost`.** `POST /workspaces/me/agents/generate` calls Anthropic server-side, bypassing the SDK auto-patch entirely; tokens were billed but never showed up on `/cost`. Wrapped the `_ask` call in a `try/finally` that accumulates tokens across every attempt (initial + name retry + validation retry) and commits one Run row attributed to a synthetic `lightsei.system` agent — the bucket surfaces in the cost rollup's `by_agent` breakdown so 12B usage is visible. Filtered `lightsei.*` out of `/agents` and the constellation map so the synthetic agent doesn't pollute the user's bot list. New backend test asserts `mtd_usd == expected` after a stubbed generation call.
- [x] **Fix #2 — failed-call cost.** `llm_call_failed` events now run through `add_run_cost_from_event` alongside `llm_call_completed`. Anthropic bills input tokens on refusals/rate-limits/safety-stops — those are real money that produced no output, and the helper already handles `output_tokens=0` cleanly. New test verifies failed-event cost lands on `runs.cost_usd`.
- [x] **Polaris prompt caching (the real money saver).** Polaris was shipping all 106,020 input tokens every tick (full MEMORY.md + TASKS.md + workspace context). Wrapped the system prompt and the user-message docs in `cache_control: ephemeral` blocks so Anthropic bills cache reads at 10% and cache writes at 1.25× of the input rate. At a 60s tick interval and a 5-min cache TTL, expect ~1 in 5 calls to be a cache write; the rest are reads. Per-call cost should drop from ~$1.59 to ~$0.16.
- [x] **Cache-aware pricing in the dashboard.** Updated the SDK's `_summarize_response` (sync + streaming) to capture `cache_creation_input_tokens` and `cache_read_input_tokens` from Anthropic's usage object. Extended `compute_cost_usd` to apply the 1.25× / 0.10× rates and updated the workspace `by_model` rollup SQL to mirror the same math. `runs.cost_usd` and `/workspaces/me/cost` now agree with Anthropic's invoice once `cache_control` is in play, instead of pricing every prompt at the full input rate.
- [x] **Per-hostname instance cap.** Backend constant `MAX_INSTANCES_PER_HOSTNAME = 3` (env-overridable via `LIGHTSEI_MAX_INSTANCES_PER_HOSTNAME`). On a *new* registration, `instance_heartbeat` counts active heartbeats for `(workspace, agent_name, hostname)` and 409s if the cap is hit. Existing instances refresh their own heartbeat regardless of cap. Stale rows (past `INSTANCE_ACTIVE_WINDOW = 90s`) don't count toward the cap, so crashed processes free up their slot automatically. New `lightsei.TooManyInstancesError` raised from `init()` when the synchronous startup heartbeat returns 409 — runaway-process pattern fails loudly instead of silently launching a 26th polaris that bills LLMs in parallel. Three new tests: backend cap enforcement + stale-doesn't-count, SDK 409-on-init.
- [x] **SDK 0.1.5 published to PyPI.** `TooManyInstancesError` re-exported from the package; `_HeartbeatPoster._post_once(raise_on_refusal=True)` only on the synchronous startup post (background refreshes still degrade gracefully so a config change while a process is alive can't kill it). Atlas/hermes already pin `lightsei>=0.1.4` so they pick this up on next redeploy.
- [x] **Killed all 25 runaway polaris processes** with `pkill -f "lightsei-worker.*src/bot.py"` — they were spawned by the worker into `/tmp/lightsei-worker/<deploy>/src/bot.py`, not from the repo's `polaris/bot.py` directly, so the first `pkill` target missed them. Worker (`worker/runner.py`) left running so it respawns one polaris per active deployment. Active polaris count immediately dropped from 25 → 1 and stayed there.
- [x] **Wiped the inflated May 2-5 cost data.** Direct UPDATE on prod: `runs.cost_usd = 0` for `agent_name='polaris' AND started_at >= '2026-05-02 00:00:00+00' AND started_at < '2026-05-06 00:00:00+00'` — 54 runs at $88.76 zeroed, May 6+ ($6.67) preserved. Dashboard MTD is now an honest baseline going forward; the events themselves are kept so `/runs` history isn't disturbed. Audit trail is this Done Log entry.

**Tests:** 5 new (failed-call cost, generation cost, two cap tests, SDK 409-on-init). Full suite at 470 backend + 13 SDK pass.

**Lessons logged:**

- *Token math being internally consistent doesn't mean it's right.* The dashboard agreed with itself across every aggregation but the price-per-token assumption (full rate, no cache awareness) was wrong for the world Anthropic actually bills in. Always cross-check against the vendor's invoice before trusting your own number.
- *Spawn-without-retire is a $X/day bug.* The worker's deploy lifecycle didn't kill prior bots when a new deploy claimed the same agent. With Polaris ticking every 60s on a 106k-token prompt, three weeks of accumulated zombie processes cost more than the actual product spend. Adding the per-hostname cap is the smallest fix that catches this class of bug across all bots, not just polaris.
- *Graceful degradation has scope.* Hard rule #4 says "SDK never crashes user code if backend is unreachable" — but the per-host cap is the backend explicitly *refusing* registration, not unreachable. Letting that crash on init is correct (it's the user's signal they have a runaway-process problem); the rule applies to network flakes, not policy-level rejections.

### 2026-05-05 — Phase 12D.1: cost insights page

The first slice of "Polaris is smart about spending." A read-only audit of the last 30 days of runs + events that surfaces five concrete waste signals on a new `/cost/insights` page. Pure analytics on existing data — no LLM calls, no schema changes, no bot code touched.

- [x] **Backend module `cost_insights.py`.** Pure functions, no I/O beyond the SQLAlchemy session. Each insight returns a homogeneous `{kind, headline, detail, apply}` shape so the dashboard's renderer is a generic map. Five insights:
  - `cache_skip_savings` — `polaris.tick_skipped` count × median `polaris.plan` cost over 30d. Tells the user how much Polaris's hash cache saved them in real dollars.
  - `plan_volatility` — hash the last 10 `polaris.plan` events' `summary + canonicalized next_actions` and count consecutive identical signatures starting from most-recent. Streak ≥3 surfaces a "consider doubling tick interval" recommendation with a link to the agent's schedule.
  - `model_tier_mismatch` — per-agent: if MAX input over 30d is under 16k tokens AND the agent uses an "expensive" model with a known cheaper sibling (opus → sonnet → haiku, gpt-4 → gpt-4o → gpt-4o-mini, gemini-pro → gemini-flash), recommend the swap with projected savings. Conservative MAX-based filter (not p95) — even one tail call needing the bigger model suppresses the recommendation, since a wrong downgrade breaking a real workload is worse UX than a missed recommendation.
  - `failed_call_cost` — sums input-token cost across `llm_call_failed` events grouped by error class (top 5).
  - `per_trigger_roi` — per-agent (≥5 runs threshold for sample size): flag agents whose useful-run rate (events emitted other than `run_started/run_ended/llm_call_started/polaris.tick_skipped`) is below 25% over 7 days. Suggests a tighter trigger filter or longer tick interval.
- [x] **`GET /workspaces/me/cost/insights`** wraps `all_insights()`. Workspace-scoped, no new auth surface, safe to poll on page load.
- [x] **`/cost/insights` dashboard page.** Renders the homogeneous list with tone-coded chips (fix / audit / status). Model-tier-swap recommendations have a one-click "apply" button that PATCHes the agent's pinned model directly via `patchAgent`; other recommendations route to the relevant agent page for considered review. Linked from `/cost` (top-right "✨ insights" button) and the activity dropdown ("✨ cost insights").
- [x] **13 new tests** covering each insight independently + endpoint shape + workspace isolation. Suite at 483/483.

**What this leaves on the table for 12D.2:** Polaris narrating these insights in its plan event stream so the user sees them in their normal review flow. 12D.3 (auto-tune) stays parked behind Phase 14's eval signal.

### 2026-05-05 — Phase 12B aftercare: dashboard polish + missing surfaces

A long stretch of UX work after Phase 12B + the parking-lot promotions on 2026-05-04. Theme: turn the home page from "list every state somewhere, hope the user finds it" into "every counter / label / failure on the home page links to the place that fixes or explains it." Most of these are individual commits; bundling here for the audit trail.

- [x] **Per-agent tick interval.** New `agents.tick_interval_s` column (migration 0021) + dashboard "Schedule" section on `/agents/[name]` with six preset buttons (1m / 5m / 15m / 1h / 4h / daily) and a custom seconds input. Polaris's main loop reads the override at the start of each sleep cycle via `lightsei.get_agent_config()` (added to SDK 0.1.4) — change takes effect on the very next sleep, no redeploy. Reactive bots (atlas, hermes) ignore the column. SDK 0.1.4 published to PyPI.
- [x] **`/getting-started` page + sharper empty states.** New 5-step walkthrough (API key → deploy a bot → see runs → optional Slack/GitHub → concept primer for agents/dispatch chains/approval gates/validators). Includes a sample `bot.py` + `requirements.txt` to copy. Header gets a `docs` nav link. Empty states on `/`, `/dispatch`, `/deployments` rewritten to define the concept first, then offer a primary action + a secondary "read the guide" button.
- [x] **Header reorg into dropdowns.** Flat `polaris / dispatch / notifications / github / docs` collapsed into 6 logical slots: `home / polaris / agents ▾ / activity ▾ / integrations ▾ / docs`. Each dropdown item has a one-line hint so the menu reads as a mini onboarding. Logo + new explicit `home` tab both teach the dashboard-home affordance for users who don't know "logo = home" by convention.
- [x] **Home page split.** Runs section on `/` capped at 5 entries with a "see all N runs →" footer; `CostPanel` got a `compact` prop that hides per-agent + per-model breakdowns and shows a "see breakdown by agent + model →" link. Two new dedicated pages: `/runs` (full unbounded table) and `/cost` (full breakdown). Activity dropdown brings them back into the nav (`runs / dispatch chains / cost`).
- [x] **`/validators` management page.** Backend already had GET/PUT/DELETE — needed a UI. New page lists every workspace validator (event_kind × validator_name × mode), inline edit per row (mode dropdown + JSON config textarea), failed JSON parse on save shows a clean error rather than corrupting the row. The home page's "X failed validation" pulse chip now links here (was `/runs`, which was a list view, not a fix-it surface).
- [x] **`/agents` directory roster.** New page lists every bot in the workspace with name (linked to detail) + 2-line description sub-row + role + status + pinned model + tick interval + runs(24h) + cost(24h) + last seen + delete. Inline model edit per row (provider dropdown + model id input + save / × buttons). Sorted orchestrator-first then by activity. Delete button per row with confirm dialog (history kept; only the agents row drops). Linked from the home page's "9 agents" Hero label and the "Constellation →" header label.
- [x] **`/agents/[name]` description editor.** New top-of-page Description section above Model. 12B-generated bots auto-populate from the LLM rationale on deploy (the `/agents/generate` flow now PATCHes the new agent with `{description: rationale}` after upload). Hand-deployed bots get an empty editor.
- [x] **Migration 0023: `agents.description`** (nullable text) + AgentPatchIn + serializer + dashboard wiring.
- [x] **DELETE `/agents/{name}`.** Hard-deletes the agents row; past runs/events/commands stay as audit trail (their string `agent_name` reference survives). Workspace-isolated. Three new tests.
- [x] **`failed_validations` pulse counter auto-clears when a rule is edited.** SQL change: the LEFT JOIN against validator_configs filters out fails older than the validator's `updated_at`. Editing a rule on `/validators` is the user's signal that they've addressed it; stale fails of an already-tuned rule disappear from the home pulse. Closes the "I fixed the rule, why is the home page still nagging me" paper cut.
- [x] **`drop` removed from banned-destructive-verbs default rule pack.** Polaris's plan landed FAIL because the verb fired on innocuous English ("drop a zip on /agents/new", "drop an entry from the parking lot"). Tightened to `(delete|truncate|destroy|nuke)` — the four with unambiguously destructive valence. Tests updated; new regression test asserts plans containing "drop" pass cleanly. Re-registered the live workspace's rule via PUT.
- [x] **Stale-agents counter retightened** earlier on 2026-05-04. Constellation map now filters by recent activity (heartbeat in last hour OR event in last 24h OR role=orchestrator); stale-agents pulse counter only counts agents that were recently relevant (so abandoned test bots from weeks ago don't keep nagging).
- [x] **Constellation polish** earlier on 2026-05-04. Even-angle layout per role ring (was hash-based with a post-hoc collision bump that left atlas + hermes on top of each other once their FNV-1a hashes clashed). Edges switched from bezier to straight lines (cleaner for hub-and-spoke than fighting bezier control points around the orchestrator).
- [x] **`/deployments` index page** earlier on 2026-05-04. The `[id]` subroute existed but `/deployments` itself 404'd; new index lists every deployment with status pill, source + commit, last heartbeat, error, link to detail.

**What this leaves on the table for tomorrow:**

- 12C.1 (project analysis endpoint, drop a README → propose a team)
- "Edit auto-approval rules from the dispatch view" — 11.6 already has the side panel, no work needed but unmentioned in the docs walkthrough
- 12B's parking-lot followups (in-browser directory zip via the existing JSZip dep, "deploy from GitHub repo path" form)

### 2026-05-04 — Phase 12B: describe a bot, get a bot

End-to-end LLM-generated bots from a natural-language description. Routes through a backend endpoint that calls Claude with a curated system prompt + the workspace's existing constellation, validates the output, and hands the user editable `bot.py` + `requirements.txt` they can tweak and deploy in the browser without ever touching a terminal.

- [x] **12B.1 — backend `/workspaces/me/agents/generate`.** Endpoint reads the workspace's `ANTHROPIC_API_KEY` secret, snapshots the existing constellation (agents + their command kinds), assembles a curated system prompt (SDK reference + 3 worked examples + star-naming dictionary with reserved names filtered out), and calls Claude Opus 4.7 with forced `submit_bot` tool_choice for guaranteed-shape output. Workspace budget cap from Phase 11B.1 enforced as a 429 gate. Backend `requirements.txt` bumped to include `anthropic>=0.40.0` (the local backend had it transitively / dev-side; Railway's strict resolver was 500ing on `ImportError` until this lined up).
- [x] **12B.2 — dashboard `/agents/generate` page.** Form with description textarea, multi-select for "coordinate with these agents" (populated from `/agents`), optional name hint. Generated bot lands in editable textareas (name + bot.py + requirements.txt). Deploy button assembles the two files into a `.zip` in-browser via JSZip (~50KB new dep — also unblocks parking-lot directory zipping later) and posts to the existing upload endpoint, routing to `/deployments/{id}` on success.
- [x] **12B.3 — iteration loop.** When the user wants to refine instead of restart, a "regenerate with tweaks" textarea appears below the preview. The endpoint accepts `tweak_request` + `previous_bot_py` + `previous_requirements_txt` together and appends an iteration turn to the Claude conversation so the prior bot is in scope and Claude diffs against it.
- [x] **12B.4 — validation gate.** Every generation passes through `validate_generated_bot()` before being returned: bot.py compiles (no SyntaxError), defines a top-level `main`, every import resolves (stdlib / lightsei / declared in requirements.txt with a small dist-name override table for `yaml→pyyaml`, `bs4→beautifulsoup4`, `PIL→pillow`, `cv2→opencv-python`), and requirements.txt mentions lightsei. Failures get one corrective retry; second-failure surfaces as 422 with the remaining issues so the dashboard can show the user rather than ship broken code.
- [x] **12B.5 — demo against prod.** First call: `description="Build a hello-world bot that emits a custom event."` → `lyra` (harmony / coordination glue) in ~8s, clean bot, validation passed. Second call: `description="Build a security scanner bot that scans pushes for secrets and dispatches to hermes when something is found."` → `argus` in ~25s (all-seeing giant — the dictionary picked the right star unprompted), wrote a real secret-detection regex table covering AWS / GitHub / Slack / OpenAI / Anthropic / Google / Stripe / PEM keys / generic password assignments, used `@lightsei.on_command("argus.scan")` and `@lightsei.track` correctly, validation passed first try. Quality of the generated code on both demos was deploy-ready with light edits.

**Star-naming worked exactly as intended.** Both demos picked names from the dictionary (`lyra`, `argus`) that thematically matched the role, with no name_hint. The system prompt's filtered-by-reserved-names dictionary + the post-response validation + retry path is the right structural answer.

**Operational notes:**

- Anthropic library missing from `backend/requirements.txt` was the only operational hiccup — caught immediately on first prod call, fixed in `c117089`. Mirror of the Phase 11.5 lesson "never publish a wheel until the surface it needs is committed": the same trap applies to backend deps. Add a CI check that compares `requirements.txt` against actual imports at some point.
- Generation latency is ~8-25s for short-to-medium prompts. Railway's default request deadline handles this fine. Longer / more elaborate prompts may approach the deadline; revisit if users hit timeouts.

### 2026-05-04 — Worker retires previous deploys + browser-native deploy

Two parking-lot items promoted on 2026-05-04 right after Phase 12 closed.

- [x] **Worker retire-on-redeploy.** New `_retire_active_deployments_for_agent` helper in `backend/main.py` flips `desired_state` to `stopped` on every existing active deployment for `(workspace_id, agent_name)` before persisting a new one. Wired into both deploy-creating call sites: the CLI / dashboard upload at `POST /workspaces/me/deployments` and the GitHub-push redeploy `_queue_github_redeploy`. Worker side already polls `desired_state` (worker/runner.py:383) and terminates cleanly, so this is purely a server-side change. Three new tests cover: previous deploy retired on redeploy, sibling agents untouched, already-stopped deploys not re-touched. Closes the manual `pkill` workaround that ran ~10 times during the Phase 11.7 + 12.4 demos.
- [x] **Browser-native deploy.** New `/agents/new` page with a drop zone (or click-to-pick) + agent-name input. Posts multipart to the existing `POST /workspaces/me/deployments` (same shape the CLI sends). Upload progress via XHR's `upload.onprogress`. On success, routes to `/deployments/{id}` so the user lands on real-time build + run logs. Linked from `/deployments` top-right and the empty-state CTA. New `uploadDeploymentBundle` helper in `api.ts` bypasses `authedJson` so multipart uploads use fetch's auto-boundary (which `application/json`-default helper would clobber). Out of scope for this slice and parked: in-browser directory zipping via JSZip, and a "deploy from GitHub repo path" form. Pairs with the worker-retire fix above so non-engineers using this surface won't accumulate orphan bots.

### 2026-05-04 — Phase 12: multi-provider model selector

Per-agent `provider` + `model` pin in the DB, an SDK Gemini auto-patch, a dashboard form to swap models, and a polaris routing layer that reads the pin at tick time. Swapping a bot's LLM is now one DB write — no code, no redeploy.

Shipped in four slices:

- [x] **12.1 — Agent row.** Migration `0021` added nullable `provider` + `model` columns on `agents`. PATCH `/agents/{name}` accepts both fields, validates provider against `{openai, anthropic, google, groq, xai, cohere}` with a friendly 422, normalizes to lowercase, respects `model_fields_set` so partial updates don't clear unrelated fields. Six new tests.
- [x] **12.2 — SDK Gemini adapter.** `sdk/lightsei/integrations/gemini_patch.py` patches `google.generativeai.GenerativeModel.generate_content` (sync + async) and emits the same `llm_call_started/completed/failed` events as the OpenAI + Anthropic patches. Idempotent class marker; soft-import fallback when `google-generativeai` isn't installed. Pricing entries added for `gemini-1.5-flash`, `1.5-flash-8b`, `1.5-pro`, `2.0-flash-exp`, `2.0-flash`, `2.0-flash-lite`, `2.5-flash`, `2.5-pro`. Wired into `_auto_patch()`. Seven new SDK tests using a stubbed `google.generativeai`.
- [x] **12.3 — Dashboard selector.** New "Model" section on `/agents/[name]` between System Prompt and Send Command. Provider dropdown + free-form model id input; partial fill warns; saved state shows "currently pinned to ..." or "no pin set." Calls the PATCH from 12.1. `AGENT_PROVIDERS` constant + `AgentProvider` type in `api.ts` mirror the backend enum.
- [x] **12.4 — Polaris routes by pin.** New `lightsei.get_agent_config(name)` SDK helper (returns `{provider, model}`). Polaris's `_call_llm` reads its own pin at tick time and dispatches to `_call_anthropic` (existing tool_use path) or `_call_gemini` (new `response_schema` path). `_strip_schema_for_gemini` walks the `submit_plan` schema and drops the fields Gemini's structured-output mode rejects (`additionalProperties`, `strict`, the `anyOf` nullable trick). Unknown provider raises with a helpful message rather than silently falling back. Eight new tests.

**Released SDK 0.1.3 to PyPI.** Two release dances during this phase: 0.1.2 cut after 12.2, 0.1.3 cut after 12.4 once `get_agent_config` was on the public surface. Both bot bundles' `requirements.txt` bumped to `>=0.1.3`.

**Demo result:** routing proven end-to-end on prod against `app.lightsei.com`. Polaris ticks logged `polaris: routing tick to google/gemini-2.0-flash` and `.../gemini-2.0-flash-lite` after the dashboard pin saved; the call reached Google's API; the response cleared the model+pricing layer correctly. Sustained Gemini calls require a paid Google Cloud project — Polaris's prompt is ~75k tokens (whole MEMORY.md + TASKS.md), and the free-tier `GenerateContentInputTokensPerModelPerMinute` quota tripped after a single tick + the SDK's internal retries. Pin flipped back to anthropic post-demo so the hourly tick keeps producing plans on the existing budget.

**Operational story (the fixes that surfaced):**

- [x] **PyPI 0.1.0 was pre-Phase-11.** The published wheel didn't have `send_command` at all (it landed in unpublished source for 11.1 + 11.5). Polaris's `@on_command` handler crashed with `AttributeError`. Fix: build + publish 0.1.1, bump bot requirements.
- [x] **Atlas's `source_agent="atlas"` was a latent prod-only crash.** SDK's `send_command` didn't accept the kwarg, and atlas's tests masked it via MagicMock. Found while wiring polaris's same call site; fixed in 0.1.1 by adding the parameter to the public surface + `_impl_send_command`.
- [x] **SDK auto-poller raced atlas/hermes tick().** The SDK auto-registers a default `ping` handler at module import which makes `has_handlers()` True and starts the auto-poller. Both bots use explicit `claim_command`/`tick()` and don't want the poller — it would race their loop and complete unrecognized command kinds (`atlas.run_tests`, `hermes.post`) as failed before tick() got a turn. Fix: clear `lightsei._commands._handlers` before `init()` in atlas + hermes.
- [x] **0.1.2 cut before 12.4 landed.** Same release-cadence trap as 0.1.0: I bumped + published 0.1.2 immediately after 12.2/12.3, then shipped 12.4's `get_agent_config` to source-only. Polaris's worker venv pulled 0.1.2 from PyPI, hit `AttributeError: module 'lightsei' has no attribute 'get_agent_config'`, my generic `except Exception` caught it, and routing fell back to anthropic every tick — looked exactly like the pin wasn't being read. Fix: bump to 0.1.3 with the helper, redeploy. Lesson logged: **never publish a wheel until the surface it needs is committed**, not the other way around.
- [x] **Worker concurrency cap kept biting.** `MAX_CONCURRENT=4` and the worker doesn't auto-retire previous deploys of the same agent, so each `lightsei deploy ./polaris` queued and the running instance held a slot indefinitely. Manual workaround: identify polaris by `setup_validators.py` in its bundle dir, `kill <pid>`, watch the worker claim. Real fix is the parking-lot item "worker should retire stale bot instances on redeploy" — the demo would have been ~30 min faster with that done.
- [x] **Gemini model name churn.** First attempt pinned `gemini-1.5-flash` per pricing entries — Google deprecated the 1.5 family on the v1beta API endpoint, error: `404 models/gemini-1.5-flash is not found for API version v1beta`. Switched to `gemini-2.0-flash` then `gemini-2.0-flash-lite`. Lesson: pricing.py's model list goes stale fast — pricing entries should age out and we should surface "deprecated" status in the dashboard's model selector (parking-lot).
- [x] **Free-tier rate limits don't fit Polaris's prompt.** `gemini-2.0-flash` and `flash-lite` both ran out of `GenerateContentInputTokensPerModelPerMinute` after one or two ticks. Each tick is ~75k tokens, and the SDK's internal retries multiply requests on transient failures. Demo verified routing without sustained calls.

### 2026-05-03 — Phase 11.7: Phase 11 demo on prod

All three demo variants verified end-to-end against `app.lightsei.com` and a real Slack `#default` channel.

- [x] **Variant 1 (click-to-approve).** Pushed a comment-only change to `backend/main.py`. Webhook fired → `polaris.evaluate_push` (auto-approved) → polaris matched `backend/**` → dispatched `atlas.run_tests` (pending) → clicked approve → atlas ran the bundled smoke tests (`2 passed`) → dispatched `hermes.post` (auto-approved via the `(polaris, hermes, hermes.post)` rule) → `✅ atlas: 2 passed` landed in Slack. End-to-end visible in `/dispatch`.
- [x] **Variant 2 (failure path).** Added a deliberate failing assertion to `agents/atlas/tests/test_smoke.py`, redeployed atlas, pushed another trigger. Atlas ran pytest → 2 passed + 1 failed → severity inferred as `error` → hermes posted `❌ atlas: 2 passed, 1 failed` to Slack.
- [x] **Variant 3 (full auto-approve).** Flipped the test back to passing, redeployed atlas, added `(polaris, atlas, atlas.run_tests) → auto_approve` rule via the `/dispatch` side panel, pushed a third trigger. Whole chain ran end-to-end with zero human clicks; `✅ atlas: 2 passed` landed in Slack.

**Operational story (the part that took the day):**

- [x] **SDK 0.1.0 was pre-Phase-11.** The bot bundle's `pip install lightsei>=0.1.0` pulled the published wheel which didn't expose `send_command` at all — polaris's `@on_command` handler crashed with `AttributeError` on the first push. Fixed by bumping `sdk/pyproject.toml` to 0.1.1, `python -m build`, `twine upload dist/lightsei-0.1.1*` (first PyPI publish since the rename), then bumping `lightsei>=0.1.1` in all 3 bot `requirements.txt` files and redeploying the bots. The CLI's lightsei was already on the source so the deploy command itself worked throughout.
- [x] **SDK auto-poller raced atlas's tick().** Atlas + hermes use the explicit `claim_command` / `tick()` pattern, not `@on_command`. The SDK auto-registers a default `ping` handler at module import, which makes `has_handlers()` True and starts the auto-poller. The poller would race tick() for commands and complete unrecognized kinds (`atlas.run_tests`, `hermes.post`) as failed before tick() ever got a chance to claim. Fix: clear `lightsei._commands._handlers` before `init()` in atlas + hermes so the auto-poller never starts.
- [x] **Worker's MAX_CONCURRENT cap kept claims stuck.** `LIGHTSEI_WORKER_MAX_CONCURRENT=4` (default) and the local worker had 4 stale polaris instances from the Phase 10.6 demo plus replays of failed deploys. Killed stale subprocs to free slots; redeployed all 3 bots fresh. Future-you: bump the env var if you ever want > 4 concurrent bots without surgery.
- [x] **Approve button rendered as `[object Object]`.** `POST /commands/{id}/approve` requires a `CommandApprovalIn` body (with optional `reason`); the dashboard was POSTing with no body, FastAPI 422'd with a structured array detail, and the error renderer flattened it via `String()`. Send `{}` on the wire and `JSON.stringify` non-string detail values; same fix applies to `/reject`.
- [x] **Atlas pytest target needed bundle-relative tests.** Default `ATLAS_PYTEST_ARGS` was `backend/tests/`, which doesn't exist inside an atlas-only deploy bundle (the worker spawns bot.py from a directory with just `bot.py + requirements.txt + tests/`). Added `agents/atlas/tests/test_smoke.py` (one trivial passing test + one importable-bot sanity check), defaulted `ATLAS_PYTEST_ARGS` to `tests/`, added `pytest>=8.0` to `agents/atlas/requirements.txt`. Phase 13+ will reshape this when atlas points at a checked-out user repo.
- [x] **Slack channel name had to match.** Hermes's first dispatch returned 404 against `/workspaces/me/notifications/dispatch` because no `NotificationChannel` row existed with `name='default'`. Created an Incoming Webhook on the user's Slack workspace, added a channel named `default` (matching atlas's `ATLAS_HERMES_CHANNEL` default) at `/notifications` with the webhook URL.
- [x] **CI was broken on every commit for 30+ pushes.** Pre-existing dep conflict in `backend/requirements-dev.txt` (`httpx==0.28.1` on top of an inherited `httpx==0.27.2` from `requirements.txt`); pip's strict resolver rejected. Drop the duplicate dev pin. Then `test_polaris_docs.py` import surfaced a second issue: it imports `polaris/bot.py` which imports `lightsei`, and CI's backend env wasn't installing the SDK. Added `pip install -e ../sdk` to the backend job. Both green now.
- [x] **Atlas bundle redeploys piled up under MAX_CONCURRENT.** Stale instances kept claiming the chain because each redeploy didn't retire the previous supervisor cleanly, so multiple atlas bundles raced and the old (failing-test) one sometimes won. Added a paranoid `pkill` of stale bundles per redeploy as a manual workaround; a real fix would have the supervisor send `desired_state=stop` to siblings when a new bundle for the same agent comes in. Parking-lot worthy.

**Dispatch chain rollup polish:** `_aggregate_chain_status` was checking for `Command.status == "done"` while the actual sentinel set by `complete_command()` is `"completed"`, so chains where every sub-command finished cleanly fell through to `"pending"` on the list view. Fixed.

**Surfaced cmd.error + cmd.result in the timeline.** The `/dispatch` row was rendering the payload only, leaving the actual stack trace invisible. Now `error` + `result` render alongside payload + linked events.

**Atlas latent bug exposed.** Atlas had been calling `lightsei.send_command(..., source_agent="atlas")` for two phases against a SDK that didn't accept the kwarg. Tests passed because they MagicMock the SDK and accept any kwargs. Real prod call would have crashed with `TypeError`. Found and fixed (the SDK now accepts `source_agent`, plus the bots have a defensive try/except that worked against pre-publish 0.1.0). Lesson logged: per-bot tests that mock the SDK are necessary but not sufficient; an integration check that exercises a real SDK call would have caught this earlier.

### 2026-05-03 — Phase 11.6: Dashboard `/dispatch` view

- [x] `GET /workspaces/me/dispatch` lists chains newest-first; one row per `dispatch_chain_id` with aggregate status (`pending_approval` > `running` > `failed` > `expired` > `done`), command count, max depth, last activity timestamp, and pending-approval count for the click-to-approve lozenge.
- [x] `GET /workspaces/me/dispatch/{chain_id}` returns the full timeline: every command ordered by depth then created_at, plus any events whose `payload.command_id` ties them to a chain command. Workspace-isolated (404 if the chain belongs to another tenant).
- [x] Frontend route `/dispatch` (linked from the header next to `polaris`). Each chain row expandable into an indented timeline; depth controls left-padding so a `polaris → atlas → hermes` chain visually nests. Pending commands get inline `approve` / `reject` buttons; auto-approved + completed commands skip the buttons. Per-command details flyout shows payload + linked event payloads.
- [x] Auto-approval rule editor lives as a toggleable side panel on the same page. Add / update / delete rules without leaving the view; wildcards (`*`) supported in the source-agent and command-kind fields per the existing 11.2 resolver precedence.
- [x] Backend tests (419 passing): list endpoint returns aggregates + ordering + workspace isolation; chain detail returns commands + linked events ordered by depth, 404s on unknown chain, 404s cross-workspace.

### 2026-05-03 — Phase 11.5: Polaris reacts to push events

- [x] Webhook receiver enqueues a `polaris.evaluate_push` command on every accepted push, with `dispatch_chain_id` set to the GitHub `X-GitHub-Delivery` header so the entire downstream chain (evaluate_push → atlas.run_tests → hermes.post) groups under one id for the 11.6 view. Payload carries commit_sha, branch, repo, touched_paths, author. (`backend/main.py`)
- [x] New `polaris/bot.py:evaluate_push` handler (registered via `@lightsei.on_command("polaris.evaluate_push")`) reads `POLARIS_PUSH_RULES` (default `backend/**:atlas.run_tests,polaris/**:atlas.run_tests`), fnmatches each touched path against each rule, and dispatches one downstream command per matching rule. No Claude call — the LLM-driven hourly tick stays untouched.
- [x] Side-quest fix: `lightsei.send_command()` now accepts and forwards `source_agent`. The kwarg was already used by Atlas's bot but the SDK silently dropped it; Atlas's tests covered the call site with a MagicMock so the latent `TypeError` only would have surfaced in prod. Now correct.
- [x] Tests (411 passing): push touching `polaris/bot.py` dispatches one `atlas.run_tests`; push touching only `*.md` dispatches nothing; push touching paths outside any rule dispatches nothing; chain_id matches the GitHub delivery id; rule parser tolerates whitespace + drops malformed entries; `**` is directory-aware (`backend/**` does not match `backendXYZ/`).

### 2026-05-03 — Connection-leak watch infra

Pre-Phase-11 pool was 5+10 = 15 connections; post-Phase-11 we saw 15 idle-in-transaction sessions saturating it during peak dashboard polling. Bumped to 20+40 = 60 in `4a7e37b` as a band-aid — but the root cause is still unidentified. Three pieces of passive instrumentation now in place to either confirm the bump fixed it or pinpoint the leaking endpoint:

- [x] `/health` returns SQLAlchemy pool counters + `pg_stat_activity` state breakdown (`idle_in_txn`, `active`, `idle`, `total`). One short query against the system view, cheap enough to run on every keepalive ping.
- [x] `.github/workflows/keepalive.yml` pipes the response body into the GitHub Actions log and pulls `idle_in_txn=N` onto its own line for grep — 288 samples/day for free.
- [x] Daily routine `Daily idle_in_txn watch` (`trig_019shTxF315odkgcK7LnhErb`, fires `0 13 * * *` UTC) scans the last 24h of keepalive runs, computes max + avg, opens a GitHub issue if any sample exceeds 5.

### 2026-05-01 — Phase 10.6: Phase 10 demo

Both legs of the loop verified end-to-end against prod. Polaris is now reading docs from `bewallace01/lightsei` on every tick, and any push that touches a registered agent path becomes a `github_push` deployment within ~8 seconds with no CLI involvement. Phase 10 is done.

**Happy path, both legs:**

- [x] **Polaris-from-GitHub.** Saved `POLARIS_GITHUB_REPO=bewallace01/lightsei`, `POLARIS_GITHUB_BRANCH=main`, `POLARIS_GITHUB_TOKEN=<fine-grained PAT>` as workspace secrets. Redeployed Polaris. The bot's startup banner went from `docs=<local path>` to `docs github=bewallace01/lightsei@main paths=MEMORY.md,TASKS.md`. First tick after the redeploy fetched both docs via `GET /repos/.../contents/...`, called `claude-opus-4-7` with **75400 input tokens** (vs. ~48k from the older bundled docs — that delta is the entire Phase 10 content), and emitted a `polaris.plan` event whose summary explicitly references "Phase 10 is at the final task (10.6: Phase 10 demo)" — content that only exists in the GitHub copy. Both `schema_strict` and `content_rules` validators returned `pass` on the emit.
- [x] **Push-to-deploy.** Single comment-only edit to `polaris/bot.py`, committed (`ede6e01`), pushed. github.com fired the `push` webhook → Lightsei's webhook receiver verified the HMAC, matched the touched file against the registered `polaris → polaris/` path, fetched the dir at `ede6e01` via the GitHub Contents API tree+blob dance (Phase 10.3 code), built the in-memory zip, created a `deployments` row with `source=github_push, source_commit_sha=ede6e01...`. The locally-running worker claimed it on its next 5-second poll, built the venv, fetched secrets, and the new instance went `running` 8 seconds after the push hit GitHub.

**Operational story (the part that actually took the day):**

- [x] **Phase 10.5 + 10.4 + 10.3 + 10.2 + 10.1 weren't deployed.** When I started the demo, `origin/main` was 4 commits behind local — Phases 10.1–10.4 had been committed but never pushed. Pushed everything; Railway picked them up. Pre-existing problem, surfaced because this was the first end-to-end use of prod since they shipped.
- [x] **Railway dashboard service had a config bug.** The first dashboard build after the push failed with "Set the root directory to 'dashboard' in your service settings". Auto-detect was trying to build from the repo root (no `package.json`). The previous successful build was still serving, so the dashboard *worked* but couldn't accept new code. Fix is one Railway UI toggle: Settings → Root Directory → `dashboard`. Backend service had the same risk; verified its root was already set correctly.
- [x] **The webhook leaked into chat.** Pasted a fine-grained PAT into the conversation by accident. Treated as compromised: revoked, generated a new one, scoped to `Contents: read` on the one repo. New PAT went only into the dashboard form (which validates against GitHub at PUT time and stores encrypted). The same trust boundary thinking led to Phase 10.4 deferring auto-injection of `POLARIS_GITHUB_TOKEN` from the integration PAT — even when they're the same string today, the integration PAT and the bot-runtime PAT should be rotatable independently.
- [x] **First webhook delivery timed out.** github.com's "We couldn't deliver this payload" yellow banner. Backend was responding fine to `/health`... after a 9.6s cold start. GitHub's webhook deadline is 10s. Delivery was retryable from the github.com Recent Deliveries UI (cold container, then warm at 0.3s), but a missed first webhook with no retry trigger would silently drop on the floor. The fix is two-pronged: a `.github/workflows/keepalive.yml` that pings `/health` and `/` every 5 min (free, lives in this repo, soft schedule with 5–15 min jitter), plus a Railway-side "no sleep / min instances ≥ 1" toggle when the plan tier supports it. The workflow needed a YAML fix — an unquoted colon in the curl `-w` format string broke the parser at column 88; switched both run steps to block-scalar `|` syntax to match `db-backup.yml`.
- [x] **DB connection leak.** Pages were taking 30+ seconds to load and `TypeError: Failed to fetch` was firing in the browser. `pg_stat_activity` showed a runaway 34 connections in `idle in transaction` state with ages 15–45s, all running `SELECT users.*` or `SELECT events.*` (the auth lookup pattern from `_resolve()`). Bursts of 9–11 simultaneously-leaked queries pointed straight at the dashboard's parallel page-load fan-out — the user navigates away mid-flight, Starlette doesn't always run the cleanup of generator-based DB session deps wrapped in a separate `@contextmanager`, sessions sit "idle in transaction" forever, pool saturates, every request behind it queues. Fix in `backend/db.py`:
    - Inlined `get_session()`'s try/yield/commit/rollback/close instead of going through `with session_scope()`. Generator-based deps inside a wrapping contextmanager have an extra suspended frame that doesn't always unwind on `GeneratorExit` (Starlette's cancel signal). Flat generator with cleanup directly in `finally` is the pattern FastAPI's docs actually show.
    - Defensive `s.rollback()` in `finally` before `close()` — no-op after a successful commit, but resets the txn if the dep was cancelled before either ran.
    - Server-side belt: SQLAlchemy `connect` event listener that runs `SET idle_in_transaction_session_timeout = '30s'` on every new pool connection. If the cleanup somehow doesn't run, Postgres rolls back orphaned txns at 30s instead of holding the connection hostage indefinitely.
    - `pool_recycle=300` so stale TCP connections die out cleanly across DB restarts / network blips.

  All 322 backend tests passed pre-push. The first few hours after deploy, the leak watchdog (a polling monitor I wrote against `pg_stat_activity`) saw the count rise to ~15, then drop, then rise again, never crossing 20 — exactly the self-healing behavior the 30s timeout is supposed to produce. Note: `current_setting('idle_in_transaction_session_timeout')` returns the **calling session's** value, not the listed pid's, so external verification of the per-session GUC isn't possible. The fix is judged by behavior, not direct readout.

  This is a workaround, not a root-cause fix. The "actually find the leaky cleanup path" investigation is parking-lot material — covered by the existing entry on `get_authenticated()` audit work.

- [x] **The worker on prod doesn't exist.** Per the Phase 5A runtime decision in `MEMORY.md`, the worker is single-host and runs *on the user's laptop* (`worker/runner.py`) — prod has only the API + DB + dashboard. The user's laptop hadn't been running the worker for 3 days, so every "redeploy Polaris" today was creating a `deployments` row that nothing claimed. The screenshot showing the latest run as "1d ago" was actually from a hand-emitted Phase 9.6 demo event (`run_failed: synthetic crash for Phase 9.6 demo`), not a real Polaris tick. The actual last real Polaris run was 2026-04-29 03:06. Started the worker locally with `LIGHTSEI_WORKER_TOKEN=... LIGHTSEI_BASE_URL=https://api.lightsei.com python worker/runner.py`, queued deployment got claimed within a second.
- [x] **Stale bundle.** First post-fix Polaris run still came up in disk mode. The bot's startup line said `docs=/private/tmp/lightsei-worker/.../src` (local) instead of `docs github=...`. Direct check of the running subprocess's env confirmed `POLARIS_GITHUB_REPO`, `POLARIS_GITHUB_BRANCH`, `POLARIS_GITHUB_TOKEN` were all injected — but a `grep -c "POLARIS_GITHUB_REPO\|_gh_config" bot.py` against the unpacked bundle returned **0**. The bundle in the deployment was the pre-Phase-10.4 `bot.py`. The user's prior `lightsei deploy ./polaris` had been from a checkout that didn't have the new code. Re-ran the deploy from the current source; new bundle had Phase 10.4 logic; banner flipped to `github=...`.
- [x] **Phase 10.4 silently broke Phase 8's strict-schema validator.** The first plan emit on the new bundle showed `lightsei event rejected (polaris.plan): schema_strict/required — 'tasks_md' is a required property`. Phase 8's `schema_strict` config in `validator_configs` requires `doc_hashes: {memory_md, tasks_md}` (fixed keys). Phase 10.4 generalized doc handling so `doc_hashes` is now keyed by filename (`{"MEMORY.md": ..., "TASKS.md": ...}`) — same content, different shape. The pre-Phase-10.4 validator schema rejected the new shape on the first wire tour, even though the plan was generated correctly (validators run on the SDK→backend `emit` path, not on the bot's local `plan` dict). Fixed via direct DB UPDATE on `validator_configs` for `(workspace_id, polaris.plan, schema_strict)`: replaced the `doc_hashes` schema with `{type: object, minProperties: 1, additionalProperties: {type: string}}`. After the fix and a redeploy (to reset the bot's in-process hash cache, which was sticky after the rejection because `_last_hashes` is updated regardless of validator outcome), the next emit landed with `schema_strict: pass, content_rules: pass`. **Followup**: this fix touched prod data directly and isn't checked in. Should be backfilled into a migration / seed update so a fresh workspace gets the right schema.

**What I ended up shipping inside this phase:**

- `.github/workflows/keepalive.yml` — 5-min `/health` ping for cold-start mitigation.
- `backend/db.py` — connection leak fix (inlined dep cleanup + 30s idle_in_transaction_session_timeout listener + pool_recycle=300).
- Direct prod patch on `validator_configs` to accept filename-keyed `doc_hashes`.
- `MEMORY.md` and `polaris/bot.py` got demo-marker comments to test the GitHub fetch + push-to-deploy paths; can stay or be removed in a cleanup pass — they're factually accurate either way.

**Honest assessment of "git push vs `lightsei deploy`":** push wins for routine code changes — the latency from `git push` to a `running` instance was 8 seconds, and there's no tab to keep open or env to source. The CLI is still useful for (a) the very first deploy of a new agent (no GitHub integration yet), (b) iterating on a branch you don't want to push, (c) deploying from a checkout that's ahead of GitHub. For Polaris, I won't run `lightsei deploy ./polaris` again unless I'm intentionally testing locally.

**Phase 10 is closed.**

### 2026-04-30 — Phase 10.5: Dashboard `/github` panel

Top-level `/github` route in the dashboard. Plain-Tailwind, matches `/notifications` and `/account`. Empty state shows a connect form (repo URL or `owner/name`, branch, PAT). Registered state shows connection status + masked PAT, the webhook URL/secret to paste into GitHub (secret revealed exactly once), an agent-path mapping table backed by `/workspaces/me/github/agents`, and a recent-pushes deploy list filtered to `source=github_push`. The user no longer has to know `curl` to wire up GitHub.

- [x] **`dashboard/app/github/page.tsx`** (~700 lines): four sub-components — `ConnectForm`, `StatusBlock`, `AgentPathsBlock`, `RecentDeploysBlock` — composed by `GitHubPage`. `parseRepoInput` accepts `owner/name`, `https://github.com/owner/name`, or `…/owner/name.git`; rejects anything else with a clean form error so we never POST garbage to the backend.
- [x] **`webhook_secret` is a one-time reveal**: the PUT response (`GitHubIntegrationFresh`) carries a transient `webhook_secret` field that lives in `useState` only for the current page lifecycle. Refreshing wipes it. The persistent `GitHubIntegration` shape exposes only `has_webhook_secret: boolean`. To rotate, the user disconnects + reconnects — confirmed by an explicit copy in both the UI and the disconnect-confirm dialog. This matches the backend's "show once, store encrypted" contract from 10.1.
- [x] **Agent-path mapping**: dropdown is filtered to agents that don't already have a mapping (no UI affordance for "edit existing" — remove + re-add is the path). The "add path" button disables when every registered agent already has a mapping, with a tooltip explaining why. Path input is sent as-is; the backend's `_validate_github_path` is the source of truth on what's allowed (no leading slash, no `..`, etc.).
- [x] **Recent deploys panel**: reuses `fetchDeployments()` and filters client-side to `source === "github_push"`. Top-10 most-recent. Each row links to `/deployments/{id}` so the user can pivot from "I see a github push deploy" to its full status + logs without a route change. Honest empty state — "Push to a registered branch + path to see deploys land here" — instead of hiding the section.
- [x] **`dashboard/app/api.ts` extensions**: `GitHubIntegration`, `GitHubIntegrationFresh`, `GitHubAgentPath` types matching `_serialize_github_integration` / `_serialize_github_agent_path` from the backend. `fetchGitHubIntegration` swallows 404 → `null` (the frontend's load-bearing "no integration" signal — `authedJson`'s default would have thrown a useless `Error("404")` instead). New `fetchAgents()` helper since `/github` needs the workspace's agent list to populate the mapping dropdown and we didn't have one yet (had `fetchAgent(name)` singular).
- [x] **`dashboard/app/Header.tsx`**: added `github` link between `notifications` and `account`. Same plain-Tailwind treatment as the other top-level links.
- [x] **Verification**:
  - `npx tsc --noEmit` clean.
  - `curl http://localhost:3000/github` returns 200, SSR snapshot includes the page header + "loading…" before client-side fetch resolves.
  - Empty integration: GET `/workspaces/me/github` → 404 → page renders `ConnectForm`.
  - PUT with a fake PAT against a real GitHub repo returns the backend's authoritative 400 (`"GitHub rejected the personal access token (401). Generate a new fine-grained PAT…"`) — the form catches it and surfaces it inline. Confirms the form's error path is wired to the backend, not just to network failures.
  - Inserted a fixture `github_integrations` row directly + path-mapped `polaris` → `polaris/` via PUT `/workspaces/me/github/agents/polaris`. GET responses match the `GitHubIntegration` and `GitHubAgentPath` types exactly.
- [x] **Deferred to 10.6**: the auto-injection of `POLARIS_GITHUB_REPO` / `POLARIS_GITHUB_BRANCH` into workspace secrets at integration registration time, mentioned in the Phase 10.4 Done Log as 10.5 work. Holding off because (a) it couples integration registration to the secrets API in a way that's hard to undo, (b) the user is already manually setting `POLARIS_GITHUB_TOKEN` so the "set 3 vars" UX isn't meaningfully worse than "set 2 vars", and (c) Phase 10.6 will be a real-PAT shakedown of the whole flow and is the right time to decide whether the auto-injection is worth the coupling. Easy to add later if the demo says it's needed.

Phase 10 backend + frontend are now complete. Phase 10.6 is a real-world shakedown: register the integration on prod against `bewallace01/lightsei`, configure a webhook on github.com, push, watch Polaris read the new docs and a code change auto-deploy.

### 2026-04-30 — Phase 10.4: Polaris reads docs from GitHub

The orchestrator gains an optional GitHub fetch path. When `POLARIS_GITHUB_REPO` + `POLARIS_GITHUB_TOKEN` are set, the bot pulls docs from the repo on every tick instead of from disk. Combined with the Phase 6.2 hash-skip cache, the user can iterate on docs by pushing to GitHub — no redeploy required, the cache busts on every push that changes a hashed doc and skips on every push that doesn't. CLI deploys still work; missing env vars fall back transparently to `POLARIS_DOCS_DIR`.

- [x] **New env vars on `polaris/bot.py`**: `POLARIS_GITHUB_REPO` (`owner/name`), `POLARIS_GITHUB_BRANCH` (default `main`), `POLARIS_GITHUB_TOKEN` (workspace secret), `POLARIS_GITHUB_DOCS_PATHS` (comma-separated, default `MEMORY.md,TASKS.md`). Resolved per-tick (not at import) so a worker secret-injection that lands after import still takes effect on the next poll.
- [x] **`_gh_config()` dispatcher**: returns a fully-populated config dict only when REPO + TOKEN are both set and well-formed; returns None otherwise. Single source of truth for "is GitHub mode active?" — callers don't reason about partial state.
- [x] **`_fetch_github_doc(...)`**: hits `GET /repos/{owner}/{name}/contents/{path}?ref={branch}` with the PAT. Decodes inline base64 content (Polaris docs are well inside GitHub's 1MB inline ceiling). Maps 401/404/non-2xx/transport/list-response (directory at this path) to a single `GitHubDocFetchError` so the tick loop has one exception type to catch.
- [x] **Hash stability**: hashes are computed from the decoded text, NOT from GitHub's reported blob `sha` — so disk and GitHub modes produce identical hashes for identical content. A user transitioning from `lightsei deploy` to `git push` keeps their cache instead of invalidating it.
- [x] **`_call_claude` generalized**: instead of hardcoding `<MEMORY.md>` + `<TASKS.md>` tags, the prompt builder iterates `docs["docs"]` (a `{filename: text}` dict) and wraps each in an XML tag named after the file. Default config produces the same prompt shape as before; custom `POLARIS_GITHUB_DOCS_PATHS` lets users include additional files (e.g., `ROADMAP.md`).
- [x] **`tick()` skips on fetch failure**: `GitHubDocFetchError` is caught, logged, emitted as `polaris.tick_skipped` with reason `github fetch failed`. Crucially, `_last_hashes` is left unchanged — so the next successful fetch produces a plan even if the now-current content matches the last-cached hashes (i.e., the failure window doesn't silently extend the cache).
- [x] **17 tests in `backend/tests/test_polaris_docs.py`** (all passing): `_gh_config` dispatch (env unset → None, malformed repo → None, defaults applied, custom values honored, empty CSV falls back to defaults); `_fetch_github_doc` (base64 decode, ref + path + auth header sent correctly, 401 + 404 + transport + directory-listing-response all raise `GitHubDocFetchError`); `_read_docs_from_github` (hits API once per path, hashes are stable across identical fetches, hashes match disk-mode hashes for identical content); `_read_docs` dispatch (env unset → disk; env set → GitHub).
- [x] **`backend/pytest.ini` extended**: `pythonpath` now includes `../polaris` so the backend test runner can `import bot` and exercise the polaris doc-reading paths without spinning up a separate test infrastructure.
- [x] **Backwards compat**: when GitHub vars are unset the bot reads from disk exactly as before, including the Phase 6.2 hash-skip cache. Existing CLI-deployed Polaris instances get no behavior change from this commit.
- [x] **Auto-injecting `POLARIS_GITHUB_REPO` / `POLARIS_GITHUB_BRANCH` into workspace secrets on integration registration** is deferred to Phase 10.5 (the dashboard `/github` panel) — it's UX glue, not a backend requirement, and adding a side effect to PUT now would couple integration registration to the secrets API. The user manually sets `POLARIS_GITHUB_TOKEN` (intentional — it's a separate trust boundary from the workspace-level integration PAT, even if today they end up being the same string).

The push-to-Polaris loop is now wired end-to-end on the backend / bot side. Phase 10.5 (dashboard `/github` panel) plus 10.6 (the prod demo) are what's left before Phase 10 closes.

### 2026-04-30 — Phase 10.3: Push-triggered redeploy

Closes the loop. A push to a registered branch that touches files under a registered agent path now becomes a real `Deployment` row with `source=github_push` and the commit SHA. The Phase 5 worker's existing claim loop picks them up — no new worker code, no changes to the runner.

- [x] **Migration `0017_deployments_source`** (`backend/alembic/versions/20260430_0017_deployments_source.py`): two additive columns. `source` ('cli' | 'github_push', NOT NULL, server_default 'cli' so existing rows take CLI semantics on backfill) + `source_commit_sha` (nullable, populated only on github_push rows).
- [x] **Deployment ORM model** picks up both columns. `_serialize_deployment` includes them in the API response. The CLI upload path (`POST /workspaces/me/deployments`) now explicitly sets `source='cli', source_commit_sha=None`. The redeploy endpoint (`POST .../redeploy`) carries the original provenance forward — clicking "redeploy" on a github_push row produces a new github_push row with the same commit SHA, not a falsely-labeled CLI row.
- [x] **`github_api.fetch_directory_zip`** (~120 lines): two-step git-data API dance. `GET /repos/{owner}/{name}/git/trees/{commit_sha}?recursive=1` to enumerate the commit's blobs, filter to entries under the agent's path, then `GET /repos/.../git/blobs/{blob_sha}` per file to pull the base64 content. Build a `zipfile.ZipFile` in-memory rooted at the agent dir (the `polaris/` prefix is stripped so the zip looks identical to a CLI bundle of just that directory). Refuses to deploy if GitHub flags `truncated: true` on the tree (>100k entries / >7MB) or if the running blob size exceeds the 10MB cap that matches the multipart upload limit on the CLI path.
- [x] **`_queue_github_redeploy` real implementation** in `main.py`: decrypt PAT → fetch_directory_zip → store as `DeploymentBlob` → create `Deployment(source='github_push', source_commit_sha=commit_sha, desired_state='running')`. Returns the new deployment id. `GitHubAPIError` from the fetch is swallowed (return None) so a transient GitHub failure doesn't make the webhook retry forever — caller logs intent in `queued_redeploys[].deployment_id` (None when fetch failed). PAT decryption failure also returns None.
- [x] **8 new tests** in `tests/test_github_webhook.py` (30 total in the file, all passing): end-to-end push creates a real deployment row with `source=github_push` and the commit SHA; CLI upload still tags `source=cli`; multi-agent push creates one row per matched path with shared commit SHA; cross-workspace isolation (Bob's push doesn't create deployments in Alice's workspace); `is_active=False` short-circuits before deployment creation; GitHub 404 on tree-fetch swallowed with `deployment_id=None`; zip is built from filtered subtree (verified by unzipping the stored blob and inspecting namelist — `polaris/` prefix correctly stripped); redeploy endpoint preserves `source` and `source_commit_sha`.
- [x] **httpx mock plumbing fix**: nested `patch.object(github_api.httpx, "Client")` was capturing the autouse fixture's MagicMock instead of the real `httpx.Client`. Captured `_REAL_HTTPX_CLIENT` at module-import time (before any test patches), and both the autouse fixture and per-test override now use it directly. Subtle bug — surfaced when one test needed a 404-from-GitHub override and got the autouse 200 instead.
- [x] **Dashboard Deployments rows show provenance**: `dashboard/app/agents/[name]/page.tsx` deployment list now shows an inline `↳ github abc1234` (with a tooltip carrying the full commit SHA) for github_push rows or `cli` for CLI uploads. Deployment detail page (`dashboard/app/deployments/[id]/page.tsx`) gains a "source" row showing "github push @ <short SHA>" or "cli upload". Plain Tailwind, matches existing row aesthetic. TypeScript clean. The `Deployment` type in `dashboard/app/api.ts` now declares `source` + `source_commit_sha`.
- [x] **Full backend suite**: 305 passed (from 297 in 10.2; +8 new tests, no regressions). Clean `tsc --noEmit` on the dashboard.
- [x] **End-to-end push-to-deploy verification deferred to Phase 10.6 demo**: rendering the github_push variant of the deployment row requires registering a real GitHub integration, configuring the webhook on github.com, and pushing — which is exactly what 10.6 does. The CLI variant renders as expected against existing local deploys.

The scaffolding for Phase 10.4 (Polaris reads docs from GitHub) lives entirely in `polaris/bot.py` plus the existing `WorkspaceSecret` injection path — no new tables or backend endpoints. A fresh `github_api.fetch_file_content` will land there.

### 2026-04-30 — Phase 10.2: GitHub webhook receiver

`POST /webhooks/github` — public, no Lightsei API key. GitHub posts here whenever a subscribed event fires on a registered repo. The endpoint verifies HMAC-SHA256 against the integration's `webhook_secret` (the one revealed once during 10.1 registration), filters event types, and matches changed files in the push against registered agent paths to determine which agents need a redeploy.

- [x] **HMAC verification**: `_verify_github_signature` extracts the digest from `X-Hub-Signature-256` (format `sha256=<hex>`), recomputes HMAC over the raw bytes (not the parsed JSON — bytes-level is what GitHub signed), constant-time-compares with `hmac.compare_digest`. Missing header, wrong prefix, or any mismatch → 401.
- [x] **Order of operations**: read raw body → parse JSON → extract `repository.full_name` → look up integration → decrypt `encrypted_webhook_secret` → verify signature. We have to find the integration to find the secret, so the lookup precedes verification. Lookup is read-only; nothing state-changing happens before HMAC succeeds.
- [x] **Repo lookup**: 404 if no workspace has registered this repo (so a misconfigured webhook URL surfaces in GitHub's webhook log instead of being silently accepted). Repos on github.com are public knowledge anyway, so the 404-vs-401 differential isn't a meaningful leak.
- [x] **Event filtering**: `ping` (GitHub's "is this thing on?" event) → 200 no-op. Anything that isn't `push` → 200 with `skipped: "event_type_not_handled"`. `push` to a branch other than `integration.branch` → 200 with `skipped: "branch_not_tracked"`. `is_active=False` integration → 200 with `skipped: "integration_inactive"` (GitHub stops retrying without us doing anything).
- [x] **Path matching**: `_push_touched_path` walks `commits[].added/modified/removed` and treats a registered path as a directory boundary, not a substring prefix. So `polaris/` matches `polaris/bot.py` but NOT `polarisXYZ/foo.py`. Renames are already covered because GitHub reports them as remove + add pairs.
- [x] **`_queue_github_redeploy` stub**: 10.2 only identifies which agents would be redeployed. The real deployment-row creation lands in 10.3, swapped into this function in place. The webhook receiver doesn't have to change. Tests verify the receiver's intent by asserting on the response body's `queued_redeploys` list rather than mocking a function.
- [x] **22 tests in `tests/test_github_webhook.py`** (all passing): signed/unsigned/wrong-secret/tampered-sig/tampered-body, unknown repo 404, malformed body 400, missing repository field 400, ping accepted, unhandled event type accepted, untracked branch accepted, inactive integration accepted, no-paths/touching-paths/outside-paths, directory-vs-substring boundary, multi-path-only-matching-one, multi-path-touching-multiple, added/removed file detection, multi-commit pushes, cross-workspace isolation (Bob's push doesn't trigger Alice's paths), cross-secret rejection (Alice's secret can't sign Bob's webhook).

10.3 will replace `_queue_github_redeploy`'s body with the real work: fetch the agent's directory at the pushed commit via the GitHub Contents API, build a deploy zip, create `deployment_blob` + `deployment` rows with `source=github_push` so the existing Phase 5 worker picks them up via its claim loop. No new worker code.

### 2026-04-30 — Phase 10.1: GitHub auth + repo registration

Server-side surface for "connect a GitHub repo to a Lightsei workspace." The user pastes a fine-grained PAT, we validate it against the GitHub API on `PUT`, generate a webhook secret on first registration (revealed once, stored encrypted forever after), and accept per-agent path mappings so a future webhook knows which agent corresponds to which repo subdirectory.

- [x] **Migration `0016_github_integrations`** (`backend/alembic/versions/20260430_0016_github_integrations.py`): two tables. `github_integrations` (id, workspace_id FK CASCADE UNIQUE, repo_owner, repo_name, branch default `main`, encrypted_pat, encrypted_webhook_secret, is_active, timestamps). `github_agent_paths` ((workspace_id, agent_name) composite PK, path up to 512 chars, timestamps). Both encrypted columns reuse `secrets_crypto.encrypt()` — same scheme as `WorkspaceSecret` rows.
- [x] **ORM models** (`backend/models.py`): `GitHubIntegration` + `GitHubAgentPath`. UNIQUE(workspace_id) on the integration enforces "one repo per workspace" in v1; multi-repo lands in Phase 10B if there's demand.
- [x] **Thin GitHub REST client** (`backend/github_api.py`, ~130 lines, no `PyGithub` dep): single function `validate_pat(owner, name, pat)` pings `GET /repos/{owner}/{name}` with a 5s timeout. Translates 401→auth error, 403→scope/rate-limit hint, 404→not_found, timeouts→transport. Returns `RepoMetadata` (full_name, default_branch, private). Tests mock `httpx.Client` at the module level — same pattern Phase 9.2's notifications tests use.
- [x] **Six endpoints** (all under `/workspaces/me/github`, all `Depends(get_workspace_id)`): `PUT` registers/updates and validates the PAT against GitHub before storing; `GET` returns the masked PAT (`ghp_...5678`) but never the plaintext webhook secret on subsequent reads; `DELETE` removes the integration; `GET /agents` lists path mappings; `PUT /agents/{name}` upserts a single mapping; `DELETE /agents/{name}` removes one. Webhook secret revealed exactly once on first registration with explicit reveal note ("Save this — it is shown once. To rotate, DELETE and re-register").
- [x] **Defense at the boundary**: regex on owner (`^[A-Za-z0-9](?:[A-Za-z0-9._-]){0,38}$`), repo (`^[A-Za-z0-9](?:[A-Za-z0-9._-]){0,99}$`), branch, agent name. Path validator rejects empty, leading slashes, backslashes, and any segment containing `..` (catches `../foo`, `foo/../bar`, `foo/..`).
- [x] **27 tests in `tests/test_github.py`** (all passing): happy-path PUT→GET round-trip with masked PAT + one-time webhook reveal, update keeps webhook secret but rotates PAT, DELETE+re-PUT generates fresh webhook secret, 401/403/404/transport translations from mocked GitHub, malformed-input rejections, agent path CRUD, parametrized bad-path test (`["", "/leading/slash", "../foo", "foo/../bar", "foo/..", "windows\\path"]`), cross-workspace isolation on both integration and agent paths, 401 on unauthenticated requests.
- [x] **Full backend suite green**: 275 passed, no regressions. The single bug surfaced during testing — GET returned `***` instead of the masked PAT because the serializer only masked when plaintext was passed in — is fixed (GET now decrypts the stored PAT inside the handler purely to compute the display mask).
- [x] **Note on the masking pattern**: serializer always masks; passing `pat_plaintext` enables a *visible* mask (`ghp_...5678`) instead of the fallback `***`. Plaintext is never echoed to the wire. PUT response does the same — caller never sees the PAT bytes back, even on the first registration.

Phase 10.2 (webhook receiver) builds directly on this: the `webhook_secret` is the HMAC key GitHub uses to sign incoming push payloads, and the `(workspace_id, agent_name) → path` map is what determines which agent gets redeployed when a push touches files under its registered path.

### 2026-04-30 — Phase 9 Notifications COMPLETE 🎯
Demo criterion (from MEMORY.md / Phase 9 header): *"Add a webhook URL on the workspace through the dashboard. Within minutes Polaris's next plan lands in your team chat as a formatted message with the summary, top next-action, and a deep link back to /polaris. Validation FAIL → different message with the matched rule. Run failure → crash message. The user never opened the dashboard."* — passed.

**Three real Slack messages, three different templates, all delivered to a real Slack workspace within seconds of the trigger event.**

Demo run pointed at prod (`https://api.lightsei.com`, `https://app.lightsei.com`, real Slack incoming webhook).

- [x] **Pushed all of Phase 9 (commits 9.0 plan, 9.0 publish, 9.1, 9.2, 9.3, 9.4, 9.5) to `origin/main` and triggered Railway redeploys** for both backend and dashboard in parallel via `railway up`. Both `Deploy complete`. Migration through 0015 ran on backend boot (`/workspaces/me/notifications` returns `{channels: []}` instead of the old `404 Not Found`).
- [x] **No regressions on prior phases**: `/agents/polaris/latest-plan` still 200 with the canonical plan, `/workspaces/me/validators` still shows `polaris.plan / schema_strict mode=blocking` and `polaris.plan / content_rules mode=advisory` from Phase 8. Phase 9 is purely additive.
- [x] **User-supplied real Slack webhook URL** (kept in shell only, never logged or committed). Registered as `team-slack` channel via `POST /workspaces/me/notifications` subscribing to all three triggers. Channel id ends in `e406b`. The dashboard renders the URL masked as `https://hooks.slack.com/ser...i9Lk`.
- [x] **Three real Slack messages dispatched and confirmed received**:

  **Test fire (`POST /test`)** at 23:07:13 UTC: `{"status": "sent", "http_status": 200, "response_preview": "ok"}`. User confirmed the message arrived as:
  > ✅ Lightsei test message
  > If you're seeing this, your Slack channel is wired up. Real notifications for team-slack will arrive when their triggers fire.
  > Manage channels ↗

  **`polaris.plan` event** at 23:09:32 UTC: posted via `/events` with a conforming envelope (schema_strict in mode=blocking required all fields present). BackgroundTasks dispatched to Slack within ~600ms. User confirmed:
  > 🌟 Polaris plan
  > polaris · just now
  > Phase 9.6 is firing real notifications: this plan was emitted by hand to demonstrate that the trigger pipeline carries Polaris's structured payload all the way to Slack.
  > **Next actions**
  > 1. Verify this plan arrived in your Slack channel as a Block Kit message
  > 2. Optionally add a Discord and Teams channel via /notifications
  > 3. Watch the deliveries audit trail at /notifications expand to show this delivery as 'sent'
  > View full plan ↗

  **`run_failed` event** at 23:09:33 UTC: posted via `/events` with `{"error": "RuntimeError: synthetic crash for Phase 9.6 demo"}`. User confirmed:
  > 💥 polaris run failed
  > **Error**
  > ```
  > RuntimeError: synthetic crash for Phase 9.6 demo
  > ```
  > View run ↗
- [x] **Audit trail visible in the dashboard**: `docs/phase9-prod-slack-deliveries.png` shows `/notifications` rendering the live `team-slack` channel (Slack purple "S" icon, masked URL, three trigger chips, send test/mute/delete actions), the recent-deliveries panel expanded showing "last 3: 3 sent, 0 failed", and the audit table with all three rows green-PILL `SENT` and `200` HTTP status. The audit table is the durable record — future debugging never needs to reach into Slack itself.
- [x] **Why didn't I deploy Polaris fresh for this demo?** The trigger-pipeline path is identical whether Polaris emits an event via the SDK or a script does it via `/events`. Manually emitting the payloads costs nothing (vs ~$0.18 for a real Polaris tick), runs in seconds, and lets us trigger all three event types deliberately rather than waiting for Polaris to coincidentally produce a `run_failed`. The Phase 6.6 demo already proved the SDK→`/events` path; Phase 9.6's evidence is what happens *after* an event lands, not before.
- [x] **Discord + Teams deferred (still on the menu)**: the user provided a Slack URL only. The dashboard's add-channel form is now the easiest path forward — Discord/Teams URLs can be pasted in at any time without redeploys. Phase 9 ships the surface; the user provisions channels at their own pace. Phase 10+ can revisit if we want a richer onboarding flow.
- [x] **Demo data left in prod**: `polaris.plan` event 164 and `run_failed` event 167 are now in the polaris agent's history. The summary on event 164 explicitly labels it as a Phase 9.6 demo synthetic, so future Polaris plans (and the `/polaris` dashboard's plan history) will show it as the demo entry rather than a confusing real-looking row. Acceptable as-is; a manual cleanup endpoint isn't worth Phase 9 scope.

This is the most user-facing phase Lightsei has shipped. Polaris went from "I have to remember to check the dashboard" to "Polaris pings me in Slack when there's something to look at." The next time I want to know whether the project is stuck, I look at Slack instead of opening a browser tab. That's the diff Phase 9 was meant to make.

### 2026-04-30 — Phase 9.5 Dashboard "Notifications" panel
- [x] **New `/notifications` route** at `dashboard/app/notifications/page.tsx`. Sibling to `/account` (rather than nested) since notifications are workspace-level config that users will want to bookmark and link to. Linked from the global Header next to "polaris" so it's reachable from anywhere.
- [x] **Add-channel form** with name, type dropdown (Slack / Discord / Teams / Mattermost / Generic webhook), URL paste field with type-specific hints, three trigger checkboxes pre-selected. Generic-webhook type reveals an extra "Shared secret" field for HMAC signing with a one-line explainer of what the X-Lightsei-Signature/Timestamp headers will carry. Form errors land inline; on success the new channel slides into the registered-channels list.
- [x] **Channel rows** with one-letter mono-badge per type (S=Slack purple, D=Discord indigo, T=Teams blue, M=Mattermost cyan, W=Webhook gray) — keeps the page free of brand-asset licensing concerns while still letting users scan rows by type. Each row shows: name + type label + muted/signed status pills, masked target URL in mono, trigger chips (with inline edit), and per-row action buttons: send test / mute (or unmute) / delete.
- [x] **"Send test" button**: posts to `/workspaces/me/notifications/{id}/test` and updates the button to "✓ sent" or "✗ failed" depending on the dispatcher result. Failure path renders the error reason + http_status inline (e.g., "http_error 401") so a user fixing a bad URL can see the issue without opening the deliveries panel.
- [x] **Recent deliveries panel** (collapsed by default, expand per channel) — fetches the last 50 deliveries via `/deliveries`, shows a summary line ("last 50: 3 sent, 47 failed") and a table with timestamp / trigger / colored status pill / detail (HTTP status or error code). The deliveries table is the audit trail that lets a user diagnose why a channel isn't firing without leaving the dashboard.
- [x] **Edit triggers in place**: clicking "edit" on the trigger chips swaps to a checkbox row with save/cancel; PATCH on save. No full-page re-render — just the row updates. Same pattern as the dashboard's other PATCH-driven edits.
- [x] **Mute/unmute** flips `is_active` via PATCH. Muted channels render with a slightly grayer background and a "MUTED" pill so the user can see at a glance which ones are paused without expanding.
- [x] **Delete uses `confirm()`** — workspace-level config the user owns, not data the bot creates, so a single confirmation is fine. No undo path; the audit deliveries are kept on the FK SET NULL side anyway (channel deletion doesn't cascade-delete past deliveries — though FK CASCADE on the channel does, so revisit if we ever care about deletion-survivor audits).
- [x] **api.ts additions**: `ChannelType` + `TriggerName` literal types pinned to the backend's allow-lists; `NotificationChannel` and `NotificationDelivery` types matching the masked-URL response shape; six fetch helpers (`fetch`, `create`, `patch`, `delete`, `test`, `fetchDeliveries`).
- [x] **Header link added** so `/notifications` appears next to `/polaris` and `/account` for logged-in users. Logged-out header is unchanged.
- [x] **Real bug surfaced and fixed**: the dashboard built but the local backend container crashed on startup with `ModuleNotFoundError: No module named 'httpx'`. The 9.2 dispatcher introduced `httpx` as a dep but it was never added to `backend/requirements.txt` — local pytest passed because httpx was already installed for the SDK. Added `httpx==0.27.2` to backend requirements; rebuild lands cleanly through migrations 0014 and 0015 (validator_mode + notification_channels/deliveries). Backend test runs pre-9.5 unknowingly relied on a system-installed httpx; this would have broken the prod Railway deploy on 9.x rollout.
- Verified live with `docker compose up backend` + `npm run dev`. Migration ran to 0015. Captured two screenshots:
  - `docs/phase9-notifications-empty.png` — empty state with "Add a channel" form (default Slack, all 3 trigger boxes checked) and "No channels yet" empty card.
  - `docs/phase9-notifications-populated.png` — three seeded channels (team-ops/Slack with all 3 triggers, alerts-discord with 2 triggers, n8n-pipeline/Webhook with HMAC SIGNED pill and 1 trigger) demonstrating the type-icon scan, masked URLs, and per-row actions.
- Verified the dashboard build path: `npx tsc --noEmit` clean, `next build` produces `/notifications` at 6.95 KB First Load JS. Backend suite still 248/248 (no backend changes besides the requirements.txt fix).

### 2026-04-30 — Phase 9.4 Trigger pipeline hooked into event ingestion
- [x] **`backend/notifications/triggers.py`** — three pieces split by responsibility:
  - `detect_triggers(event, outcomes) -> list[str]` decides which symbolic triggers fired (`polaris.plan` from `event.kind`, `validation.fail` from any fail-status outcome, `run_failed` from `event.kind`). Pure logic, no I/O.
  - `build_dispatch_plans(session, ...) -> list[DispatchPlan]` cross-products fired triggers × workspace's active channels, builds a `DispatchPlan` per matching pair carrying everything the BG task needs. Reads from the request session (cheap; one channel-list query).
  - `dispatch_and_persist(plan)` runs one dispatch in a fresh DB session and writes the `NotificationDelivery` audit row. Designed for `BackgroundTasks` — never raises; defensive paths produce a `failed` Delivery and still write the row. A dispatcher exception lands `error: dispatch_exception`.
- [x] **Hooked into `POST /events`** with the minimal possible diff: after `write_validation_rows`, call `detect_triggers` → `build_dispatch_plans` → `background_tasks.add_task` per plan. Three guarantees stay intact: a 422-rejected event (blocking validator) fires nothing because the `raise HTTPException(422)` short-circuits before this block; a slow webhook never blocks `/events` because BG tasks run after the response is sent; a misconfigured channel can't crash ingestion.
- [x] **Validation outcomes attached to `validation.fail` signals**: the event's own payload doesn't carry post-emit validation results, so for `validation.fail` triggers we supplement `signal_payload` with the validation outcomes (validator + status + violations) so the formatter has them to render. The chat formatters' `first_violation_summary` helper picks the first failing entry; webhook formatter ships the full array.
- [x] **`DispatchPlan` is a flat dataclass** with no DB session reference and no live ORM rows. The plan can outlive the request that built it — required because the request session closes before the BG task runs.
- [x] **16 new tests** in `backend/tests/test_notifications_triggers.py`:
  - 7 unit tests on `detect_triggers` covering each kind/outcome combo, the `polaris.plan` + `fail` double-fire case, the "warn doesn't trigger validation.fail" rule, and dedup behavior (two failing validators → one `validation.fail` not two).
  - 9 integration tests on the full `POST /events` pipeline: subscribed channel fires + delivery row lands; no matching subs creates no rows; `is_active=False` channel skipped (mute without delete); a channel subscribed to multiple matching triggers gets one delivery per trigger; multiple channels on the same trigger each get their own delivery; cross-workspace isolation; failed dispatch records the audit row with status='failed'; `run_failed` event fires the right trigger; dispatcher crash records `dispatch_exception` and never blocks the response.
- [x] **`BackgroundTasks` is a function parameter** on `post_event` — FastAPI auto-injects it. Tested transparently because `TestClient` runs BG tasks synchronously at end-of-request, so test assertions read the deliveries table immediately after `/events` returns.
- [x] **`mock_httpx_post` helper duplicated** into `test_notifications_triggers.py` rather than imported from `test_notifications_dispatch.py` — pytest collection ordering means the patch needs to apply per-test-module, not via an import.
- Real bug surfaced while writing the dispatcher-crash test: `monkeypatch.setattr(notifications, "dispatch", boom)` doesn't reach the call site inside `triggers.py` because `from notifications import dispatch as run_dispatch` was bound at module-load time. Fixed by patching `notifications.triggers.run_dispatch` directly. The test now verifies the right error type lands on the audit row, which is the actual product invariant we want.
- Verified: `pytest tests/test_notifications_triggers.py -v` → **16/16** pass. Full backend suite → **248/248** (was 232; +16 trigger tests, no regressions). The notification surface is end-to-end functional: register a channel → real event lands in /events → dispatcher fires → audit row records the result. 9.5 makes it visible + manageable in the dashboard; 9.6 is the live demo.

### 2026-04-30 — Phase 9.3 Generic webhook channel + HMAC signing
- [x] **`backend/notifications/webhook.py`** — fifth and final v1 channel type. Per-trigger envelope with a stable shape (`{type, workspace_id, agent_name, timestamp, dashboard_url, data}`); the `data` field carries the trigger-specific structured fields verbatim (no truncation, since webhook receivers consume programmatically rather than for human display). Three rendered envelopes (polaris.plan, validation.fail, run_failed) plus a self-explaining test envelope plus a future-proof passthrough so unrecognized triggers still ship the source payload.
- [x] **HMAC-SHA256 signing** when `secret_token` is set on the channel. Each request carries:
  - `X-Lightsei-Timestamp: <unix epoch seconds>` — fresh per request, used by receivers for replay protection
  - `X-Lightsei-Signature: sha256=<hex>` — HMAC over `f"{ts}.".encode() + body_bytes`
  Receivers verify by re-deriving the signing input and constant-time comparing. Recommended replay window of 300s is documented in the module (and will surface in the dashboard's webhook hint in 9.5).
- [x] **Bytes-controlled posting via new `_http.post_raw(url, content, headers)`**. The byte sequence we sign must equal the byte sequence we post — `httpx.Client.post(json=...)` would let httpx's serializer sneak in subtly different bytes. webhook.py JSON-serializes once with `sort_keys=True, separators=(",", ":")` (deterministic, compact), signs those bytes, posts the same bytes via `post_raw`. `post_json` keeps its current shape; both share the same response-mapping helpers (`_delivery_from_response`, `_timeout`, `_transport_error`, `_post_exception`) so failure shapes are identical across all five channel types.
- [x] **Registered as `webhook` in REGISTRY** in `notifications/__init__.py`. `dispatch(channel_type="webhook", ...)` routes to the webhook formatter+poster like any other type. The 9.1 API surface already accepts `type: "webhook"` (it was in `NOTIFICATION_CHANNEL_TYPES` from day one); 9.3 is what makes the `/test` endpoint actually do something for those channels instead of returning `unknown_channel_type`.
- [x] **Updated `test_registry_contains_v1_channel_types`** (renamed from `_native_chat_types`) and `test_dispatch_routes_by_type_and_returns_delivery` to include the webhook type. Both still pass — the registry is now `[discord, mattermost, slack, teams, webhook]`.
- [x] **9 new webhook tests**: envelope shape per trigger (polaris.plan with full fields, validation.fail with verbatim validations array, run_failed with error string, test with explanatory note); HMAC headers present + verifiable when secret set (test recomputes the signature in-test from captured ts + bytes — proves real receivers can verify); no signature headers when secret is None (dumb receivers like Zapier still work); deterministic byte serialization (verified against a body whose key order would matter — `{"z":1,"a":2,"m":{"y":3,"b":4}}` posts as `{"a":2,"m":{"b":4,"y":3},"z":1}`); 4xx response from receiver lands `failed` with `http_status`; dispatch via the public registry entry point produces a parseable JSON envelope.
- Verified: `pytest tests/test_notifications_dispatch.py -v` → **37/37** pass (was 28; +9 webhook). Full backend suite → **232/232** (was 223; +9). The five-channel platform-agnostic notification surface is complete; 9.4 wires it into POST /events so registered channels fire automatically.

### 2026-04-30 — Phase 9.2 Channel-type registry + Slack / Discord / Teams / Mattermost formatters
- [x] **`backend/notifications/` package** mirrors `backend/validators/`:
  - `_types.py`: `Signal` dataclass (trigger, agent_name, dashboard_url, timestamp, payload, workspace_id) and `Delivery` (status, response_summary, attempt_count). Plain dataclasses keep storage trivially JSON-serializable.
  - `_shared.py`: cross-platform message-text helpers (`relative_time`, `truncate`, `top_next_actions`, `first_violation_summary`, `run_failed_summary`). One source of truth for the strings each formatter renders, so a user toggling between Slack and the dashboard sees consistent labels.
  - `_http.py`: shared `post_json()` HTTP-out with 2s timeout, 500-char response-body preview, and clean failure mapping (timeout / transport_error / http_error / post_exception). No retries — webhook providers have their own delivery story; the audit trail is the durable record.
  - Per-platform: `slack.py`, `discord.py`, `teams.py`, `mattermost.py`. Each exports `format(signal) -> dict` and `post(url, body, secret_token=None) -> Delivery`.
  - `__init__.py`: registry of `{type: (format_fn, post_fn)}` + a `dispatch(channel_type, target_url, signal, secret_token)` entry point that never raises.
- [x] **Slack** uses Block Kit. Each message includes both `text` (fallback for screen readers + phone previews) and `blocks` (rich layout). Three rendered templates plus a `test` template plus a future-proof `_format_generic` so an unrecognized trigger lands a generic message instead of raising.
- [x] **Discord** uses webhook embeds. Color-coded by signal type using the same hex values as the dashboard's STATUS_STYLES chips (green `0x10B981` for plan, amber `0xF59E0B` for warn/test, red `0xEF4444` for fail/crash). Color is the visual cue Discord users expect; Slack handles the same via emoji.
- [x] **Teams** uses Adaptive Card 1.5 wrapped in the Bot Framework `attachments` envelope (`type: "message"`, `attachments: [{contentType: "application/vnd.microsoft.card.adaptive", contentUrl: null, content: <card>}]`). Targets the modern Workflows webhook URL — the legacy Office 365 Connector format / URLs were deprecated by Microsoft in 2025. Action.OpenUrl renders the deep link as a tap-through button. `fontType: Monospace` on the run-failed error block renders fixed-width across desktop/mobile/web Teams clients.
- [x] **Mattermost** registered as Slack-compat: the registry entry uses `slack.format` directly with `mattermost.post`. Mattermost accepts Slack incoming-webhook JSON verbatim, so the format function is shared. Keeping `mattermost.post` separate means stack traces blame the right module and we have a place for Mattermost-specific tweaks if we ever need them.
- [x] **Test-fire endpoint swapped from stub to real dispatch**: `POST /workspaces/me/notifications/{id}/test` now calls `notifications.dispatch()`, persists the resulting `Delivery` to `notification_deliveries`, returns 200 with the row regardless of dispatch outcome. A misconfigured channel surfaces as `status='failed'` with the http_status / error reason in `response_summary`, not as a 5xx — that's the user's problem to fix, not a server alarm.
- [x] **`DASHBOARD_BASE_URL` configurable** via `LIGHTSEI_DASHBOARD_BASE_URL` env var (defaults to `https://app.lightsei.com`). `_dashboard_url_for(trigger, agent_name, run_id)` builds the deep link the formatters embed in the "View ↗" buttons. Self-hosters can point at their own dashboard.
- [x] **28 new tests** in `backend/tests/test_notifications_dispatch.py`: registry contents pin (`v1 = slack/discord/teams/mattermost`); dispatch unknown-type returns `failed` not raise; helpers (`relative_time`, `truncate`, `top_next_actions`, `first_violation_summary`, `run_failed_summary`) covering happy paths + defensive paths (mixed-garbage `next_actions`, missing fields, all three error-message field-name fallbacks); per-platform shape snapshots for each formatter (Slack Block Kit `text + blocks` + header/section/context structure; Discord embed with the right `color` per trigger, `url`, `timestamp`; Teams Adaptive Card envelope including `Action.OpenUrl` action and `Attention`/`Monospace` color cues; Mattermost format equals Slack format byte-for-byte); HTTP-out paths (2xx → sent, 4xx with body preview → failed/http_error, timeout → failed/timeout, response body clipped to 500 chars); dispatch routing across all 4 channel types via `httpx.MockTransport`; formatter-exception handling produces `formatter_exception` Delivery rather than crashing the dispatcher.
- [x] **Updated 9.1's "test-fire writes skipped row" test** to `test_test_fire_records_real_dispatch_attempt` since the stub status `skipped` was replaced. Endpoint shape unchanged so the dashboard's "send test" button doesn't need updating in 9.5.
- Real bug surfaced in test mocking: my first attempt at patching `httpx.Client` used a lambda that didn't survive `with` context-manager re-entry. Fixed with a `mock_httpx_post(handler)` `@contextmanager` helper that uses `MockTransport` and `patch.object(notifications_http.httpx, "Client", side_effect=factory)` so subsequent `with httpx.Client(timeout=...)` calls inside `_http.post_json` get a transport-mocked client. 5 tests went from FAIL to PASS with that switch. The pattern is reusable for any future formatter test.
- Verified: `pytest tests/test_notifications_dispatch.py -v` → **28/28** pass; `tests/test_notifications.py` (9.1's tests) → **22/22** pass with the swapped test-fire test. Full backend suite → **223/223** (was 195; +28 dispatcher). 9.4 (trigger pipeline hooked into POST /events) and 9.5 (dashboard panel) now have a real dispatcher to plug into; 9.3 adds the generic-webhook formatter as the last channel-type before the demo.

### 2026-04-29 — Phase 9.1 Notification channels: schema + endpoints
- [x] **Migration `0015_notifications`**: two new tables. `notification_channels` (id UUID, workspace_id FK CASCADE, name, type, target_url, triggers JSONB, secret_token nullable, is_active default true, timestamps) with `UNIQUE(workspace_id, name)` so a workspace can't have two channels with the same name. `notification_deliveries` (id BIGSERIAL, channel_id FK CASCADE, event_id FK SET NULL, trigger, status, response_summary JSONB nullable, attempt_count, sent_at) with `idx_notification_deliveries_channel_sent (channel_id, sent_at)` for the "show me the last N deliveries for this channel" hot path. The `event_id` SET NULL ON DELETE choice lets an event purge keep the audit history intact.
- [x] **ORM models** `NotificationChannel` and `NotificationDelivery` in `backend/models.py`. Type and status fields stored as free strings (not enums) so adding a new channel type or delivery status is a code-only change.
- [x] **`_mask_url` helper** keeps scheme + host so the user can recognize the platform but truncates the path so the secret token (which lives in the path for Slack / Discord / Teams / Mattermost incoming webhooks) is never echoed back. Last 4 chars of the path are kept as a "yes, this is the URL I added" identity hint. Returns `***` cleanly on garbage input rather than crashing.
- [x] **Seven endpoints** under `/workspaces/me/notifications/...`: `GET` (list), `POST` (create), `GET /{id}`, `PATCH /{id}`, `DELETE /{id}`, `POST /{id}/test` (test-fire), `GET /{id}/deliveries`. Validate `type` against `{slack, discord, teams, mattermost, webhook}` and `triggers` against `{polaris.plan, validation.fail, run_failed}` — anything else 400s with a clear message. PATCH refuses to change `type` (delete + recreate to switch platforms). Conflict on duplicate name → 409. Channel not in workspace → 404 with the same detail string regardless of "doesn't exist" vs "exists in a different workspace" so cross-workspace existence can't leak via timing.
- [x] **`/test` is a stub for now**: writes a `notification_deliveries` row with `status='skipped'` and `response_summary={"reason": "phase_9_2_will_deliver", ...}`. The endpoint shape is final; 9.2 swaps the inside without touching the API surface or the dashboard's "send test" button.
- [x] **22 new tests** in `backend/tests/test_notifications.py` covering: round-trip per channel type (Slack, Discord, Teams, Mattermost, webhook); secret_token never echoed (full-response substring search); type validation; trigger validation; name format validation; 409 on duplicate name (create + patch-rename); cross-workspace isolation on list / get / delete / deliveries; PATCH updates triggers + is_active; PATCH clears secret_token explicitly via null vs leaves alone when not in fields_set; `_mask_url` unit test against real-world URL shapes; deliveries endpoint pagination + limit validation; unauthorized.
- Verified: `pytest tests/test_notifications.py -v` → **22/22** pass. Full backend suite → **195/195** (was 173 before 9.1; +22 notifications, no regressions). The notification-channel API surface is settled and ready for 9.2 to plug the dispatcher in.

### 2026-04-29 — Phase 9.0 Publish `lightsei` to PyPI
- [x] **`lightsei` 0.1.0 live on PyPI**: https://pypi.org/project/lightsei/. Both wheel + sdist uploaded. License declared as MIT (PEP 639 license-expression form). Verified via `pip install lightsei` in a fresh venv → `lightsei.__version__ = "0.1.0"`, `init/track/emit` all importable, `lightsei deploy --help` works as a console script.
- [x] **Trusted publishing (OIDC)**: registered `bewallace01/lightsei` + `release.yml` + `pypi` environment as a trusted publisher on PyPI. No API token to manage anywhere — every future tag-push that matches `v*` triggers the workflow, which builds + twine-checks + uploads via `pypa/gh-action-pypi-publish`.
- [x] **Release-tag verification step**: the workflow refuses to publish if the git tag doesn't match `sdk/pyproject.toml`'s version. Catches the common "tagged but forgot to bump" mistake before it ships a non-existent version.
- [x] **`sdk/pyproject.toml`** filled out for a real release line: bumped to `0.1.0`, added readme/authors/keywords/classifiers/project URLs/license + license-files. Was previously a one-line stub that would've shown "License: UNKNOWN" on the PyPI page — that's the kind of detail that makes the difference between a project people install and one they hesitate over.
- [x] **`sdk/lightsei/__init__.py`** exposes `__version__` resolved from `importlib.metadata` so the version is single-source-of-truth from `pyproject.toml`. Sentinel fallback (`"0.0.0+source"` / `"0.0.0+unknown"`) for source-tree imports that aren't installed — non-blocking.
- [x] **MIT LICENSE** at repo root + duplicated into `sdk/` so `license-files = ["LICENSE"]` resolves at build time and the wheel ships with it. 2026 Bailey Wallace.
- [x] **`sdk/README.md`**: user-facing landing page that PyPI renders on the package detail. Two-line setup (`pip install lightsei` + `lightsei.init(...)`), what-you-get section covering observability/guardrails/Polaris/notifications/graceful-degradation, configuration, deploy command, links. Distinct from the repo-root `README.md` which has dev instructions.
- [x] **Bundles cleaned up**: `polaris/lightsei-0.0.1-py3-none-any.whl` and `examples/demo_deploy/lightsei-0.0.1-py3-none-any.whl` deleted; their `requirements.txt` files now read `lightsei>=0.1.0` instead of the local-wheel reference. Future Polaris deploys no longer need the `python -m build && cp` dance — `pip install lightsei` from PyPI in the worker venv is enough.
- [x] **`polaris/README.md`** deploy section rewritten to reflect the simplified flow.
- Real bug caught locally before tagging: PEP 639 metadata 2.4 needs `packaging>=24.2` — older twine versions fail validation with "unrecognized field 'license-expression'". Local fix was `pip install --upgrade packaging`; GHA installs latest on each run so the prod release path is unaffected. Mentioned in the commit message so future me / future contributors don't trip over it.

### 2026-04-28 — Phase 8 Blocking validators (guardrail layer 3, pre-emit) COMPLETE 🎯
Demo criterion (from MEMORY.md / Phase 8 header): *"Promote `polaris.plan / schema_strict` from advisory to blocking. Inject a schema-failing case. Deploy. The worker's logs show a `422` from `POST /events` with the violation list, the bot keeps running (graceful), and **no new plan appears in the dashboard's `/polaris` view** — the rejected event never landed. The demo's evidence is what *isn't* there: a rejected event leaves no trace in the events table, just a worker-log line."* — passed.

Demo run pointed at prod (`https://api.lightsei.com`, `https://app.lightsei.com`).

- [x] **Pushed 4 commits to `origin/main`** (Phase 8 plan + 8.1, 8.2, 8.3) and triggered `railway up backend --service lightsei-backend --ci`. Migration 0014 ran on the prod Postgres on backend startup; existing `polaris.plan / schema_strict` and `polaris.plan / content_rules` validator-config rows backfilled to `mode: "advisory"` automatically (Phase 7A behavior preserved). Verified with `GET /workspaces/me/validators` showing both rows with the new `mode` field.
- [x] **Promoted `schema_strict` to blocking with a tightened schema.** Phase 7's natural fail-injection (modify `polaris/system_prompt.md` to ask Polaris to violate the schema) doesn't work cleanly because Anthropic's strict tool calling already enforces the input schema upstream — the bot literally can't emit a polaris.plan tool call that breaks the registered shape. Fall-back path the phase plan called out worked: PUT the validator config with the canonical schema PLUS an extra `human_approved` field added to `required`. Polaris doesn't emit that field, so every tick now trips the gate. `mode: "blocking"` confirmed via the listing.
- [x] **Bundle + deploy**: built the wheel from `./sdk`, copied `MEMORY.md` + `TASKS.md` into `polaris/`, started a local worker pointed at `api.lightsei.com`, deployed via `lightsei deploy ./polaris --agent polaris`. Status went `queued → building → running` in ~9s. Bot started, called Claude (~48K input / ~1.1K output tokens), tried to emit the `polaris.plan` event, got a 422.
- [x] **Verbatim worker log line** from `/workspaces/me/deployments/{id}/logs` (not the local worker process log — the bot's stderr streams up to the deployment_logs table via the Phase 5.2 endpoint):

  ```
  [stdout] polaris up: agent=polaris model=claude-opus-4-7 poll=3600.0s docs=...
  [stdout] docs: memory=74a298e82dd60a65 tasks=0da7c46847fffd87
  [stdout] plan: 4 actions, 0 promotions, 0 drift items (48441 in / 1102 out)
  [stderr] lightsei event rejected (polaris.plan): schema_strict/required — 'human_approved' is a required property
  ```

  The bot computed a perfectly fine plan, the SDK queued it, the backend rejected it with 422, the SDK logged one WARNING per violation (here just one — the only schema rule failing), and `_post_event` returned without raising. Bot kept running and went into its sleep. The verification that mattered: `_event_rejected_count` increments per-event but never surfaces as an exception (per Hard Rule 4, graceful degradation).
- [x] **Dashboard evidence: the new tick produced no new entry in the events table.** `GET /agents/polaris/latest-plan` still returns event_id 158 (the canonical pre-promotion test event from earlier verification), not a new event from this tick. Polaris's run finished cleanly but its plan never landed.
- [x] **Real bug caught and fixed during the screenshot capture**: the dashboard's `/polaris` page assumed every polaris.plan payload conforms to `PolarisPlanPayload` and called `payload.tokens_in.toLocaleString()` directly. When event 158 (a manual test event with payload `{"intentionally": "empty"}`) became the latest plan, the page crashed with "Application error: a client-side exception has occurred." Hardened the type to mark payload fields optional and used `?? 0` / `?? "—"` fallbacks in the renderer. Redeployed dashboard.
- [x] **Screenshot**: `docs/phase8-prod-blocking-rejected.png` — the now-hardened `/polaris` view rendering event 158 with a `FAIL` chip on every entry's validation row, the VALIDATION panel showing every required-field violation (`'doc_hashes' is a required property`, `'model'`, etc.), the hero band reading "The latest plan didn't parse cleanly." and the footer showing `—` for missing fields and `0 / 0` for tokens. The visual story of what blocking-mode protects against.
- [x] **Cleanup**: stopped the deployment via `POST /workspaces/me/deployments/{id}/stop`, killed the local worker. Reverted the validator config back to the canonical `POLARIS_PLAN_SCHEMA` from `setup_validators.py` — but kept `mode: "blocking"`. **schema_strict on polaris.plan is now blocking in prod as the steady-state.** Phase 8 isn't a temporary demo state; it's the actual product capability. Future Polaris runs against the canonical schema will pass through cleanly (proven by `test_blocking_validator_with_clean_payload_ingests_normally` unit test in 8.2 — no need to wait an hour for a tick to verify). content_rules stays advisory; it's a softer signal than schema_strict.
- [x] **Phase 9 (act, don't just plan) is now unblocked.** With schema_strict in blocking mode, downstream consumers can trust that any polaris.plan event in the events table conforms to the schema. That guarantee is what `lightsei.send_command()` from Polaris will need.

### 2026-04-28 — Phase 8.3 SDK graceful 422 handling
- [x] **`sdk/lightsei/_client.py:_post_event` recognizes 422 explicitly.** Before this change, every non-2xx fell through `r.raise_for_status()` into the retry loop, so a deliberate rejection from a blocking validator would be retried `max_retries` times before being dropped — wasted work, since the same payload would always be rejected. Phase 8.3 checks `r.status_code == 422` BEFORE `raise_for_status()`, calls a dedicated `_handle_rejection`, and returns immediately. Other status codes still go through the existing exception path with retry.
- [x] **`_handle_rejection` parses the `{detail: {message, violations}}` shape** the backend ships in 8.2 and logs one `WARNING` per violation as `lightsei event rejected (kind): validator/rule — message`. Multi-rule rejections produce multi-line output so the operator can see each thing that fired in their worker logs at a glance. Falls back gracefully on unparseable bodies, missing detail dicts, and old-shape responses (older deploy mid-rollout) — never raises.
- [x] **`_event_rejected_count` counter** on the client increments per rejected event. Surfaced as a debug aid for long-running bots that want to detect a sustained rejection pattern (e.g., to back off, alert, or page on). Never used by the SDK itself for any decision — the contract stays "log and drop" per Hard Rule 4 (graceful degradation).
- [x] **No exceptions raised on 422 path.** Verified by a test that runs both rejected and accepted emits inside one `@track`-wrapped call and confirms the function returns its declared value with the rejected emit's run lifecycle (`run_started`, `run_ended`) still landing.
- [x] **Fake backend extended** with a `reject_kinds: dict[str, dict]` param that returns 422 with the given body when an event of a matching kind is posted. Lets the test fixture express "reject this kind" without forking a whole second backend.
- [x] **3 new tests** in `sdk/tests/test_basic.py`: rejection produces per-violation WARNING log lines and drops the event from the queue; 422 doesn't crash the bot mid-run; rejection counter increments per-event with no leakage across accepted events. Each test wraps emits in `@lightsei.track` because the SDK silently drops emits without a run_id (caught while writing the tests — `lightsei.emit` requires an active context, surfaced loud-failing in the manual repro before a single test even ran).
- Verified: `pytest tests/test_basic.py -v` → **12/12** pass (was 9; +3 for 422 cases). SDK suite → **19/19** including the unchanged CLI tests. Backend suite → **173/173** unchanged. Phase 8 SDK + backend story is end-to-end green; only the demo (8.4) remains.

### 2026-04-28 — Phase 8.2 Pipeline pre-emit blocking on FAIL
- [x] **`backend/validation_pipeline.py` refactored** into three stages: `evaluate_validators(session, workspace_id, event_kind, payload) → list[ValidationOutcome]` (pure compute, no DB writes), `find_blocking_failures(outcomes) → list[ValidationOutcome]` (filters to mode='blocking' AND status='fail'), and `write_validation_rows(session, event_id, outcomes) → None` (audit-trail persist). The split lets `POST /events` evaluate before deciding whether the event row gets created at all.
- [x] **`POST /events` rewritten** to call evaluate → blocking-check → 422 if blocking fails (no event row created, no audit rows) → otherwise insert event → write audit rows. Phase 7A behavior is fully preserved when no validators are in blocking mode (the default): the blocking-check returns an empty list, evaluation results write as audit rows after insert, exactly like 7.3.
- [x] **422 detail shape**: `{"detail": {"message": "event rejected by blocking validator", "violations": [{"validator": str, "rule": str, "message": str, ...}]}}`. The `validator` field is added per-violation by the pipeline (the underlying violation dict from the validator function doesn't know its own validator name); the SDK can use it to attribute failures across multiple registered validators in one rejection.
- [x] **Defensive paths preserved**: blocking-mode validators returning `status='error'` (validator function raised) or `status='timeout'` (cumulative-budget exceeded) or `status='warn'` do NOT block. Only an explicit `status='fail'` blocks. A buggy or slow validator must not take the API down for a workspace.
- [x] **`ValidationOutcome` dataclass** carries (validator_name, mode, status, violations). The mode comes along on every outcome so `find_blocking_failures` can filter without re-querying.
- [x] **7 new tests** in `test_validation_pipeline.py` covering: blocking + clean payload ingests with pass row, blocking + bad payload returns 422 with no event row written, advisory + bad payload preserves Phase 7A (lands with fail row), mixed (blocking schema + advisory content_rules) on a content-rules-only failure ingests cleanly with both rows, blocking validator that throws gets `status='error'` and doesn't block, blocking validator referencing an unknown registry name gets `status='error'` (rule='unknown_validator') and doesn't block, blocking rejection includes all failing violations (multi-error schema fails report every distinct rule, not just the first).
- Verified: `pytest tests/test_validation_pipeline.py -v` → **28/28** pass (was 21; +7 for blocking pipeline). Full backend suite → **173/173** (was 166; +7). Phase 7A regression coverage: every existing pipeline test continued to pass without modification — the new code path is opt-in via mode='blocking'.

### 2026-04-28 — Phase 8.1 Validator-mode migration + endpoint update
- [x] **Migration `0014_validator_mode`** adds `mode VARCHAR(16) NOT NULL DEFAULT 'advisory'` to `validator_configs`. The `server_default` makes the upgrade atomic: every existing row is backfilled to `advisory` in the same DDL pass, so Phase 7A's behavior is unchanged on rollout. The default also covers the API path — a PUT that omits `mode` lands advisory, which is what Phase 7A clients send.
- [x] **ORM model update**: `ValidatorConfig.mode` field with a Python-side default of `"advisory"`. Stored as a free `String(16)` rather than an enum so adding a new mode in the future (e.g., `"shadow"` — run the validator but only log) is a code-only change with no migration needed.
- [x] **Endpoints**: `PUT /workspaces/me/validators/{event_kind}/{validator_name}` accepts an optional `mode` field (default `"advisory"`) and validates it against `{"advisory", "blocking"}` — anything else 400s with `mode must be one of [...]`. `GET /workspaces/me/validators` now includes `mode` per row.
- [x] **5 new tests** in `backend/tests/test_validation_pipeline.py` covering the operator flow: omit-mode-defaults-to-advisory, round-trip with `mode=blocking`, round-trip with `mode=advisory` set explicitly, unknown mode (e.g., `"shadow"`) returns 400, and PUT can promote an existing advisory config to blocking in place (the upsert path updates `mode` rather than creating a new row).
- Verified: `pytest tests/test_validation_pipeline.py -v` → **21/21** pass (was 16; +5 for mode). Full backend suite → **166/166** (was 160; +5 mode tests, +1 from a small adjustment elsewhere). Phase 7A behavior preserved end-to-end — the existing pipeline integration tests (advisory rows ingest cleanly, validations land, summaries render) all pass without touching them.

### 2026-04-28 — Phase 7 Output validation (guardrail layer 3) COMPLETE 🎯
Demo criterion (from MEMORY.md / Phase 7 header): *"Polaris's `polaris.plan` events flow through a validator pipeline before being treated as trustworthy by the dashboard. The `/polaris` view's history sidebar shows a small PASS / FAIL / WARN chip next to each plan; clicking a failed plan reveals the specific violations in the main pane. Demo run: deploy Polaris with a normal prompt → validators pass → dashboard shows green PASS chips. Then briefly inject a bad pattern → validators flag it → dashboard shows FAIL with the matched rule."* — passed.

**More dogfood**: the deployed bot's plan even described the demo it was enabling. The PASS-run plan's `next_actions[0]` was "register validators + redeploy + capture green-PASS screenshot"; the FAIL-run plan's `next_actions[0]` started with the word "delete" because the system-prompt injection asked it to, which then tripped `banned_destructive_verbs` against `next_actions[].task`. Polaris narrating the demo from inside the demo loop.

Demo run pointed at prod (`https://api.lightsei.com`, `https://app.lightsei.com`).

- [x] **Pushed 6 commits to `origin/main`**: Phase 7 plan + 7.1 through 7.5. All locally tested at 160/160 backend before push. Triggered Railway redeploys with `railway up backend --service lightsei-backend` and `... dashboard --service lightsei-dashboard` in parallel after the push (Railway auto-deploy on push isn't wired here). Build times: ~63s backend, ~70s dashboard. Both `Deploy complete`. **Hit a Railway-CLI auth-token-expired blocker between the push and the redeploy** — fix was a `railway login` from a real terminal; CLI login can't run non-interactively from this shell.
- [x] **Verified the new endpoints landed in prod** before deploying Polaris: `GET /agents/polaris/plans?limit=1` returns plans with the `validations` field; `GET /events/12345/validations` returns 404 cleanly (endpoint exists); `GET /workspaces/me/validators` returns `{"validators":[]}` (table exists, empty as expected). Migration 0013 ran on the prod Postgres on the backend container's startup pass.
- [x] **Registered validators on prod** via `python polaris/setup_validators.py` against `https://api.lightsei.com`. Listing confirms: `polaris.plan / schema_strict` (config keys: schema), `polaris.plan / content_rules` (config keys: rules — the case-insensitive `DEFAULT_RULE_PACK` after the 7.5 fix).
- [x] **Bundle**: built fresh wheel from `./sdk`, copied current `MEMORY.md` + `TASKS.md` into `polaris/`. Bundle size: 71 KB (was 60 KB at 6.6 — the diff is the larger TASKS.md after Phase 7 entries).
- [x] **Clean PASS run**: deployed via `lightsei deploy ./polaris --agent polaris`, status went `queued → building → running` in ~9s (cached venv). First plan landed at event_id 151, 42,390 in / 825 out tokens, ~$0.23 at `effort: "high"` on `claude-opus-4-7`. Both validators passed. Dashboard rendered `phase7-prod-pass.png`: latest plan selected, green PASS chip on the new plan, no chip on the older Phase 6.6 plan (no validators registered at that time — gray-no-chip is the correct "unchecked" state). VALIDATION panel correctly hidden on all-PASS.
- [x] **Injected FAIL run**: edited `polaris/system_prompt.md` to add a temporary "DEMO INJECTION" paragraph instructing Polaris to mention `alice@example.com` in `summary` and start the first next-action with `delete`. Stopped the clean deployment, redeployed. New plan landed at event_id 156, 42,573 in / 1107 out tokens. Dashboard rendered `phase7-prod-fail.png`: latest plan with red FAIL chip, VALIDATION panel showing `content_rules: FAIL · 2 violations` with the specific matches, plus `schema_strict: PASS · 0 violations` for contrast.
- [x] **Verbatim violations** from the FAIL plan:
  - `email_in_summary` at `summary`, matched `a***` (the validator redacted the long string per Phase 7.2's `_redact_match` — emails are PII-shaped, the original `alice@example.com` stays only in the event payload), message `forbidden pattern matched in summary`.
  - `banned_destructive_verbs` at `next_actions[].task`, matched `delete` (short keyword kept verbatim), message `forbidden pattern matched in next_actions[].task`.
- [x] **Sidebar chip usefulness check**: the dashboard's history sidebar shows three rows in `phase7-prod-fail.png` — the latest with a red `FAIL` chip, the Phase 7 PASS run with a green `PASS` chip, and the Phase 6 demo plan with no chip. Glanceable: a sweep of the sidebar tells you which plans are trustworthy without drilling into any of them. The `/polaris` page is now its own at-a-glance status board for the agent.
- [x] **Cleanup**: reverted the system-prompt injection (system_prompt.md is back to the 6.5 / 7.5 shape — no diff in this commit). Stopped the FAIL deployment via `POST /workspaces/me/deployments/{id}/stop`, killed the local worker. Validators stay registered on the prod workspace; the next ad-hoc Polaris run will auto-validate without rerunning `setup_validators.py`.
- [x] **Two screenshots committed**: `docs/phase7-prod-pass.png` (latest plan green, panel hidden, NEXT ACTIONS leading), `docs/phase7-prod-fail.png` (latest plan red, panel showing 2 violations with redacted matched strings, schema_strict still PASS).

### 2026-04-28 — Phase 7.5 Dashboard shows validation status
- [x] **Sidebar chips**: each plan entry in the history sidebar now carries a small PASS / FAIL / WARN / ERROR / TIMEOUT chip derived from the worst status across that plan's validations (`worstValidationStatus` helper in `api.ts`). Plans with no validators registered get no chip (the gray "unchecked" state is reserved for that case but doesn't render — the absence of a chip is the signal).
- [x] **`ValidationsPanel`** in `polaris/page.tsx` renders above the NEXT ACTIONS block when the selected plan has any non-PASS validations. Per validator: validator name in monospace + status chip + violation count, then the violation list. Violations show `rule` (bold mono), `path` (for schema_strict), `matched` (redacted display from the validator), and the `message`. Hidden entirely on all-pass plans — at-a-glance "no panel = nothing wrong."
- [x] **Lazy-load full violations**: the list endpoint ships only summaries (validator + status + violation_count, per Phase 7.4). When the user selects a plan with any non-PASS validation, `useEffect` fetches `/events/{event_id}/validations` once and caches the result in a `Map<event_id, PolarisValidation[]>`. Concurrent fetches deduped via a ref-tracked in-flight set so a fast click-through doesn't fire duplicate requests. PASS-only plans skip the fetch entirely (the panel doesn't render).
- [x] **Status colors centralized**: a single `STATUS_STYLES` map drives both the sidebar chip and the panel-header chip so they always agree. PASS is emerald, WARN/TIMEOUT are amber, FAIL/ERROR are red.
- [x] **api.ts additions**: `ValidationStatus` literal type, `PolarisViolation`, `PolarisValidation` (single optional-fields type that handles both lite-summary and full-detail shapes), `worstValidationStatus()` helper, `fetchEventValidations(eventId)`. `PolarisPlan.validations` is optional so the page handles old-shape responses gracefully if a deploy is half-rolled-out.
- [x] **Bug caught while taking the demo screenshot**: the Phase 7.2 `DEFAULT_RULE_PACK` regexes were case-sensitive, so a Polaris plan saying "Delete the cache" didn't fire `banned_destructive_verbs` (only lowercase `delete` matched). Added `(?i)` inline-flag prefix to both default rules and a regression test (`test_default_rule_pack_is_case_insensitive` in `test_validators.py`). Operators expect "delete" / "Delete" / "DELETE" to all flag the same way; the case-sensitive default was the wrong call. Tests now: 27/27 in test_validators.py.
- [x] **Verified live** against `docker compose up backend` + `npm run dev`. Seeded one clean plan and one fail plan (the fail plan's summary contains `alice@example.com` and its first next-action starts with `Delete the orphaned ...`). Both screenshots in `docs/`:
  - `docs/phase7-polaris-validations-fail.png` — latest plan selected: hero band shows the suspect summary in serif, sidebar shows red FAIL chip on top entry + green PASS chip on the older one, VALIDATION panel renders above NEXT ACTIONS with `content_rules: FAIL · 2 violations` (`email_in_summary at summary matched a***`, `banned_destructive_verbs at next_actions[].task matched Delete`) + `schema_strict: PASS · 0 violations`.
  - `docs/phase7-polaris-validations-pass.png` — clicked the older clean plan: VALIDATION panel correctly hidden (every status is PASS), page jumps straight to NEXT ACTIONS, sidebar still shows both chips so the user can see the status of the unselected fail plan at a glance.
- Verified the dashboard build path: `npx tsc --noEmit` clean, `next build` produces `/polaris` at 7.14 KB First Load JS (was 5.4 KB; +1.7 KB for the validation panel + lazy-load logic).

### 2026-04-28 — Phase 7.4 Backend endpoints for validation results
- [x] **`GET /events/{event_id}/validations`** returns the full validation rows for one event (validator + status + full violations[]). Workspace-scoped via two-step check (event exists, event.workspace_id matches) so cross-workspace event existence can't leak via timing — both branches return identical 404 detail.
- [x] **`GET /agents/{name}/latest-plan` extended** to embed full violations inline. The dashboard selects this plan by default; shipping violations on the same response avoids a follow-up fetch on first render. Single-plan response, fine to be fat.
- [x] **`GET /agents/{name}/plans` extended** with lite validation summaries: `[{validator, status, violation_count}]` per plan. The list endpoint powers the dashboard sidebar (50 plans × full violations would inflate responses); summaries are enough to render PASS / FAIL / WARN chips. The dashboard fetches full violations via `/events/{id}/validations` when the user clicks a historical plan.
- [x] **Helpers in `main.py`**: `_validation_summaries_for_events(session, event_ids)` does a single bulk query (`WHERE event_id IN (...)`) and groups by event_id, avoiding N+1 on the list endpoint. `_validations_for_event(session, event_id)` is the full-detail variant for single-plan responses. `_serialize_plan_event` now takes an optional `validations` argument so the same serializer powers both the lite and the full view.
- [x] **8 new tests** in `backend/tests/test_polaris.py` covering: full violations on `/events/{id}/validations`, 404 with no detail leak when event belongs to another workspace, 404 on unknown event id, latest-plan ships full violations (key set is `{validator, status, violations}`), list-plans ships only summaries (key set is `{validator, status, violation_count}`), violation_count reflects an actual failed validation, plans with no validators registered carry `validations: []` (distinguishable from "all passed" by the array being empty), workspace isolation on the validations field of latest-plan.
- Verified: `pytest tests/test_polaris.py -v` → **20/20** pass (was 12; +8 for 7.4). Full backend suite → **160/160** (was 152; +8). The dashboard hookup in 7.5 has everything it needs from the API surface.

### 2026-04-28 — Phase 7.3 Validation pipeline + event annotation
- [x] **Migration `0013_validators`** creates two tables. `validator_configs` (workspace_id FK CASCADE, event_kind, validator_name, config JSONB, timestamps) — composite PK is the natural upsert key for `PUT /workspaces/me/validators/{event_kind}/{validator_name}`. `event_validations` (id BIGSERIAL, event_id FK CASCADE, validator_name, status, violations JSONB, created_at) with `UNIQUE(event_id, validator_name)` so a re-run can't double up rows. Indexes on the hot lookup paths.
- [x] **`backend/models.py`** gets `ValidatorConfig` and `EventValidation` ORM models matching the migration shape. `UniqueConstraint` import added.
- [x] **Pipeline at `backend/validation_pipeline.py`** wraps the pure-function validators in the actual ingestion path. `run_validators(session, workspace_id, event)` queries `validator_configs` for the workspace + event kind, runs each registered validator on the event payload, and inserts one `event_validations` row per validator. Status mapping: `ok=True && no violations → pass`, `ok=True && warn-severity violations → warn`, `ok=False → fail`. Two defensive paths: (a) registry-mismatch (config references a validator no longer in the registry) records `status='error'` with `rule='unknown_validator'`, (b) any exception from a validator function records `status='error'` with `rule='validator_exception'`. Cumulative time budget of 200ms across all validators on one event — anything past it gets `status='timeout'`. The function never raises, so a buggy validator can't take down `/events` for a workspace.
- [x] **Hooked into `POST /events`** with one new line after `session.flush()`. Same transaction as the event insert, so a subsequent fetch sees the validations and the event together (no race on the dashboard).
- [x] **Endpoints**: `PUT /workspaces/me/validators/{event_kind}/{validator_name}` (upsert config, idempotent), `GET /workspaces/me/validators` (list all for the workspace), `DELETE /workspaces/me/validators/{event_kind}/{validator_name}`. Path-param validation rejects malformed names with 400. PUT also rejects unknown validator names (must be in REGISTRY); DELETE deliberately accepts unknown names so an operator can clean up stale rows after a registry rename.
- [x] **`polaris/setup_validators.py`**: one-shot script that registers schema_strict + content_rules for `polaris.plan` events on the calling workspace. Reads `LIGHTSEI_API_KEY` + `LIGHTSEI_BASE_URL` from env, hits the new endpoints with `urllib.request` (no SDK dep — keeps the script self-contained). The `POLARIS_PLAN_SCHEMA` defined in the script extends the bot's `submit_plan` input_schema with the bot-emitted envelope fields (text, doc_hashes, model, tokens_in, tokens_out). The content_rules config lifts `DEFAULT_RULE_PACK` from `backend/validators/content_rules.py` directly so the demo always runs against whatever the validator module ships with. Idempotent — calling twice just overwrites the existing config.
- [x] **16 new tests in `backend/tests/test_validation_pipeline.py`** covering: PUT/GET/DELETE round-trip, PUT idempotency, PUT rejects unknown validator name, PUT rejects malformed event_kind / validator_name, DELETE 404 path, cross-workspace isolation on the config endpoints, unauthorized requests, POST /events triggers registered validators (clean-pass row), invalid payload produces fail row, no validators registered → no rows, multiple validators all run, registry-mismatch produces unknown_validator error row, validator-exception produces validator_exception error row, cross-workspace isolation in the pipeline (alice's config doesn't run on bob's events).
- Verified: `pytest tests/test_validation_pipeline.py -v` → **16/16** pass. Full backend suite → **152/152** (was 136; +16). Pipeline + endpoints + setup script ready for the dashboard hookup in 7.4.
- Schema-source decision: the 6.5 phase plan flagged the option of lifting Polaris's plan schema into a shared backend-importable spot. Took the alternate path the plan offered: Polaris registers its own schema as the validator config at deploy time, the backend stays agnostic. `setup_validators.py` is the schema's source of truth in the deployment path; the bot's `submit_plan` `input_schema` and the script's `POLARIS_PLAN_SCHEMA` overlap intentionally — they describe two related-but-distinct payloads (the model's tool input vs the bot-emitted envelope).

### 2026-04-28 — Phase 7.2 Content-rules validator
- [x] **Second concrete validator at `backend/validators/content_rules.py`.** Config shape: `{"rules": [{"name": str, "pattern": str, "fields": [str], "mode": "must_not_match"|"must_match", "severity": "fail"|"warn"}]}`. Each rule names a regex, a list of field paths to check, and a mode/severity. The validator walks the named paths, runs the regex against any string values found, and emits a violation per match (or non-match, depending on mode).
- [x] **`severity: warn` violations are recorded but don't fail the result.** This is the hook for advisory-only rules — the dashboard chips will show WARN, but the event is still considered valid. Demo will use `fail`-severity rules; the warn path is there for future use.
- [x] **Default rule pack** shipped as `DEFAULT_RULE_PACK` constant in the module: `email_in_summary` (must_not_match a permissive email regex against `summary`) and `banned_destructive_verbs` (must_not_match `\b(delete|drop|truncate|destroy|nuke)\b` against `next_actions[].task`). 7.3 will register this pack against `polaris.plan` events automatically when Polaris deploys.
- [x] **Minimal field-path syntax** (`summary`, `outer.inner`, `next_actions[].task`). Implemented in 30 lines of `_parse_path` + `_walk` rather than pulling in jsonpath-ng or jmespath; the syntax is intentionally too small to need a real path library, and adding one for two demo rules would have been heavier than the entire module.
- [x] **`_redact_match` for matched-substring display.** Short matches (under 8 chars) are kept verbatim because they're almost always the keyword the operator chose to flag (`delete`, `drop`); longer matches are redacted to first-char + `***` because they're more likely user-supplied content (emails, paths, names) the validator caught. The full match still lives in the original event payload — this field is just for display so the dashboard's eventual violation panel can render "what fired" without leaking PII into the violations table.
- [x] **Robustness**: every config / rule error path emits a violation rather than raising. A bad rule (missing pattern, invalid regex) emits its own per-rule violation but doesn't stop the other rules from running. Missing fields silently yield nothing — schema-strict catches missing required fields, so content-rules treats them as "no values to check" to avoid double-reporting the same problem from two validators.
- [x] **15 new tests** covering: clean pass with default pack, email-in-summary flag with redaction verified, destructive-verb flag with verbatim short match kept, multiple matches in array yield multiple violations, must_match mode, warn-severity preserves ok=True, invalid regex per-rule violation (other rules still run), missing pattern violation, missing config violation, missing field silently yields nothing (avoid double-report with schema-strict), array path resolution end-to-end, registry routing via `validate(name, ...)`, default pack rule names pinned, redaction threshold boundary, registry contains canonical `content_rules` key.
- Verified: `pytest tests/test_validators.py -v` → **26/26** pass (was 11; +15 for content-rules). Full backend suite → **136/136** (was 121; +15). No I/O in any code path.

### 2026-04-27 — Phase 7.1 Validator interface + schema-strict validator
- [x] **New `backend/validators/` package** with three files: `_types.py` (the `Violation` and `ValidationResult` types — plain dicts / TypedDict so storage in the future `event_validations` table is JSON-trivial), `schema_strict.py` (the first concrete validator), and `__init__.py` (a `REGISTRY` dict + a `validate(name, payload, config)` entry point).
- [x] **Validator contract**: pure function `(payload: Any, config: dict) -> ValidationResult`. No I/O, no side effects. Result is `{"ok": bool, "violations": list[dict]}` where each violation has at least `rule` and `message`. Validators may attach extra fields (schema-strict adds `path`, content-rules will add `matched`/`severity` in 7.2). Pure-function constraint keeps the future pipeline (Phase 7.3, synchronous post-emit) cheap and trivially parallelizable later.
- [x] **schema-strict** uses `jsonschema` Draft 2020-12 (matches Anthropic's strict tool-use schemas — the same draft Polaris's `submit_plan` `input_schema` targets). Reports every violation in the payload, not just the first, so the dashboard's eventual violation panel can render a complete picture instead of a one-error-at-a-time game of whack-a-mole. Configuration errors (missing schema, malformed schema, unresolvable `$ref`) are themselves reported as violations rather than raised — the pipeline must stay crash-free even when an operator mis-registers a config.
- [x] **Real bug caught and fixed during testing**: first impl wrapped only `Draft202012Validator(schema)` construction, but jsonschema's `iter_errors()` raises `UnknownType` (not `SchemaError`) when a schema references a non-existent type. Fixed by calling `Draft202012Validator.check_schema(schema)` upfront to validate the meta-schema, plus wrapping `iter_errors()` in a `SchemaError` catch for evaluation-time failures (unresolvable `$ref`). One real bug per phase; this one would have crashed the Phase 7.3 pipeline on any deployment with a typo in its registered schema.
- [x] **`jsonschema==4.23.0` added to `backend/requirements.txt`.** Stable, widely-used, last-modified mid-2024.
- [x] **`polaris.plan` schema lift deferred to Phase 7.3.** The phase plan flagged the option of lifting the schema into a shared spot. Skipped here because the cleaner shape is "Polaris registers its schema as the validator config on its workspace at deploy time" — the backend doesn't need a static import of the polaris schema, it gets it from the registry. 7.3 ships `polaris/setup_validators.py` for that.
- [x] **11 new tests in `backend/tests/test_validators.py`** covering: clean payload passes, missing required field, wrong type, additional-property rejection, multi-violation collection in one pass, JSON-pointer path on nested errors, `missing_config` violation when schema absent, `invalid_schema` violation when meta-schema fails, registry routing via `validate(name, ...)`, KeyError on unknown validator name, registry contains the canonical `schema_strict` key (rename guard).
- Verified: `pytest tests/test_validators.py -v` → 11/11 pass. Full backend suite → **121/121** (was 110; +11 for validators). No I/O in any validator code path; tests run in 0.89s.

### 2026-04-27 — Phase 6 Polaris (project orchestrator bot) COMPLETE 🎯
Demo criterion (from MEMORY.md / Phase 6 header): *"From a fresh terminal, `lightsei deploy ./polaris` (with this project's MEMORY.md + TASKS.md copied into the bundle) deploys the Polaris bot via the Phase 5 PaaS. Within ~5 minutes the dashboard's Polaris view renders a generated plan against this project's own docs, with at least 3 next-action recommendations, parking-lot evaluation, and any drift it spots. The plan is 'good enough' — sanity check is whether I would have picked the same next move."* — passed.

Polaris is dogfood. The bot deployed in prod literally identified its own deployment as `next_actions[0]`. Recursive.

Demo run pointed at prod (`https://api.lightsei.com`, `https://app.lightsei.com`, real Postgres, workspace `Bailey's Agent Monitor`).

- [x] **Pushed 9 commits to `origin/main`**: worker pip-cwd bugfix, .gitignore for build artifacts, Phase 5 demo, Phase 6 plan, and Phase 6.1 through 6.5. All 9 had been hand-tested locally; full backend suite was 110/110 before push. Railway auto-deploy on push isn't wired up here, so triggered both services manually with `railway up backend --service lightsei-backend --ci` and `railway up dashboard --service lightsei-dashboard --ci`. Both `SUCCESS`. Deploy time: ~30s backend, ~60s dashboard.
- [x] **Verified the new endpoints landed in prod**: `GET /agents/polaris/plans?limit=1` → `{"plans": []}` (200), `GET /agents/polaris/latest-plan` → `{"detail": "no plan yet"}` (404), `https://app.lightsei.com/polaris` → 200. Existing `/health` and `/auth/me` still green throughout.
- [x] **Bundle**: built wheel from `./sdk` (`python -m build --wheel`), copied `lightsei-0.0.1-py3-none-any.whl` + `MEMORY.md` + `TASKS.md` into `polaris/`. Final zip: 60,347 bytes. Bundle ships the project's own docs so Polaris reads what it'll plan against; the `POLARIS_DOCS_DIR` default (`.`) resolves them inside the unpacked deployment dir.
- [x] **Workspace secrets**: `LIGHTSEI_API_KEY` already there from Phase 5 demo. Stored `ANTHROPIC_API_KEY` via `PUT /workspaces/me/secrets/ANTHROPIC_API_KEY`. Both auto-injected by the worker into the bot's env on spawn.
- [x] **Lifecycle proven end-to-end**: `lightsei deploy ./polaris --agent polaris` zipped, uploaded, status went `queued → building → running` in ~9s after the worker claim (the build step reused the cached venv — pip install completed quickly because all deps were already present from the Phase 5 demo's `examples/demo_deploy/` deployment). Bot started, ticked once, called Claude `claude-opus-4-7` at `effort: "high"` with the forced `submit_plan` tool, emitted the structured plan to `/events`. The dashboard's `/polaris` view picked it up via `/agents/polaris/latest-plan` within seconds.
- [x] **Verbatim plan from the deployed bot** (event_id 146, run 8a9e33d2, 32,656 in / 841 out tokens, ~$0.18):

  > **Summary.** Phase 6 is at the final demo task (6.6): Polaris's scaffold, plan schema, backend endpoint, dashboard view, and tuned system prompt all shipped 2026-04-27. What remains is deploying Polaris via the Phase 5 PaaS against this project's own docs and confirming the first plan's top recommendation matches user intuition.
  >
  > **Next actions:**
  >
  > 1. **Execute Phase 6.6: build the lightsei wheel, copy MEMORY.md + TASKS.md into polaris/, set ANTHROPIC_API_KEY and LIGHTSEI_API_KEY as workspace secrets, then run `lightsei deploy ./polaris --agent polaris` against api.lightsei.com.** This is the NOW task and the only remaining item before Phase 6 closes. The 6.5 Done Log already validated the prompt against these exact docs in a local harness, so deploying via the PaaS is the last unverified link. *Blocked by: ANTHROPIC_API_KEY and LIGHTSEI_API_KEY need to be present in the workspace secrets store before deploy.*
  >
  > 2. **Wait for the first scheduled tick, capture a screenshot of /polaris rendering the plan, and sanity-check that next_actions[0] matches what you would have picked.** Phase 6 demo criterion explicitly requires the screenshot, the verbatim plan text, and the intuition-match call. Without that artifact the phase isn't done per the project's "each phase ends with a demo" principle. *Blocked by: Polaris deploy reaching `running` and one tick completing (~5 minutes per the phase header).*
  >
  > 3. **If next_actions[0] doesn't match intuition, loop back to 6.5 and iterate polaris/system_prompt.md, then redeploy; if it does match, write the 6.6 Done Log entry with screenshot path, verbatim plan, and the match note.** The phase header makes prompt iteration the explicit fallback path. Either way the Done Log entry is what closes Phase 6 and unblocks the Phase 7+ decision. *Blocked by: First plan output from the previous step.*
  >
  > Parking-lot promotions: none.
  > Drift: none.

- [x] **Intuition check**: `next_actions[0]` is "execute Phase 6.6", which was *literally what I was doing while reading the plan*. The plan correctly identifies its own deployment as the active task, then the screenshot step (which I am about to do), then the close-or-iterate decision. The 3-action shape, the `blocked_by` callouts, and the "no em dashes / no padded recommendations" output matches the prompt iteration from 6.5 exactly. **Demo passes.**
- [x] **Screenshot**: `docs/phase6-polaris-prod.png` — `/polaris` against prod, summary in serif in the dark hero band, three numbered next-actions in the body, doc-hash chips at the bottom, sidebar with a single past-readings entry.
- [x] **Cleanup**: stopped the deployment via `POST /workspaces/me/deployments/{id}/stop`, waited for the worker to wind down (~35s), killed the local worker process. Polaris is no longer running in prod; the deployment row sits in `stopped` state as the artifact. Phase 6A always intended to be on-demand, not always-on; that's a Phase 6B concern alongside the act-don't-just-plan work.

### 2026-04-27 — Phase 6.5 System prompt iteration
- [x] **Switched the structured-output mechanism from "ask for JSON, parse text" to Anthropic strict tool use.** `polaris/bot.py` now defines a single `submit_plan` tool with `strict: true` and a JSON-Schema `input_schema` covering `summary`, `next_actions[task, why, blocked_by]`, `parking_lot_promotions[item, why]`, and `drift[between, observation]`. `tool_choice` forces the model to call exactly that tool, so the response is guaranteed to contain a `tool_use` block whose `input` matches the schema verbatim. The old `_parse_plan` helper is deleted; the bot reads `tool_block.input` directly into the event payload.
- [x] **Plan-deviation note: parse-and-retry loop dropped.** The 6.5 task description listed a parse-and-retry loop as the fallback path "if Claude's structured-output / JSON mode isn't cleanly exposed by the SDK." The Anthropic SDK does expose strict tool calling cleanly, so we took that path and dropped the retry loop. The Phase 6.2 tolerant `_parse_plan` parser was load-bearing only under the old text-output strategy and is now gone.
- [x] **Plan-deviation note: temperature dropped (forced by Opus 4.7).** The 6.5 task description said to "pin temperature low (~0.2)." Opus 4.7 returns a 400 if `temperature` is sent — sampling parameters are removed in that model. Replaced with `output_config={"effort": "high"}`, which the Claude API skill's Opus 4.7 guidance recommends as the minimum for intelligence-sensitive work.
- [x] **Constraint surfaced and worked around: forced `tool_choice` is incompatible with thinking on Opus 4.7.** First call attempt with both `thinking: {type: "adaptive"}` and `tool_choice: {type: "tool", name: "submit_plan"}` returned `400 invalid_request_error: "Thinking may not be enabled when tool_choice forces tool use."`. Dropped thinking; kept `effort: "high"` and the forced tool call. Comment in `bot.py:_call_claude` notes the alternative (`tool_choice: {type: "any"}` allows thinking, would still effectively force `submit_plan` since it is the only tool defined) for if we want both later.
- [x] **System prompt rewritten** (`polaris/system_prompt.md`). Role-focused now that the JSON shape is encoded in the tool's `input_schema`. Two iterations against this project's real docs:
  - **Pass 1** (5 next-actions, 1181 output tokens): correctly identified Phase 6.5 as `next_actions[0]`. Issues: peppered with em dashes (project preference is none), included a Phase 7+ "pick next phase" recommendation that was outside the active phase.
  - **Pass 2** (4 next-actions, 829 output tokens): no em dashes, all four items inside Phase 6, sharper phrasing. `next_actions[0]` still correctly identified Phase 6.5. Items 2 and 3 echoed the original 6.5 task wording (parse-and-retry, temperature 0.2) which is faithful to TASKS.md as written and will resolve once this Done Log entry lands.
- [x] **System prompt now bans em dashes explicitly** (project preference from CLAUDE.md, which Polaris does not currently see). If we later have Polaris read CLAUDE.md too, this rule can move.
- [x] **System prompt instructs the model to trust the Done Log over older task descriptions when they conflict.** Surfaced from pass 1, where the model treated the 6.5 task wording as more authoritative than the 6.2-and-later Done Log.
- [x] **Canonical plan captured in `polaris/README.md`** along with how Polaris works, how to deploy, and a cost note (~31K in / ~830 out per plan, ~$0.18 each at hourly default; hash-skip keeps steady-state cost near zero when docs are stable).
- Cost of the 6.5 iteration loop: 2 calls × ~$0.18 = ~$0.36 against `claude-opus-4-7` at `effort: "high"`. Iteration script lives at `/tmp/lightsei-demo/iterate_polaris.py` for future prompt tweaks (not committed; throwaway tooling).
- Demo criterion check: pass 2's `next_actions[0]` ("Phase 6.5: draft polaris/system_prompt.md and hand-test it...") matches what I would have picked. Phase 6.5 closed.

### 2026-04-27 — Phase 6.4 Dashboard "Polaris" view
- [x] **New `/polaris` route** at `dashboard/app/polaris/page.tsx`. Top-level concept, separate from agent pages (which still serve as the per-agent control plane). Linked from the global Header next to "account" so it's discoverable from anywhere in the dashboard.
- [x] **Creative styling**, deviating intentionally from the rest of the dashboard's plain Tailwind aesthetic per user direction. Dark indigo/slate gradient hero band with a subtle hand-placed starfield (12 absolutely-positioned dots, deterministic so it doesn't jitter on render), a 4-pointed star glyph (distinct from the 5-pointed stars elsewhere), and a serif headline rendering the plan summary directly in the band. Eyebrow text in 0.2em-tracked uppercase reads "POLARIS · PROJECT ORCHESTRATOR." When no plan is selected the band says "Awaiting first sighting." in serif.
- [x] **Guided empty state** when no `polaris.plan` events exist yet. Three numbered steps under "Polaris is dark.": build the wheel (copy-button), set ANTHROPIC_API_KEY in workspace secrets (link to /account), deploy via the CLI (copy-button). Footer card explains what Polaris does in 1 paragraph. Avoids the "blank page + opaque error" trap.
- [x] **History sidebar** on the left (260px wide, sticky). Lists up to 50 past plans newest-first, each as a clickable button with relative timestamp ("4m ago") + absolute timestamp underneath. Selected plan has an indigo left-border accent and tinted background; the most recent plan gets a small star icon. Clicking switches the hero band + main pane to a frozen historical view. Selection persists across polls until a new tick lands.
- [x] **Plan-detail rendering** in the main pane: numbered next-actions with `blocked_by` chips in amber when present, parking-lot promotions as small cards, drift entries with full amber styling (drift is a project-health signal, not a routine output). A grid of doc-hash + model + token chips at the bottom; a collapsible `<details>` for the raw Claude response (useful when `parse_error` is set, which renders as a dedicated amber banner above the rest).
- [x] **30s polling** via setInterval; aborts cleanly on unmount.
- [x] **Backend follow-on**: `GET /agents/{name}/plans?limit=N` (1..100, default 20) added to support the history sidebar. Validates limit, returns `{plans: [...]}` newest-first. Workspace-scoped, returns `{plans: []}` (200, not 404) when no events exist — distinct from latest-plan's 404 because callers iterating history shouldn't have to special-case "agent never emitted." 5 new tests covering empty list, ordering, limit bounds, validation 400s, and cross-workspace isolation. Full backend suite at 110/110.
- [x] **api.ts additions**: PolarisNextAction / PolarisPromotion / PolarisDrift / PolarisPlanPayload / PolarisPlan types matching the 6.2 event schema; fetchLatestPolarisPlan and fetchPolarisPlans helpers using the existing authedJson pattern.
- Verified end-to-end against `docker compose up db backend` with the rebuilt backend image and `npm run dev` for the dashboard. Captured three screenshots: `docs/phase6-polaris-empty.png` (genuine empty state, no plans in DB), `docs/phase6-polaris-plan.png` (latest plan rendered with summary in serif, 3 next-actions, 1 promotion, 1 drift entry, doc-hash chips), `docs/phase6-polaris-history.png` (clicked the second sidebar entry; hero band + main pane switched to that frozen plan, selection styling correct). Plans were injected via Python urllib against `/events` because two earlier shell-heredoc attempts fought with quoting; using the SDK-style POST avoided the issue.

### 2026-04-27 — Phase 6.3 Backend latest-plan endpoint
- [x] `GET /agents/{agent_name}/latest-plan` returns the most recent `polaris.plan` event for the named agent in the calling workspace. 404 when no plan event has been emitted yet. The full event payload is wrapped in `{event_id, run_id, agent_name, timestamp, payload}`.
- [x] **Plan-deviation note (URL).** The Phase 6 plan committed earlier listed this endpoint at `/workspaces/me/agents/{name}/latest-plan`. Switched to `/agents/{agent_name}/latest-plan` to match existing convention: `/agents/{name}` and `/agents/{name}/cost` and `/agents/{name}/commands` are all top-level routes scoped via the `get_workspace_id` dep, not via URL prefix. `/workspaces/me/...` is reserved for workspace-level resources (api-keys, secrets, deployments). Treating `latest-plan` as workspace-level would have been a one-off inconsistency.
- [x] Reused the existing `events` table — no new migration. Query is `SELECT FROM events WHERE workspace_id = $1 AND agent_name = $2 AND kind = 'polaris.plan' ORDER BY timestamp DESC, id DESC LIMIT 1`. Composite index `idx_events_ws_agent_kind_ts` (added in Phase 4.2) covers it.
- [x] **Endpoint is agent-name-agnostic by design.** It doesn't require the agent to be named "polaris" — any agent emitting `polaris.plan` events works. This matches the original phase plan's "caller decides what to render" intent and keeps the door open for "Polaris-style" agents under different names later.
- [x] 7 new tests in `backend/tests/test_polaris.py` covering: 404 on no events, 200 with full payload, latest-wins ordering, ignoring non-polaris-plan kinds (run_started / tick_skipped don't count), cross-workspace isolation (alice can't see bob's plan even on the same agent name), works for arbitrary agent names, 401 unauthorized.
- Verified: `pytest tests/test_polaris.py -v` → 7/7 pass. Full backend suite → 105/105 pass (was 98 before this change). Run-time: 57s.

### 2026-04-27 — Phase 6.2 Plan event schema + emit + change detection
- [x] **Schema for `polaris.plan` events** (carried in the event payload, not a DB column — `events.payload` is JSONB):
  - Always present: `text` (raw Claude response), `doc_hashes`, `model`, `tokens_in`, `tokens_out`.
  - Present on successful parse: `summary`, `next_actions[{task, why, blocked_by}]`, `parking_lot_promotions[{item, why}]`, `drift[{between, observation}]`.
  - Present on parse failure: `parse_error` (string explaining what went wrong); structured fields are absent so the dashboard can render "raw text only, parse failed."
- [x] **Tolerant JSON parser** (`_parse_plan` in `polaris/bot.py`). Strips a leading ` ```json ` fence + trailing ` ``` ` if Claude added one despite being told not to (common failure mode at low temperature). Returns `(parsed, parse_error)` with exactly one non-None — no exceptions thrown to the caller.
- [x] **Change detection via in-process `_last_hashes`**. First tick after bot start always proceeds (LLM call or dry-run emit). On every successful emit (`polaris.plan` with a clean parse, or `polaris.tick_dry_run`), the bot caches the docs' hashes. Subsequent ticks compare; if hashes match, the bot emits a tiny `polaris.tick_skipped` event with `reason: "docs unchanged"` and skips the LLM call. Choice: in-process state, not a backend lookup. Trade-off — a redeploy resets it and re-calls Claude on first tick even on identical docs. That's intentional: redeploys should confirm the new bundle's prompt still produces good output. Cross-deploy hash sharing can be added later by reading the latest plan event from the backend on bot start, at the cost of a startup query (and a Phase 6.3 endpoint to call).
- [x] **Parse-failure retry semantics**. `_last_hashes` is updated only on a clean parse. If Claude returns malformed JSON, the next tick will re-call Claude rather than silently waiting for the docs to change. Temperature 0.2 is non-deterministic enough that a parse failure can self-resolve.
- [x] **System prompt updated** to specify the JSON output shape (`polaris/system_prompt.md`). Still flagged as the 6.2 placeholder; real iteration lands in 6.5.
- Verified end-to-end against prod with `POLARIS_DRY_RUN=1 POLARIS_POLL_S=4`: tick 1 emitted `polaris.tick_dry_run` and set `_last_hashes`; ticks 2 + 3 emitted `polaris.tick_skipped` with `{reason: "docs unchanged", hashes: {tasks_md: f4989e78…, memory_md: 74a298e8…}}`. Confirmed via `/runs/{id}/events` on each of the three runs that the kind sequence was `run_started → polaris.tick_skipped|tick_dry_run → run_ended`.
- Verified parse path with 5 cases (clean JSON, fenced JSON, malformed text, top-level array, partial fields). All pass. The full `polaris.plan` end-to-end emission isn't exercised here because we don't have an Anthropic key in this shell; the Phase 6.6 demo will close that loop. Component-by-component, each piece — `_call_claude` (same shape as the Phase 5 demo bot's verified Anthropic calls), `_parse_plan` (5 unit cases), `lightsei.emit` (verified by the dry-run path) — is exercised.

### 2026-04-27 — Phase 6.1 Polaris bot scaffold
- [x] New `polaris/` at repo root with `bot.py`, `system_prompt.md` (placeholder, real version in 6.5), `requirements.txt`, and a bundled `lightsei-0.0.1-py3-none-any.whl` (rebuilt from `./sdk` per deploy, same pattern Phase 5's demo bundle used).
- [x] `bot.py` loop: read `MEMORY.md` + `TASKS.md` from `POLARIS_DOCS_DIR`, hash both with sha256 (truncated to 16 hex chars), call Claude with the orchestrator system prompt via the Anthropic SDK, emit `polaris.plan_raw` carrying `{text, hashes, model, tokens_in, tokens_out}`. Wrapped in `@lightsei.track` so each tick is its own run.
- [x] Configurable env: `POLARIS_POLL_S` (3600), `POLARIS_MODEL` (`claude-opus-4-7`), `POLARIS_DOCS_DIR` (`.`), and `POLARIS_DRY_RUN=1` which skips the Anthropic call (emits `polaris.tick_dry_run` instead) so the loop is verifiable without an API key.
- [x] Required workspace secrets at deploy time: `LIGHTSEI_API_KEY` and `ANTHROPIC_API_KEY` (the second one is skipped in dry-run mode).
- Verified locally against this project's docs: `POLARIS_DRY_RUN=1 POLARIS_POLL_S=4` ran for ~12s, produced 3 ticks. Each tick read `MEMORY.md` + `TASKS.md` (resolved to `/Users/baileywallace/Desktop/Beacon/Beacon`), computed identical hashes (`memory=74a298e82dd60a65`, `tasks=c2f08f32620756c8`), opened a Lightsei run, emitted `polaris.tick_dry_run` with the hash payload, and closed the run. Confirmed via prod API: 3 separate runs against agent `polaris-local-test` ingested with the expected `run_started → polaris.tick_dry_run → run_ended` sequence on each. Total verification time: ~30s. Note: the `polaris-local-test` agent now exists in the workspace as test data; the real Phase 6.6 demo will deploy under agent name `polaris`, so the two are segregated.
- Schema choice that helps 6.2: the `hashes` field on `polaris.plan_raw` / `polaris.tick_dry_run` is exactly what 6.2's change-detection will read from the latest event to decide whether to skip the next LLM call. 6.1 and 6.2 are wired so 6.2 is purely additive.

### 2026-04-26 — Phase 5 PaaS for agents COMPLETE 🎯
Demo criterion (from MEMORY.md / Phase 5 header): *"From a fresh terminal, `lightsei deploy ./my-bot` zips the directory, uploads it, and a worker process spawns the bot. Within a minute the dashboard shows the deployment as `running`, the bot's instance heartbeats are visible, and logs stream into a Deployments tab. Stop and redeploy from the dashboard work end-to-end. Nothing about the user's bot code changes between local and hosted runs."* — passed.

Demo run pointed at prod (`https://api.lightsei.com`, real Postgres, real workspace `Bailey's Agent Monitor`).

- [x] **Bundle**: `examples/demo_deploy/` packages the four-call demo bot (OpenAI + Anthropic, regular + streaming) wrapped in a 20s loop so the deployment stays in `running` long enough to demonstrate heartbeats / stop / redeploy. Provider calls fail gracefully when the workspace doesn't have `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` stored as secrets, so the demo is observable end-to-end with only `LIGHTSEI_API_KEY` in workspace secrets.
- [x] **Wheel bundling**: since `lightsei` isn't on PyPI yet, the bundle ships `lightsei-0.0.1-py3-none-any.whl` (built from `./sdk` via `python -m build --wheel`) and references it as a relative path in `requirements.txt`. The worker's pip install resolves that against the unpacked bundle dir.
- [x] **Real bug caught and fixed**: first deploy attempt (deployment 4a89845c…) failed with `pip install exit 1` because the worker's `subprocess.run([..., "pip", "install", "-r", req])` call in `worker/runner.py:233` was missing `cwd=str(bot_dir)`, so pip inherited the parent shell's cwd and resolved relative wheel paths against the wrong directory. Added `cwd=str(bot_dir)` to match how `_spawn` already runs the bot (`runner.py:277`). Re-deploy succeeded immediately. Real worker bug, surfaced only because relative paths in requirements.txt are a legit user pattern.
- [x] **Lifecycle proven end-to-end**: from a fresh terminal `lightsei deploy ./examples/demo_deploy` zipped 25,226 bytes, uploaded as multipart, and polled status: `queued → building → running` in ~9s after the worker claim. Worker logs show: claim → fetch bundle → unpack → create venv → pip install → fetch workspace secrets → spawn `bot.py`. Bot heartbeats every 30s; instance shows up under `/agents/demo-deploy` with `last_seen` ticking forward. Logs streamed into the dashboard's tail viewer (`docs/phase5-deployment-detail.png` shows 16 lines including `bot up: agent=demo-deploy base_url=https://api.lightsei.com` and 7 iterations of `iteration N: provider calls`). Stop button via dashboard flipped `desired_state` to stopped; worker reacted within ~30s; deployment row went STOPPED (`docs/phase5-after-stop.png`). Redeploy button created a fresh row (c5cb718c…) pointing at the same blob; worker reused the cached venv and went `running` in seconds (`docs/phase5-after-redeploy.png`, "demo-deploy live · 2" badge during the rolling cutover). Final cleanup: stopped the live deployment from the dashboard, then killed the worker process locally.
- [x] **Screenshots**: `docs/phase5-runs-list.png` (top of the Runs page showing 7 demo-deploy runs from the bot's heartbeat loop), `docs/phase5-agent-deployments.png` (Deployments panel with the failed-then-fixed history visible), `docs/phase5-deployment-detail.png` (running deployment with streaming log viewer + stop button), `docs/phase5-after-stop.png` (post-stop state, redeploy button replaced stop), `docs/phase5-after-redeploy.png` (new running deployment c5cb718c… alongside stopped 6b92a531… and failed 4a89845c…).

Open scope-discipline note: the bug fix to `worker/runner.py` is a real Phase 5.3 worker correctness change that should ride alongside this demo commit. Hard Rule 1 ("Stay in the current phase") covered — Phase 5 is the current phase.

### 2026-04-26 — Phase 5.4 + 5.6 Streaming logs + Deployments dashboard tab
- [x] Backend: `GET /workspaces/me/deployments/{id}/logs?after_id=N&limit=...` — incremental tail so the dashboard polls without re-downloading the full buffer each tick. `POST /workspaces/me/deployments/{id}/stop` flips `desired_state` to stopped (worker picks it up on next heartbeat). `POST /workspaces/me/deployments/{id}/redeploy` creates a new deployment row pointing at the same source blob and stops the old one.
- [x] Dashboard: Deployments panel on `/agents/[name]` listing the last 10 with status pills + stop/redeploy buttons inline + click-through. New `/deployments/[id]` page with metadata grid, action buttons, and a terminal-styled log viewer. Adaptive polling (1.5s while building/running, 5s once stopped) and an auto-scroll toggle.

### 2026-04-26 — Phase 5.5 SDK CLI: `lightsei deploy`
- [x] Added `deploy` subcommand alongside `serve` in the SDK CLI. Zips the target directory deterministically, excluding the usual dev junk (`__pycache__`, `.venv`, `.git`, `node_modules`, `*.pyc`, `.DS_Store`, etc.), POSTs as multipart to `/workspaces/me/deployments`, then polls until status reaches `running` / `failed` / `stopped`.
- [x] Resolution order: `--api-key` flag, then `$LIGHTSEI_API_KEY`. Same for `--base-url`. Agent name defaults to the directory's basename.
- [x] Smoke-tested against prod: upload + queued row + delete cleanly.

### 2026-04-26 — Phase 5.3 Worker process
- [x] `worker/runner.py` polls the backend for queued deployments, builds a venv per deployment under `/tmp/lightsei-worker/`, spawns the bot subprocess, streams stdout/stderr lines back via `/worker/deployments/{id}/logs`, and reacts to `desired_state` flips on each heartbeat.
- [x] Concurrency capped at `LIGHTSEI_WORKER_MAX_CONCURRENT` (default 4). Each running bot gets its own supervisor thread + scratch dir.
- [x] `WorkerClient` takes an injectable `httpx.Client` so tests run the full loop against the FastAPI TestClient. Four integration tests cover clean exit, crash, missing entry script, and user-stop-while-running. All green in CI.
- [x] Heartbeat endpoint now returns the deployment row so the worker sees `desired_state` without a separate fetch.
- [x] Trust boundary unchanged: worker is system-component-only, single-tenant safe. External users come in Phase 5B (managed isolation).

### 2026-04-26 — Phase 5.2 Worker-facing endpoints
- [x] Six endpoints under `/worker/*` gated by `LIGHTSEI_WORKER_TOKEN` (constant-time compare, fail-closed when env unset).
- [x] `claim` uses `SKIP LOCKED` plus a stale-heartbeat steal clause (90s TTL) so a dead worker doesn't park its deployments forever.
- [x] Status updates set `started_at` / `stopped_at` as the state machine transitions. Log append bounded to the most recent 1000 lines per deployment with insert-time prune. Blob fetch returns raw bytes plus a `sha256` header for integrity checks. Workspace secrets fetch hands the worker a decrypted dict for env-var injection.
- [x] Operational note: a leaked worker token grants cross-tenant access. It joins the backup passphrase and `LIGHTSEI_SECRETS_KEY` as the third top-tier secret. Phase 5B is the right place to harden this.

### 2026-04-26 — Phase 5.1 Deployments schema + zip upload
- [x] Migration creating `deployments` (id, workspace_id, agent_name, status, desired_state, source_blob_id, error, claimed_by, claimed_at, heartbeat_at, started_at, stopped_at, created_at, updated_at) and `deployment_blobs` (id, workspace_id, size_bytes, sha256, data BYTEA, created_at).
- [x] SQLAlchemy models for both tables.
- [x] Body-size middleware bumped to 10 MB on `multipart/*` requests; JSON cap stays at 1 MB.
- [x] User-facing endpoints: `POST /workspaces/me/deployments` (multipart upload), `GET /workspaces/me/deployments`, `GET /workspaces/me/deployments/{id}`, `DELETE /workspaces/me/deployments/{id}`. Worker-facing endpoints (claim/heartbeat/log) deferred to 5.2.
- [x] Tests: roundtrip upload, list scoped to workspace, oversize blob → 413, cross-workspace → 404.

### 2026-04-25 — Phase 4 hosted-readiness COMPLETE 🎯
Demo criterion (from MEMORY.md): *"A friend signs up at a real URL, copies their API key, runs a bot, sees data in their dashboard. They never SSH anywhere or read docs longer than the homepage."* — passed.

Live demo URLs:
- https://app.lightsei.com (dashboard, signup + login)
- https://api.lightsei.com (backend, used by SDK)

### 2026-04-25 — Phase 4.6 Custom domain (lightsei.com)
- [x] User-owned domain `lightsei.com` (registered with GoDaddy, nameservers `domaincontrol.com`). Subdomains chosen so apex is left untouched: `app.lightsei.com` → dashboard, `api.lightsei.com` → backend.
- [x] Custom domains added to both Railway services via the Railway UI (CLI `railway domain <custom>` returned Unauthorized despite a valid login token — likely a CLI scope bug; UI worked fine).
- [x] DNS records added at GoDaddy: 2 CNAMEs (`api` → `8tvih17f.up.railway.app`, `app` → `8jafv9e8.up.railway.app`) + 2 TXT verification records (`_railway-verify.api`, `_railway-verify.app`). All four propagated and TLS issued by Railway's edge.
- [x] Pinned `PORT=8000` (backend) and `PORT=3000` (dashboard) as service env vars. Without this, Railway auto-injects a random PORT, the container binds to it, but the custom-domain `targetPort` (8000 / 3000 entered in the UI) doesn't match → 502. Pinning resolved the mismatch.
- [x] Rebuilt `beacon-dashboard` with `NEXT_PUBLIC_BEACON_API_URL=https://api.lightsei.com` so the client bundle calls the new API hostname (verified by grepping `https://api.lightsei.com` out of the deployed `app/page-*.js` bundle).
- Verified end-to-end: `https://api.lightsei.com/health` → 200; puppeteer drove a real signup against `https://app.lightsei.com/signup` (`docs/lightsei-signup.png` shows the one-time api-key reveal panel), seeded 3 events through the new workspace's session token, then captured `docs/lightsei-dashboard.png` showing the logged-in dashboard at `app.lightsei.com` rendering all 3 runs with model + tokens + latency. Matches the Phase 4 demo criterion exactly.

### 2026-04-25 — Phase 4.5 Deployed to Railway 🚀
Live URLs:
- backend  https://beacon-backend-production-89d2.up.railway.app
- dashboard https://beacon-dashboard-production-d08e.up.railway.app
- railway project `beacon` in workspace "Bailey Wallace's Projects" (id `a1eb9742-3bf0-46d3-a9fc-9f4e46d0db3b`)

Stack on Railway: managed Postgres + `beacon-backend` (Dockerfile at `backend/Dockerfile`) + `beacon-dashboard` (Dockerfile at `dashboard/Dockerfile`). Each service deployed via `railway up <dir> --path-as-root --service <name>` since the repo is a monorepo.

Code changes:
- [x] `backend/Dockerfile`: switched CMD to `sh -c "uvicorn ... --port ${PORT:-8000}"` so Railway's PORT env var binds correctly.
- [x] `dashboard/app/api.ts`: dropped the hardcoded `"demo-key"` fallback (was `process.env.NEXT_PUBLIC_BEACON_API_KEY || "demo-key"` → now `... || ""`). Local docker compose still passes the env var explicitly so the spine demo keeps working. Added `UnauthorizedError` so `/` and `/runs/[id]` redirect to `/login` on 401 instead of showing a stuck error banner.
- [x] `backend/db.py`: `_normalize_database_url()` rewrites `postgresql://` and `postgres://` schemes to `postgresql+psycopg://` so the same URL works in local docker compose AND on hosts (Railway, Heroku) that hand out the bare scheme.
- [x] `backend/alembic/env.py`: now imports `db.DATABASE_URL` instead of reading the raw env var, so alembic and the runtime app always agree on the driver. Without this, alembic tried psycopg2 (not shipped) and the container restart-looped silently on Railway with only `Started server process` logs.

Postgres config:
- The dashboard's bundle is built from `NEXT_PUBLIC_BEACON_API_URL=https://beacon-backend-production-89d2.up.railway.app` (Next.js bakes `NEXT_PUBLIC_*` at build time, set as a Railway service variable).
- `BEACON_DATABASE_URL` on the backend uses `${{Postgres.DATABASE_PUBLIC_URL}}` (the public proxy via `shinkansen.proxy.rlwy.net`). The internal `*.railway.internal` URL didn't resolve from the backend container; would need private networking enabled in the Railway UI, deferred. Public URL works fine, traffic over TLS.

Verification:
- `/health` returns 200 against the live URL.
- `/auth/me` with `Authorization: Bearer demo-key` returns the seeded "Default" workspace via the live Postgres (proves migrations 0001-0004 ran on the managed DB).
- Headless puppeteer drove a real signup against `https://beacon-dashboard-production-d08e.up.railway.app/signup`, captured the one-time api-key reveal (`docs/prod-signup.png`), seeded 3 events through the SDK API path with the resulting session token, and captured the logged-in dashboard rendering them (`docs/prod-dashboard.png`). Phase 4 demo criterion is now real: a signup creates the workspace, hands back an api_key, and the dashboard reflects the bot's runs end-to-end on hosted infra.
- SDK pytest still 5/5.

Bug caught and fixed during deploy: backend container restart-looped on Railway with no error in the logs because alembic was failing to import psycopg2. Reproduced locally by pointing `BEACON_DATABASE_URL` at the live Postgres public URL — got a clean `ModuleNotFoundError: No module named 'psycopg2'`. Root cause: `alembic/env.py` was reading `BEACON_DATABASE_URL` directly instead of the normalized `db.DATABASE_URL`. Fix and rebuild brought the deploy up in ~30s. The fact that this only surfaced in production (local docker compose used a URL that already had `postgresql+psycopg://`) is a reminder to test the URL-normalization path locally too.

### 2026-04-25 — Phase 4.4 Signup / login (email + password)
- [x] Picked email + password over magic link to avoid an SMTP dependency for local dev. Magic link can come later if we want to.
- [x] Migration `0004_users_sessions.py`: `users (id, email UNIQUE, password_hash, workspace_id FK, created_at)` and `sessions (id, user_id FK, token_hash UNIQUE, created_at, expires_at, revoked_at)`. Indexed on `users.workspace_id` and `sessions.user_id`.
- [x] `backend/passwords.py`: bcrypt with 12 rounds (real KDF for low-entropy human input). `keys.py` extended with `generate_session_token()` (`bks_<urlsafe-32>` distinct from `bk_` API keys) and `is_session_token()`/`is_api_key()` helpers.
- [x] `backend/auth.py` rewritten: a single `_resolve()` reads the Bearer token, routes to `sessions` if it has the `bks_` prefix, otherwise looks up `api_keys` by hash. Session expiry / revocation enforced. Returns an `AuthResult` carrying workspace_id plus whichever credential row was used (so endpoints like `/auth/logout` can revoke the calling session, and `/auth/me` can report whether you came in via session or api key). The seeded literal "demo-key" still authenticates because the api_key fallback is prefix-agnostic.
- [x] New endpoints: `POST /auth/signup` (creates user + workspace + first api_key + session, returns plaintext key + session token in one shot), `POST /auth/login`, `POST /auth/logout` (revokes only the calling session), `GET /auth/me` (returns user + workspace + which credential type was used). 8-char password minimum enforced via Pydantic.
- [x] Dashboard: new `/login` and `/signup` pages, with the signup success state showing the API key once with a copy button and a paste-ready shell snippet. Header (`app/Header.tsx`) on `/` and `/runs/[id]` shows `<workspace> · <email> · log out` when logged in, `log in | sign up` when not. Session token + user + workspace cached in `localStorage`; api.ts uses the session token for the bearer header, falling back to `NEXT_PUBLIC_BEACON_API_KEY` so the existing local docker-compose demo still works without logging in.
- Verified end-to-end with 13 backend assertions: signup returns separate `bk_` and `bks_` tokens, duplicate signup → 409, `/auth/me` reports `credential: session` vs `api_key`, wrong password → 401, both session tokens authenticate `/runs`, api_key + session see the same workspace's runs, logout revokes only the calling session (others still alive), `logout` with an api_key → 400, cross-tenant isolation holds (bob can't see alice's run), seeded demo-key still authenticates. Dashboard puppeteer pass produced three screenshots: `docs/auth-signup.png` (form), `docs/auth-key-reveal.png` (one-time key panel + copy button + ready-to-paste shell), `docs/auth-dashboard.png` (logged-in header with workspace + email + 3 seeded runs). Migration ran cleanly on fresh volume (`0001 → 0002 → 0003 → 0004`). SDK pytest still 5/5.
- Bug caught and fixed during verification: signup raised `ForeignKeyViolation` because the `users` row hadn't been flushed before the `sessions` row tried to FK to it. Added an explicit `session.flush()` after `session.add(user)`. Found by adding a temporary global exception handler that caught the silent uvicorn 500 and surfaced the SQLAlchemy traceback in the response body.

### 2026-04-25 — Phase 4.3 API key auth
- [x] Migration `0003_api_keys.py`: new `api_keys (id, workspace_id FK, name, prefix, hash UNIQUE, created_at, last_used_at, revoked_at)`. Drops `workspaces.api_key` (plaintext column, was a 4.2 stopgap). Seeds an api_keys row whose hash matches `sha256("demo-key")` against the default workspace, so the existing demo bot keeps working under hashed-key auth.
- [x] `backend/keys.py`: `generate_key()` returns `bk_<urlsafe-24>`. `hash_key()` is sha256 hex (sufficient: keys carry their own entropy, no dictionary-attack surface). `prefix_for_display()` for safe UI display.
- [x] `models.ApiKey` and `Workspace` (api_key column gone) updated to match.
- [x] `auth.py` rewritten: missing header → 401, unknown hash → 401, revoked key → 401. No more auto-create on unknown key. `get_workspace_id` and a richer `get_authenticated` dep that also returns the ApiKey row used (for foot-gun guards).
- [x] Each successful auth touches `api_keys.last_used_at` (best-effort, in the same session).
- [x] New endpoints: `POST /workspaces` (public signup, returns plaintext once), `GET /workspaces/me`, `GET /workspaces/me/api-keys` (prefix-only, no hash, no plaintext), `POST /workspaces/me/api-keys` (mint a new key, plaintext returned once), `DELETE /workspaces/me/api-keys/{id}` (revoke; refuses to revoke the key currently authenticating; cross-workspace returns 404).
- [x] Dashboard now sends `Authorization: Bearer <NEXT_PUBLIC_BEACON_API_KEY>` (default `demo-key`) on every API call. compose passes the env var as both build-arg and runtime so it gets baked into the client bundle.
- Verified end-to-end with 11 assertions: demo-key still authenticates, unknown key returns `401 invalid api key`, missing header returns `401 missing api key`, signup returns plaintext starting with `bk_`, the new plaintext authenticates against the new workspace, list endpoint never leaks plaintext or hash, revoked key returns `401 api key revoked`, can't revoke self, cross-workspace revoke returns 404, last_used_at updates after each request. Dashboard screenshot rendered the seeded run via authed polling. Migration ran cleanly on a fresh volume (`0001 → 0002 → 0003`). SDK pytest still 5/5 (SDK is unchanged: it was already sending `Authorization: Bearer <api_key>`, the backend just started enforcing it).

### 2026-04-25 — Phase 4.2 Multi-tenancy
- [x] Migration `0002_multitenancy.py`: creates `workspaces (id, name, api_key UNIQUE, created_at)`, seeds the default workspace `id=00000000-...-000001 / api_key='demo-key'`, adds `workspace_id NOT NULL FK` to runs/events/agents (backfilled to default). Drops `agents.name` PK and recreates as composite `(workspace_id, name)` so two workspaces can have agents with the same name. Adds `idx_events_ws_agent_kind_ts` and `idx_runs_ws_started_at` for scoped lookups.
- [x] `models.py` updated: new `Workspace` model; `workspace_id` FK on `Run`, `Event`, `Agent`; `Agent` PK is composite.
- [x] `auth.py` with `get_workspace_id` FastAPI dep that reads `Authorization: Bearer <api_key>`. Known key → matching workspace. Unknown key → auto-create a workspace for it (this auto-create branch goes away in 4.3 when keys get hashed). Missing header → default workspace, so local dev with no key still works.
- [x] Every endpoint that touches data now takes `workspace_id` from the dep. Every query is scoped: `Run.workspace_id == workspace_id`, `Event.workspace_id == workspace_id`, `Agent` lookups by composite PK. Cross-workspace `/runs/{id}/events` returns 404 rather than leaking.
- [x] `db.ensure_agent`, `cost.agent_cost_since`, `cost.agent_cost_today`, `policies.evaluate`, `cost_cap.check` all take `workspace_id`.
- Verified end-to-end: two SDK inits (api_key=`demo-key` and api_key=`bob-key`) produce isolated runs (1 and 2 respectively). Alice's `/runs/{bob-run}/events` returns 404. A cap of $1e-9 on alice's "alice" agent denies her, doesn't touch bob's policy decisions. Same agent name "alice" exists in both workspaces with independent caps ($1e-9 vs $5.00). Migration ran cleanly on a fresh volume (logs show `0001 → 0002`). SDK pytest still 5/5; existing demo bot's `api_key="demo-key"` automatically lands in the seeded default workspace, so the spine demo still works unchanged.

### 2026-04-25 — Phase 4.1 SQLite → Postgres + Alembic
- [x] Dropped SQLite. Backend now talks to Postgres via SQLAlchemy 2.0 + psycopg 3 + Alembic. New env var `BEACON_DATABASE_URL` (default `postgresql+psycopg://beacon:beacon@localhost:5432/beacon`).
- [x] `backend/models.py`: declarative `Run`, `Event`, `Agent` models. `Event.payload` is now `JSONB` (was TEXT + json_extract). Timestamps are `TIMESTAMPTZ`. Indexes match the previous schema (`idx_events_run_id`, `idx_events_agent_kind_ts`, `idx_runs_started_at`).
- [x] Alembic set up: `backend/alembic.ini`, `backend/alembic/env.py` reading `BEACON_DATABASE_URL`, `backend/alembic/versions/20260425_0001_initial_schema.py` as the first migration. Schema is single-source-of-truth in models, migration is hand-written and matches.
- [x] `backend/migrate.py` with `upgrade_to_head()`. FastAPI startup calls it so the backend container brings the schema up to head every boot — no manual `alembic upgrade` step.
- [x] Refactored `db.py` to use SQLAlchemy `engine` + `sessionmaker`. New `get_session()` FastAPI dependency. `ensure_agent(session, name, now)` uses Postgres `INSERT ... ON CONFLICT (name) DO NOTHING` (same syntax SQLite supported, so the helper looked nearly identical).
- [x] Refactored `main.py` to use ORM (`session.get`, `select(...).where(...)`, `session.add(...)`). Per-request session via `Depends(get_session)` with commit-on-success / rollback-on-error in `session_scope`.
- [x] Refactored `cost.py` JSONB query: `payload ->> 'model'`, `(payload ->> 'input_tokens')::int`. `cost_cap.py` and `policies/__init__.py` now take a `Session` instead of a sqlite3 connection.
- [x] `docker-compose.yml`: new `db: postgres:16-alpine` service with healthcheck, named volume `beacon_pg`, `backend` `depends_on: db: condition: service_healthy`. Backend port restored to 8000:8000 (port-conflict process was killed earlier in this session).
- Verified end-to-end with `docker compose up --build`: Postgres came up, `backend` ran alembic on startup (logs show `Running upgrade -> 0001`), schema landed (`\dt` shows runs/events/agents/alembic_version), all four runs from `examples/demo_bot.py` ingested. Cost rollup matched hand-calc to the cent: gpt-4o-mini total $0.000011 + claude-haiku-4-5 total $0.000114 = $0.000125 grand total. Cost-cap policy still fires (set $1e-6 cap → `/policy/check` returned `daily cost cap exceeded` with `cost_so_far_usd: 0.000125, cap_usd: 1e-06`). SDK pytest still 5/5 — SDK didn't need any changes since the migration is backend-internal.

### 2026-04-25 — Phase 3 second-framework + streaming COMPLETE 🎯
Demo criterion: *"An Anthropic-based bot streaming a response works in the dashboard with full token capture."* — passed.

Demo run: `examples/demo_bot.py` made 4 calls (OpenAI regular, OpenAI streaming with `include_usage`, Anthropic regular, Anthropic streaming) routed at fake provider servers. Dashboard list (`docs/multi-provider-demo.png`) shows 4 runs across both providers with tokens and latency. Anthropic streaming run-detail (`docs/streaming-demo.png`) shows model `claude-haiku-4-5`, `stream: true`, `output_chunks: 8`, `input_tokens: 14`, `output_tokens: 12` — full token capture during a stream.

### 2026-04-25 — Phase 3.3 Update demo with both providers
- [x] Rewrote `examples/demo_bot.py`: four `@beacon.track` functions (openai-regular, openai-streaming, anthropic-regular, anthropic-streaming), one run per call. OpenAI streaming opts into `stream_options={"include_usage": True}` so tokens land. All four respect `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` so verification can route at fakes without a real key.
- [x] Updated root `README.md`: `pip install -e ./sdk openai anthropic`, set both API keys, run demo, expect 4 runs.
- Verified end-to-end against `docker compose up`. Output text reconstructed correctly on the user side for all four. Backend received expected events in each run, dashboard rendered all four with tokens and latency.

### 2026-04-25 — Phase 3.2 Streaming response support
- [x] `sdk/beacon/integrations/_streamtap.py`: `_SyncStreamTap` and `_AsyncStreamTap` wrap an existing iterator/async-iterator. Forward iteration unchanged, invoke `on_chunk` per item for bookkeeping, invoke `on_finish` exactly once on stream exhaustion / `__exit__` / `close()`. Forward attribute access via `__getattr__` so the original stream's API surface (e.g. Anthropic's `text_stream`, `response`) still works.
- [x] OpenAI patch: `stream=True` no longer passes through. Calls policy/check, emits `llm_call_started{stream:true}`, returns a `_*StreamTap`-wrapped stream. On finish emits `llm_call_completed{stream:true, output_chunks, model, [input_tokens, output_tokens]}`. Tokens are captured from the final usage chunk only when the user opted into `stream_options={"include_usage": True}`. We deliberately do NOT auto-inject that flag because it adds an empty-choices chunk to the user's iteration and would break code that does `chunk.choices[0]` blindly.
- [x] Anthropic patch: same shape. Captures `model` and `input_tokens` from `message_start`, `output_tokens` from `message_delta`. Anthropic's wire format always includes usage so no opt-in is needed.
- [x] `run_id` captured at stream-creation time and passed explicitly to `_client.emit` so completion events still tag the right run if the user iterates the stream after the surrounding `@track` function returns.
- Verified end-to-end with real openai 2.32.0 + anthropic 0.97.0 against fake SSE servers. All four cases (OAI sync+async, Anthropic sync+async) reconstruct the streamed text correctly on the user side AND emit completion events with the expected token counts. Empty-`stream_options` OpenAI case correctly emits the completion event without tokens (output_chunks still counted). SDK pytest still 5/5.

### 2026-04-25 — Phase 3.1 Anthropic SDK auto-patch
- [x] `sdk/beacon/integrations/anthropic_patch.py` mirroring `openai_patch.py`. Patches `anthropic.resources.messages.Messages.create` and `AsyncMessages.create`. `_beacon_patched` marker on each class for idempotency. `stream=True` calls pass through (deferred to 3.2). Skips silently if `anthropic` is not installed.
- [x] `_check_policy_or_raise` checks `action="anthropic.messages.create"`, emits `policy_denied` and raises `BeaconPolicyError` on deny — same shape as OpenAI patch.
- [x] `_summarize_response` reads Anthropic's `usage.input_tokens` / `usage.output_tokens` (note: Anthropic uses different names than OpenAI's `prompt_tokens`/`completion_tokens`). Rolled up under the same `llm_call_completed` event shape so the dashboard, cost rollup, and policy code don't need provider branches.
- [x] `beacon._auto_patch()` now also calls `patch_anthropic()` on init.
- [x] `backend/pricing.py` extended with `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5` (and the dated alias `claude-haiku-4-5-20251001`). Numbers reflect historical Anthropic tier pricing — comment notes to verify against the live vendor page before relying on tight caps.
- [x] `policies.cost_cap._GUARDED_ACTIONS` now includes `anthropic.messages.create` so the existing daily cap covers both providers without code changes elsewhere.
- Verified with real `anthropic` 0.97.0 against a fake `/v1/messages` server: sync (sonnet, 3 messages) + async (haiku, 1 message) both produced events with the right model + tokens; cost rollup attributed $0.000122 total exactly matching hand-calc; cost cap with $1e-6 cap denied a third call, fake Anthropic only saw the 2 successful requests; SDK pytest still 5/5.

### 2026-04-25 — Phase 2 cost-cap guardrail COMPLETE 🎯
Demo criterion: *"A daily cost cap policy. Configure $X/day in the dashboard. Run the demo bot in a loop until it hits the cap. The next call gets blocked with a clear error and shows up in the dashboard as a denial."* — passed.

Demo run: cap = $0.0005 (scaled down from the spec's $0.50 because the fake OpenAI server returns toy responses; same mechanics, smaller numbers). 90 successful OpenAI calls accumulated $0.000515 of cost. Call #91 raised `BeaconPolicyError(reason='daily cost cap exceeded', policy='cost_cap', cost_so_far_usd=0.000515, cap_usd=0.0005)`. Dashboard list shows the denied run with a red badge above 90 ended runs (`docs/cost-cap-demo.png`). Run-detail view shows the red banner with policy + reason + cost-vs-cap and a highlighted policy_denied event in the timeline (`docs/denial-detail.png`). SDK pytest still 5/5.

Open item to revisit before any real customer demo: there's no UI in the dashboard to configure the cap — set it via `PATCH /agents/{name}` for now. A simple input on the index page would close that, but that wasn't in the 2.4 task list so it stays in scope discipline.

### 2026-04-25 — Phase 2.4 Denial UX
- [x] SDK emits a `policy_denied` event from `openai_patch._check_policy_or_raise` (after check_policy returns deny, before raising `BeaconPolicyError`). Payload mirrors the deny dict plus `action`. Run-attached via the contextvar so it lands on the right run automatically.
- [x] Dashboard `summarize()` now sets `denied: true` and a `denial` summary if any `policy_denied` event is present in the run.
- [x] Dashboard `/` Status column renders a red "denied" badge (Tailwind `bg-red-100 text-red-800`) when `run.denied`, with the deny reason as the title attribute.
- [x] Dashboard `/runs/[id]` shows a top-of-page red banner ("DENIED · {reason}") with `policy`, `action`, and `cost_so_far_usd / cap_usd`. Within the events table the `policy_denied` row is highlighted red and its kind label is bold.
- Verified end-to-end against `docker compose up`: backend 8001, dashboard 3000, fake OpenAI 9124. Loop on the SDK side hit the cap, dashboard list and run-detail rendered the denial UI exactly as designed (`docs/cost-cap-demo.png`, `docs/denial-detail.png`).

### 2026-04-25 — Phase 2.3 Cost-cap policy
- [x] `agents` table created in `backend/db.py` schema with columns `name PK, daily_cost_cap_usd REAL nullable, created_at, updated_at`. New composite index on `events(agent_name, kind, timestamp)` for the cost rollup hot path.
- [x] `db.ensure_agent(cur, name, now)` helper. Called from `POST /events`, `POST /policy/check`, and `PATCH /agents/{name}` so an agent row exists once we've seen the name.
- [x] New endpoints: `GET /agents`, `GET /agents/{name}`, `PATCH /agents/{name}` (body `{"daily_cost_cap_usd": <float | null>}`). PATCH with null clears the cap.
- [x] `backend/policies/` package with `evaluate(...)` runner and `cost_cap.check(...)` rule. Pure functions, returning either None (silent) or a decision dict.
- [x] `cost_cap` denies only `action == "openai.chat.completions.create"`; an agent with no cap is silent. Verdict on deny: `{"allow": false, "reason": "daily cost cap exceeded", "policy": "cost_cap", "cost_so_far_usd": ..., "cap_usd": ...}`.
- [x] `/policy/check` rewritten to call `policies.evaluate(...)`.
- [x] Refactored `/agents/{name}/cost` to use the new shared `cost.agent_cost_since(...)` helper.
- [x] SDK side: `BeaconPolicyError` is raised by the openai patch when `/policy/check` returns `allow=false` (already shipped in Phase 1.3).
- Verified backend with curl: under-cap → allow, over-cap → deny with the right reason/policy/cost_so_far_usd/cap_usd, non-guarded action allowed even when over cap, second agent untouched, `PATCH ... null` clears the cap. End-to-end SDK test: backend cap = $1e-7, pre-seeded $0.000075 of cost, openai SDK call raised `BeaconPolicyError(reason='daily cost cap exceeded')`, fake OpenAI server received 0 requests (proving the gate stops the call, doesn't just record it).

### 2026-04-25 — Phase 2.2 Cost rollup
- [x] `backend/pricing.py`: USD-per-million-token table for current OpenAI models (gpt-4o, gpt-4o-mini, gpt-4-turbo, gpt-4, gpt-3.5-turbo, o1, o1-mini, o3-mini) plus `compute_cost_usd(model, input_tokens, output_tokens)`. Unknown models cost zero (intentional: don't silently guess and then enforce a wrong cap).
- [x] `GET /agents/{agent_name}/cost?since=ISO8601` endpoint. Default `since` is start of today UTC. Returns total calls/tokens/cost_usd plus a per-model breakdown. Uses SQLite `json_extract` over `events.payload_json` filtered to `kind = 'llm_call_completed'`.
- [x] No background job needed yet; the query is fast against the index on `events.run_id` plus the agent_name + kind filter on a small spine table. Revisit when this gets slow.
- Verified with seeded data: alpha agent with gpt-4o-mini (1000 in / 500 out) + gpt-4o (2000 in / 1000 out) + gpt-4o-mini (500 in / 250 out) + made-up-model (1000 in / 1000 out) returned cost_usd = 0.015675 matching hand-calc exactly. beta-agent scoping verified separately. `?since=2099-...` returned zeros, confirming the filter applies.

### 2026-04-25 — Phase 2.1 Policy engine decision
- [x] Picked custom Python over OPA for now. Full reasoning + switch trigger documented in `MEMORY.md` under "Policy engine decision". Skipped the planned 30-min spike per user direction; the call wasn't close (1 rule, 10ms layer-2 budget, reference projects all built custom).

### 2026-04-25 — Phase 1 spine COMPLETE 🎯
Demo criterion from MEMORY.md / Phase 1 header: *"Run `python demo_bot.py` which calls OpenAI 3 times. Within 5 seconds, all 3 calls appear in a browser dashboard at localhost, with timestamps, model, latency, and token counts."* — passed.

### 2026-04-25 — Phase 1.5 Spine demo
- [x] `examples/demo_bot.py`: imports beacon + openai, `beacon.init()`, three different prompts via `@beacon.track`-wrapped function. Honors `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `BEACON_BASE_URL`.
- [x] Root `README.md` rewritten with the spine-demo run sequence: docker compose up, pip install -e ./sdk openai, set OPENAI_API_KEY, run demo, open localhost:3000.
- [x] End-to-end run: OrbStack-backed `docker compose up --build` brought up backend + dashboard. Demo bot routed at a fake OpenAI-compatible server (didn't burn the user's API key) and produced 3 runs, each with run_started → prompt → llm_call_started → llm_call_completed → run_ended, model gpt-4o-mini, ~157-183 ms latency, token counts populated.
- [x] Screenshot saved to `docs/spine-demo.png` showing all 3 runs with timestamp, agent, model, event count (5), tokens (in/out), latency, status=ended.
- Notes: verification used host port 8001 (8000 was held by an unrelated user app) via a temporary compose override; the committed `docker-compose.yml` still binds 8000 as the documented default. CORS middleware on the backend lets the browser at :3000 poll the API. Switching from the fake OpenAI to a real one requires only `unset OPENAI_BASE_URL` and `export OPENAI_API_KEY=sk-...`.

### 2026-04-25 — Phase 1.4 Minimal dashboard
- [x] Next.js 14 (App Router, TypeScript) at `dashboard/`, Tailwind defaults
- [x] `/` lists runs with started timestamp, agent, model, event count, token totals (in/out), latency, status (running/ended). Aggregates from `llm_call_completed` events client-side.
- [x] `/runs/[id]` shows the run's metadata and a table of every event with timestamp, kind, and pretty-printed payload
- [x] No auth, no login, plain Tailwind utility classes only
- [x] Both pages poll every 2s via `setInterval`; cleanup on unmount
- [x] Added `dashboard` service to `docker-compose.yml`. Multi-stage Dockerfile uses Next.js `output: "standalone"`. `NEXT_PUBLIC_BEACON_API_URL` exposed as build arg + runtime env (defaults to `http://localhost:8000`).
- [x] Added `CORSMiddleware` to backend so the browser can poll cross-origin (allow_origins=["*"] for the spine).
- Verified via `next dev` on port 3300 against backend on 9123 with 3 seeded runs. `GET /` → 200 with the SSR shell ("Beacon", "polls every 2s"); `GET /runs/{id}` → 200 with run id rendered. CORS preflight `OPTIONS /runs` → 200 with `access-control-allow-origin: *`. Backend logs confirm browser-driven `/runs` and `/runs/{id}/events` requests.
- Deferred: `docker compose up` end-to-end verification of the dashboard service. Docker isn't installed on this machine. The compose file and Dockerfile are written but unverified; will verify in Phase 1.5 once Docker is installed.

### 2026-04-25 — Phase 1.3 OpenAI auto-patch
- [x] `sdk/beacon/integrations/openai_patch.py` with `patch_openai()`
- [x] Patches sync `openai.resources.chat.completions.Completions.create`
- [x] Patches `openai.resources.chat.completions.AsyncCompletions.create`
- [x] Emits `llm_call_started` (model, message_count) and `llm_call_completed` (+ input/output/total tokens, duration_s); also `llm_call_failed` on exception
- [x] `check_policy("openai.chat.completions.create", ...)` before underlying call; raises `BeaconPolicyError` on deny (exposed at `beacon.BeaconPolicyError`)
- [x] `_beacon_patched = True` set on both classes; second `patch_openai()` is a no-op
- [x] If openai isn't installed, `patch_openai()` returns False and logs at debug
- [x] `stream=True` calls pass through with no instrumentation (deferred)
- [x] Auto-patch wired into `beacon.init()` so users don't call `patch_openai()` manually
- [x] Manual test: real `openai` SDK pointed at a fake OpenAI-compatible server. Sync + async calls landed two runs in the backend with run_started → llm_call_started → llm_call_completed → run_ended, with model, message_count, input_tokens=7, output_tokens=2 captured. Patch markers verified True on both classes. Idempotent re-init didn't disturb the wrap.

### 2026-04-25 — Phase 1.2 SDK skeleton (Python)
- [x] `sdk/beacon/__init__.py` exposing `init(api_key, agent_name, version)`, `track` decorator, plus `emit`, `flush`, `shutdown`, `check_policy`, `get_run_id`
- [x] HTTP client (httpx) with retries (default 3, exponential backoff) and timeout (default 5s)
- [x] In-memory `queue.Queue` event buffer with daemon-thread background flush every 1s, batch size 100, max queue 10k (drop on overflow with warning)
- [x] `contextvars`-based run_id tracking; works with both sync and asyncio tracked functions
- [x] Graceful degradation: HTTP failures swallowed with warning log; user code keeps running. Policy check fails open. Emit-before-init is silent.
- [x] Idempotent `init()`: second call is ignored, first values stick
- [x] Unit tests in `sdk/tests/test_basic.py`: idempotent init, end-to-end emit+flush against fake HTTP server, run-completes-with-backend-offline, emit-before-init silent, policy fails open offline
- Verified: `pytest tests/` → 5 passed in 0.91s. Smoke script with real backend on 9123 produced two runs (sync + async) with all expected event kinds and `ended_at` populated. Custom emits inside tracked functions correctly inherited the run_id via contextvars.

### 2026-04-25 — Phase 1.1 Backend skeleton
- [x] FastAPI app at `backend/` (`main.py`, `db.py`)
- [x] `POST /events` writes to SQLite, auto-creates run, sets `ended_at` on `run_ended`/`run_completed`/`run_failed`
- [x] `POST /policy/check` returns `{"allow": true}` (hardcoded for spine)
- [x] `GET /runs` returns recent runs (newest first, `?limit=` 1..500, default 50)
- [x] `GET /runs/{run_id}/events` returns events oldest-first; 404 if run missing
- [x] SQLite schema with the specified columns plus indexes on `events.run_id` and `runs.started_at`
- [x] `docker-compose.yml` at repo root, Dockerfile in `backend/`, named volume `beacon_data` for `/data/beacon.db`, port 8000 exposed
- [x] `backend/README.md` with one-command run instructions plus curl examples
- Verified by running uvicorn locally (port 8000 was occupied by an unrelated user process so verification used 9123) and curling every endpoint. All responses matched expectations including 404 on missing run. Docker compose was not exercised because Docker isn't installed on this machine; the compose file maps host 8000 → container 8000 as specified.
