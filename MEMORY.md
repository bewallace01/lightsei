# Lightsei — Memory

> Project was originally code-named "Beacon" through Phase 4. Renamed to Lightsei (matching lightsei.com) on 2026-04-25. Older Done Log entries still say "Beacon" — those are historical record, leave them alone.

This file is the source of truth for the project. Read it at the start of every session. If something here is wrong or outdated, fix it before doing anything else.

## What we're building

A platform where non-technical users assemble and operate teams of AI coworkers. The dashboard (and eventually a chat client surface like Slack or Teams) is the product. The SDK, backend, observability, and guardrails are the engine room that makes the coworkers safe, reliable, and inspectable; most customers never see them directly.

Think Viktor (viktor.com), but built around a configurable team of specialized bots instead of one generalist coworker, and with security and trust zones as defaults rather than later upgrades. See "Competitive north star" section below for the full positioning and "Target customer shape" for the buyer profile.

## Why this exists

Non-technical teams increasingly want AI coworkers that do real work, not just chat. Viktor (viktor.com) is the closest current option, but it ships as one generalist bot with workspace-wide shared context, which rules it out for anyone whose compliance team won't let an agent co-mingle PII across users. Nobody else ships configurable teams of specialized bots that a non-technical user can stand up safely. The wedge is shipping that, with trust zones and per-team isolation as defaults rather than enterprise upgrades.

(Historical note: through Phase 4 the wedge was framed as "drop-in observability and guardrails for developers, ridiculously easy install: pip install + one init() call." That framing was retired on 2026-05-17 when the configure-your-team direction was decided. The SDK and observability work it produced is still load-bearing as internal infrastructure; it just isn't the customer-facing pitch anymore.)

## Vision in one line

Easy by default, powerful when you grow into it.

90% of users never leave the dashboard. They describe the team they want, connect a few tools, and the bots do the work. The other 10% drop into the underlying SDK, write custom policies, or self-host the whole stack.

## Core architecture (decisions already made — do not re-debate)

### Hosting
- Hosted SaaS first. Backend is multi-tenant from day one (after spine works).
- Self-hosting comes later as a pro feature.

### SDK design
- Auto-patches frameworks at import time. No manual tool wrapping.
- Two API surfaces: magic API for most users, pro API underneath for advanced.
- Graceful degradation: if backend is down, user code keeps running. Non-negotiable.
- `init()` is idempotent.

### Stack
- SDK: Python first. JS/TS comes later.
- Backend: FastAPI, Postgres, Redis, Clickhouse for events, Kafka for ingestion. SQLite during spine phase to keep it simple.
- Dashboard: Next.js.
- Policy engine: **custom Python** (decided 2026-04-25, see "Policy engine decision" below). OPA stays a future option.
- Deploy: Docker Compose locally. Fly/Railway/Render for hosted.

### The agent contract
Every bot, regardless of type (LLM agent, script, scheduled job), exposes the same surface:
- manifest (declared identity and capabilities)
- emit (telemetry events)
- check_policy (gate before side effects)
- heartbeat (liveness)
- receive_command (control plane)

The framework adapters (OpenAI, Anthropic, LangChain) handle this automatically so users never see the contract directly.

### Five guardrail layers
1. Identity and intent (run start)
2. Pre-action gate (per side effect, must stay under 10ms)
3. Output validation (before delivery to user)
4. Behavioral rules (across a run, streaming detection of bad patterns)
5. Continuous evaluation (background sampling with judge-LLM)

Only layer 2 ships in the spine. Others are added as separate phases.

## Working principles (the ADHD guardrails)

These exist because momentum matters more than perfection. Re-read these when you feel pulled to a shiny new task.

1. **Build the spine first.** The spine is: bot → SDK → backend → dashboard, end to end, ugly but working. Nothing else gets built until the spine demo runs.
2. **One framework first.** OpenAI Python only. Resist adding Anthropic, LangChain, etc. until spine is done.
3. **Dogfood before features.** One real bot using Lightsei before adding new features.
4. **Done means it works, not it's perfect.** Ugly UI is fine. Hardcoded values are fine. Tests can come later.
5. **New ideas go to the Parking Lot in TASKS.md.** Do not interrupt the current phase.
6. **Each phase ends with a DEMO.** A specific, runnable thing you can show. No demo = phase isn't done.

## Things to NOT build until the spine works

If you find yourself working on any of these before Phase 1's demo runs, stop:

- A second framework adapter
- A second language SDK
- Custom policy DSL
- Behavioral guardrails (layer 4)
- Eval pipeline (layer 5)
- Self-host packaging
- Auth, signup, billing
- Pretty dashboard styling
- Slack or PagerDuty alerts
- Multi-region

Note (added 2026-05-17, after the configure-your-team direction was decided): "Pretty dashboard styling" and "Auth, signup, billing" stay on this list until the spine demo runs, but they are not "deferred forever" items the way most of the others are. Under the new direction the dashboard is the product and non-technical users need to self-serve signup, so both become high-priority right after the spine, not "someday." Don't conflate them with the genuinely-later items in the same list.

## Glossary

- **Agent**: any bot. LLM agent, script, scheduled job. They all look the same to Lightsei.
- **Run**: one invocation of an agent (e.g., one chatbot conversation, one scrape job).
- **Event**: a single telemetry record, always tied to a run_id.
- **Manifest**: declared identity and capabilities of an agent (advanced; spine doesn't need it).
- **Policy**: a rule that gates an action before it happens.
- **Spine**: the minimal end-to-end loop. The first thing we build.

## Policy engine decision (2026-04-25)

Phase 2.1 asked: OPA or custom Python?

**Picked: custom Python.** Reasoning:
- Phase 2 ships exactly one rule (daily cost cap). Phases 3 and 4 add zero new policies. We won't see 5+ rules for a while.
- Reference projects all built their gates as plain code: Sentry rules, Helicone rate limits, PostHog feature flags. None of them reach for OPA at this stage.
- OPA adds a sidecar container, the Rego language, and a network hop on every gate. The 10ms budget for layer-2 (per MEMORY.md "Five guardrail layers") is tight enough that I want to avoid that round trip until we have a reason to pay it.
- Decision logs are already free: every gate emits a Lightsei event.

**Switch trigger.** Reopen this decision when ANY of these is true:
- We have ~5 rules in `backend/policies/` and the file is starting to feel like a DSL we invented.
- A self-host customer wants to author policies without redeploying the backend.
- We need policy-as-code review workflows (e.g., PR review on policy bundles).

Keep policies in `backend/policies/` as small pure functions taking a request dict and returning `{"allow": bool, "reason": str | None, ...}`.

## Runtime decision (2026-04-27)

Phase 5 is "PaaS for agents": user runs `lightsei deploy ./bot`, Lightsei hosts the bot process and shows logs/status in the dashboard.

**Picked: build our own single-host worker for v1, swap for a managed runtime (Fly Machines, Modal sandboxes) in Phase 5B.** Reasoning:

- The control-plane shape (start, run, log, restart, stop) is the same regardless of where the container lives. Validating that shape doesn't require microVM isolation.
- Building on a vendor primitive in v1 means spending on vendor + integration work before knowing if anyone wants this. The throwaway POC under `worker/run_local.py` already proved the lifecycle.
- Phase 5A is *only safe for our own bots*. We do NOT accept other users' code on the worker until we cut over to per-bot microVMs. The bot runs as the worker's user; any escape is full host compromise.
- We'll abstract a `Runtime` interface so that swapping `LocalSubprocessRuntime` for `FlyMachinesRuntime` later is a one-file change. Don't let vendor primitives leak past `worker/`.

**Switch trigger** (Phase 5A → 5B). Cut over to a managed isolation runtime when ANY of these is true:
- We want to onboard external users (the moment isolation becomes a security requirement, not a nice-to-have).
- The single-host worker is saturating CPU/memory regularly.
- Multi-region presence becomes a real requirement.

**Storage decisions:**
- Uploaded zips live in a `deployment_blobs` BYTEA column for v1 (cap 10 MB). Move to Cloudflare R2 in Phase 5B if/when the DB feels bloated.
- Logs land in a `deployment_logs` table (worker streams lines, dashboard polls). Same future pivot to object storage for archival.

**Prod topology update (2026-05-17).** The single-host worker now lives as a Railway service (`lightsei-worker`) alongside `lightsei-backend` and `lightsei-dashboard` in the `lightsei` project. Before this, the worker was running on Bailey's laptop pointed at api.lightsei.com — closing the lid took every bot offline simultaneously (polaris + every team-from-README bot all went stale together). Worker only needs `LIGHTSEI_WORKER_TOKEN` (copy from backend's vars) and `LIGHTSEI_BASE_URL=http://beacon-backend.railway.internal:8000` (internal Railway domain — backend's private domain still uses the old `beacon-` name from before the rename; don't "fix" it without checking what the backend service's `RAILWAY_PRIVATE_DOMAIN` is). Source files: `worker/runner.py`, `worker/Procfile`, `worker/requirements.txt` (httpx only — everything else is stdlib). One known follow-up: the worker service was created via `railway up --path-as-root worker` because the CLI's `--repo` flag kept hitting "Unauthorized" on the GitHub link; need to wire GitHub auto-deploy via the Railway dashboard so pushes to main rebuild the worker the same way they rebuild backend + dashboard.

**Worker concurrency override (2026-05-17).** Set `LIGHTSEI_WORKER_MAX_CONCURRENT=8` on the Railway worker service. The code default in `worker/runner.py` is 4, which was full as soon as the team-from-README team (argus/vega/vela/spica) all started running — polaris couldn't get a slot. 8 gives comfortable headroom for the current team + polaris + a few more from future demos on a single Railway instance. If the worker starts saturating CPU/memory regularly, that's the signal to either lower this back or split across two worker instances (which is the same signal for revisiting the Runtime decision). The override lives only on the Railway service, not in the repo — `railway variables --service lightsei-worker --kv | grep MAX_CONCURRENT` is the source of truth.

## Async-job queue decision (2026-05-16)

Phase 12C.6 needed to move `POST /agents/generate` and `POST /teams/plan` off the request path (Opus + tool-call round trips outran Railway's ~100s edge timeout). Options on the table: Celery + Redis, RQ, an external queue (SQS / Cloud Tasks), or a custom in-process runner.

**Picked: single in-process asyncio runner backed by one `generation_jobs` Postgres table.** Reasoning:

- Lightsei runs as one Railway service. A multi-instance queue would be premature.
- We already use Postgres for everything; adding Redis just for a queue doubles ops surface for one feature.
- The work is bounded: only the two LLM-call endpoints need this today. Async-job ergonomics for the rest of the codebase aren't a goal yet.
- SKIP LOCKED on the claim query means a future second instance (or a stray reload) can't double-process the same row, so we don't have to redesign when we eventually scale out.
- Reference projects all land in this same neighborhood for v1: Langfuse uses its DB for queueing before introducing Redis; PostHog ran on Postgres queues for years.

**Pattern, briefly:**
- Endpoints validate input synchronously, insert a `generation_jobs` row (`status='pending'`), return `{job_id, status: 'pending'}` 202.
- `backend/jobs.py` runs one asyncio task started in FastAPI's lifespan. It claims one row at a time via `SELECT … FOR UPDATE SKIP LOCKED LIMIT 1`, dispatches by `kind` through a handler registry, runs the (sync) handler in `asyncio.to_thread`, finalizes with `result_payload` (success) or `error` text (failure).
- Handlers register themselves on import via `jobs.register_handler(kind, fn)`. No central dispatch table to keep in sync.
- No auto-retry in v1. `attempt_count` bumps on each claim; the dashboard surfaces `error` and the user retries from the UI (which enqueues a fresh row).
- Dashboard's `api.ts` hides the kick-off-and-poll loop behind the same signatures the rest of the dashboard already calls (`pollGenerationJob(jobId)` helper).

**Switch trigger.** Reopen this when ANY of these is true:
- We add a second backend instance (the in-process runner becomes a per-instance picker and we want fewer pickers per row).
- We need scheduled / delayed jobs (cron-shaped work, not just "as soon as possible").
- We need real durability for failures (retry-with-backoff, dead-letter handling, etc.).
- Sustained queue depth becomes a thing (steady >1 job in flight for >1 minute regularly), at which point the single-task serial runner gets in the way.

If we cross any of those, the most likely next pivot is: keep `generation_jobs` as the source of truth, but introduce Celery or RQ workers consuming it (they treat the row as the unit of work) so we don't have to migrate every handler at once.

## Reference projects

When stuck on architecture, look at these:
- Langfuse (open source LLM observability)
- Helicone (LLM proxy and observability)
- PostHog (best-in-class plug-and-play SDK experience)
- Sentry (gold standard for graceful degradation and auto-instrumentation)

## Target customer shape: multi-bot systems with trust zones (2026-05-17)

A concrete customer profile we want Lightsei to serve well: organizations running multiple bots that share a single workflow but live on opposite sides of a security boundary. Canonical example a user described:

- Bot A (the "internet bot") sits in a meeting room everyone in the org can join. Can browse the web, can be addressed openly by anyone in the meeting. Has no access to the CRM or any internal PII.
- Bot B (the "CRM bot") operates inside the CRM. Has read (and possibly write) access to customer records. Has no internet access.
- The two bots cannot talk to each other directly. The human in the meeting acts as the translation layer: hears the open-room ask, writes the sanitized prompt to the CRM bot.

The shape generalizes well beyond CRMs (any time PII and the outside world both touch the same workflow). Three implications for Lightsei's design:

1. **Trace correlation across a human gap.** The internet-bot trace and the CRM-bot trace are two unrelated runs from Lightsei's perspective unless we let customers log the human translation step. Lean toward making this optional but easy: an explicit "handoff" span the SDK can record, so chains can be reassembled when teams care.

2. **Trust zones in storage and access.** Internet-bot traces are PII-free by design and can live anywhere. CRM-bot traces are loaded with PII. Customers should be able to tag a project, run, or agent with a sensitivity level and have Lightsei apply redaction or access controls accordingly. Affects the SDK contract (redact at the SDK layer like Langfuse, vs. at the backend like some others), so worth picking a side before output-validation layers ship.

3. **Issue surfacing toward a "master bot" dashboard.** The dashboard becomes the inbox where bot failures and odd behaviors get surfaced to operators automatically, not just when end users complain. The data model should leave room for an "issue" or "alert" attached to a run from the start, even if the surfacing UI lands later.

None of this changes the current phase. The implementation work is now sized into Phase 16 (trust zones as a first-class concept: sensitivity tags, capability model, cross-zone dispatch enforcement, redaction, handoff span, presets) and Phase 21 (customer-facing chat widget + operator inbox + Polaris-extended incident response) in TASKS.md. The original Parking Lot entry still exists as a pointer until Phase 16 actually ships, but the canonical work breakdown lives in the phases now.

## Competitive north star: Viktor, but built security-first (2026-05-17)

User has identified Viktor (viktor.com) as the closest direct analogue to the long-term shape of Lightsei. Viktor is an AI coworker that lives in Slack/Teams, runs in its own cloud compute, connects to ~3000 SaaS tools, and produces real artifacts (PDFs, dashboards, web apps, code, emails). It is not an observability tool; it is the agent product itself. SOC 2 Type 1, $50/mo after free credits, backed by Zeta Labs / Jace AI.

This is less of a pivot than it sounds. The current TASKS.md roadmap already grows toward this shape: Phase 5 PaaS for agents, Phase 6 Polaris (project orchestrator), Phases 10 and 11 GitHub integration and constellation dispatch, plus the existing team of bots (argus, vega, vela, spica) on the worker. The constellation is the "AI coworker product" in embryo.

Three "better than Viktor" wedges to design toward:

1. **Trust-zone architecture as a default, not an upgrade.** Viktor's own FAQ admits Private Mode, RBAC, per-user token scoping, and sensitive-data handling are unbuilt and on the roadmap. Their workspace-wide shared-context model rules them out of any use case where PII can't be co-mingled across users. The multi-bot trust-zone work captured in the section above is the direct counter-positioning. Buyers whose compliance team blocks Viktor are the wedge.

2. **Chat-first surface (Slack/Teams native).** Viktor's product surface is a chat client, not a web dashboard. The current Lightsei plan has no equivalent. If "AI coworker your team talks to" is the product, this needs to land somewhere on the long-term map.

3. **Integration breadth.** Viktor advertises 3000+ tool connections. The current Lightsei plan addresses zero of these directly. The work would mostly be wrapping MCPs and prebuilt connectors; not novel, but it's volume, and Viktor is using it as a moat in their marketing.

**Strategic direction decided 2026-05-17:** Reading (b), with a configure-your-team twist. Lightsei is the AI coworker product. Non-technical users are the primary buyer. Instead of Viktor's one-generalist-coworker model, the user assembles a team of specialized bots through the dashboard (and eventually a chat client surface). The SDK, backend, observability, and guardrails are internal infrastructure that powers and protects the team; most customers never see them directly. If a developer-shaped customer ever surfaces who wants the platform directly, that's a (a)-shaped second product down the road, not the main thing.

What this sharpens:

1. **Dashboard is the primary surface.** Eventually plus chat (Slack or Teams). SDK stays internal. Dashboard polish and IA matter more than SDK ergonomics.
2. **No-code end to end.** Today `/agents/new` accepts a Python zip; the long-term path is "describe your team in plain English, get bots." Polaris-style generation is the seed. Phase 12C ("drop a README, get a team") extends it.
3. **Pricing is per-seat or per-team, not per-event.** Viktor's $50/mo/seat is the closer reference; Sentry/Datadog usage-based pricing does not fit a non-technical buyer's mental model. Affects backend metering work later.
4. **Trust-zone work is now P0, not nice-to-have.** Non-technical users won't configure isolation correctly on their own. The platform has to ship sensible presets ("this team can see customer data, this one can't, this one can talk to the internet"). Re-read the "Target customer shape" section above with this in mind.
5. **Integration breadth becomes a real moat question.** Non-technical users expect Slack, Gmail, Stripe, HubSpot, Notion, Linear, Google Drive to "just work." Wrapping MCPs is the practical path; needs to land somewhere on the long-term map.

None of this changes the current phase. Parking Lot entry mirrors this for the task list.

## Phase 10.6 demo marker (2026-05-01)

This line was appended via a git push to verify that Polaris's next tick reads MEMORY.md from GitHub instead of from the bundled disk copy. The bot was redeployed earlier today with `POLARIS_GITHUB_REPO=bewallace01/lightsei` set; if the next plan reflects this marker, the GitHub-fetch path is wired correctly. Cleanup: leave the line in place until Phase 10.6 is in the Done Log, then either remove it or fold it into the demo write-up.

Second push (after reconnecting the GitHub webhook with a fresh secret): if this line lands and github.com Recent Deliveries shows a 200, the webhook signature path is healthy end-to-end. The push touched only MEMORY.md so no redeploy should be queued; the /github panel's "Recent push-triggered deploys" stays empty, which is correct.
