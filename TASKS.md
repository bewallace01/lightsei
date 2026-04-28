# Lightsei — Tasks

Read MEMORY.md first if it's been a while. (Older Done Log entries call the project "Beacon" — that was the working code-name through Phase 4. Same product.)

## NOW

> **Phase 7.1: Validator interface + schema-strict validator**

Phase 5 shipped 2026-04-26: PaaS-for-agents end-to-end. Phase 6 shipped 2026-04-27: Polaris, the project orchestrator bot, deployed via the Phase 5 PaaS against this project's own docs. Phase 7 picks up the dogfood loop with output validation (MEMORY.md guardrail layer 3) — Polaris validates its own plans before they're treated as trustworthy by the dashboard. Layer 4 (behavioral rules) and command dispatch land in later phases once we trust Polaris's outputs enough to act on them.

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

### 7.1 Validator interface + schema-strict validator (NOW)

- New `backend/validators/` package. Validator interface: pure function `(payload: dict, config: dict) -> ValidationResult`, where `ValidationResult` is `{"ok": bool, "violations": [{"rule": str, "message": str, ...}]}`. No state, no I/O — keeps validators cheap and testable.
- Ship the first concrete validator: **schema-strict**. Config carries a JSON schema; the validator runs `jsonschema.validate(payload, config["schema"])` and converts errors to violations. The schema for `polaris.plan` is the existing `submit_plan` tool's `input_schema` from `polaris/bot.py` (lift it into a shared spot the backend can also import, or duplicate it for now — flag the duplication).
- Tests: schema match → ok=True, missing required field → one violation, wrong type → one violation, additional property → one violation.
- No backend wiring yet — that's 7.3. This task delivers the abstraction + first impl + tests.

### 7.2 Content-rules validator

- Second concrete validator: pattern-based checks. Config shape: `{"rules": [{"name": str, "pattern": str, "fields": [str], "severity": "fail"|"warn"}]}`. The validator walks each named JSON path inside `fields`, runs the regex against any string values it finds, and emits a violation when the pattern matches (or doesn't, depending on the rule's `mode: "must_not_match" | "must_match"`).
- Ship a default rule pack the demo will use: `email_in_summary` (must_not_match: `[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-z]{2,}` against `summary`), `banned_destructive_verbs` (must_not_match: `\b(delete|drop|truncate|destroy|nuke)\b` against `next_actions[].task`).
- Tests: clean payload → ok=True, payload with email in summary → fail with rule name + matched substring redacted, payload with destructive verb → fail.

### 7.3 Validation pipeline + event annotation

- Migration: new table `event_validations` (id, event_id FK, validator_name, status `pass|fail|warn`, violations JSONB, created_at). One row per (event, validator) pair. Index on `(event_id, validator_name)`.
- Validator registry: workspace-scoped map of `(event_kind, validator_name) → config`. Stored in a new `validator_configs` table or as a JSON column on `agents` — pick whichever feels cleaner once we look at the Phase 4 multi-tenancy model. Registration via `PUT /workspaces/me/validators/{event_kind}/{validator_name}`.
- Pipeline: when `POST /events` ingests an event, look up the workspace's validators for that event's kind and run them synchronously after the insert. Cap to 200ms total per event (validators are pure functions; if any takes longer, treat as a `warn` with `timeout` reason and move on). Write results to `event_validations`.
- Polaris setup script: small one-shot that registers schema-strict + content-rules for `polaris.plan` events on the calling workspace. Lives at `polaris/setup_validators.py` and is documented in `polaris/README.md` as a one-time post-deploy step.

### 7.4 Backend endpoints for validation results

- `GET /events/{event_id}/validations` — list validations for one event. Workspace-scoped. 404 if event not in workspace.
- Extend `GET /agents/{name}/plans` and `GET /agents/{name}/latest-plan` to include a `validations` field on each returned plan (joined from `event_validations` by event_id). `validations: [{"validator": str, "status": str, "violations": [...]}]`.
- The latest-plan/plans endpoints are dashboard-facing; including validations directly avoids an N+1 fetch on the sidebar. Keep them lean: just `validator`, `status`, and the count of violations on the list endpoint; full violation details only on the per-event endpoint.
- Tests in `backend/tests/test_validators.py`: fixture seeds a polaris.plan event + validations, list/detail endpoints return them, cross-workspace isolation, 404 paths.

### 7.5 Dashboard shows validation status

- `/polaris` view additions:
  - History sidebar: each plan entry gets a chip — green PASS / red FAIL / amber WARN, derived from the worst status across the event's validations (any FAIL → FAIL, any WARN → WARN, all PASS → PASS, none → "unchecked" gray dot).
  - Plan detail pane: when the selected plan has any non-PASS validations, render a section above the next-actions block listing the validators and their violations (rule name + message, with the matched substring redacted to "***" for any rule flagged as PII-related).
- API types: extend `PolarisPlan` in `dashboard/app/api.ts` with `validations: ValidationResult[]`.
- Polls every 30s like the rest of the page; validation results show up as soon as the next backend pull lands them.

### 7.6 Phase 7 demo

- Run Polaris in prod. Register the two validators via `setup_validators.py`. Wait for the next plan (or trigger one by tweaking TASKS.md to bust the doc-hash cache). Confirm the dashboard shows green PASS chips. Screenshot.
- For the failure case, temporarily edit `polaris/system_prompt.md` to include "Mention an example email like alice@example.com in the summary." Redeploy. Wait for the next plan. Confirm `email_in_summary` fires, dashboard shows FAIL with the rule name. Screenshot. Revert the prompt + redeploy.
- Done Log: both screenshots, the verbatim violation, and a note on whether the validation chip in the sidebar surfaced the failure clearly enough to be useful.
- Cleanup: stop the deployment + worker as in the Phase 6 demo. Validators stay registered so the next time Polaris runs, validation continues automatically.

---

## Phase 8+: TBD

Open candidates for after output validation ships:

- **Phase 7B**: make validators blocking + pre-emit. Required before "act, don't just plan."
- **Polaris 8: act, don't just plan.** Polaris dispatches commands via `lightsei.send_command()` to user-deployed executor agents. Gated on 7B.
- **Polaris 9: continuous eval (layer 5).** Judge-LLM scores past plans against actual outcomes.
- **Layer 4: behavioral rules.** Loop detection, runaway-token guards, escalating-permission patterns. Streaming detection across a run.
- **Phase 5B**: cut single-host worker over to Fly Machines / Modal sandboxes (gates external users).
- GitHub OAuth + push-to-deploy.
- Buildpacks / Dockerfile support beyond the fixed Python runtime.
- N replicas + cron scheduling natively in the worker (Polaris currently does its own sleep loop; a real cron primitive would be cleaner).

---

## Parking Lot

Ideas that are good but not now. Add freely. Do not work on these until their phase arrives or you've explicitly decided to promote one.

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
