# Lightsei — Memory

> Project was originally code-named "Beacon" through Phase 4. Renamed to Lightsei (matching lightsei.com) on 2026-04-25. Older Done Log entries still say "Beacon" — those are historical record, leave them alone.

This file is the source of truth for the project. Read it at the start of every session. If something here is wrong or outdated, fix it before doing anything else.

## What we're building

A drop-in observability and guardrail platform for AI agents and bots. The user installs an SDK, adds one line of code, and gets traces, cost tracking, and safety guardrails on their existing agent. No infrastructure to set up.

Think Sentry or Datadog, but for AI agents.

## Why this exists

Production agents need oversight (cost, safety, quality, errors). Existing tools require complex setup or only cover one piece (just traces, just evals, just guardrails). The wedge is making the install ridiculously easy: pip install + one init() call.

## Vision in one line

Easy by default, powerful when you grow into it.

90% of users never go beyond `init()`. The other 10% can drop into custom policies, manifests, and self-hosting.

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

## Reference projects

When stuck on architecture, look at these:
- Langfuse (open source LLM observability)
- Helicone (LLM proxy and observability)
- PostHog (best-in-class plug-and-play SDK experience)
- Sentry (gold standard for graceful degradation and auto-instrumentation)
