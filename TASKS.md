# Lightsei — Tasks

Read MEMORY.md first if it's been a while. (Older Done Log entries call the project "Beacon" — that was the working code-name through Phase 4. Same product.)

## NOW

> **Phase 11.7: Phase 11 demo (provision Atlas + Hermes, walk the chain end-to-end on prod)**

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

- **Multiple workspaces per account** (promoted from a 2026-05-01 nav-redesign conversation). Today the data model is one workspace per user — session, API keys, and every workspace-scoped row assumes it. Real multi-workspace requires a `workspace_members` join table (user ↔ workspace + role), an "active workspace" pointer on the session, list/create/switch endpoints, and a UI to drive them via the new header dropdown. The dropdown shell shipped on 2026-05-01 already has a place for "switch workspace" + "+ new workspace" entries. Worth careful design on invites, billing-per-workspace, and what happens to API keys at switch time before starting.
- **Browser-native deploy (drag-drop a folder/zip)**. Today, deploying an agent requires the `lightsei` CLI in a terminal — fine for engineers, a wall for everyone else. The backend's `POST /workspaces/me/deployments` endpoint already accepts a multipart upload (the CLI uses it), so all that's missing is a dashboard surface. Sketch: a `/agents/new` page (or section on `/dispatch`) with a drop zone that accepts a directory or `.zip`, plus a "deploy from GitHub repo path" form that hits a backend endpoint reusing Phase 10.3's GitHub fetch path. Both paths land at the same deployment endpoint. ~half a day each. The drop-zone version is the smoothest UX for non-terminal users; the GitHub-path version means non-engineers can pick from repos they already have without ever leaving the browser.
- **Backend cold-start fix for webhook delivery.** Surfaced during the Phase 10.6 demo on 2026-05-01: github.com's first push-webhook delivery after a period of inactivity timed out at GitHub's 10-second deadline because the Railway backend container had gone to sleep and the cold boot took ~9.6s. Once warm the same endpoint responded in 0.3s, so the code path is fine — the issue is operational. Three viable fixes, pick whichever fits the plan being run: (a) Railway service → Settings → set min instances ≥ 1 (or the equivalent "no sleep" toggle) so the backend stays warm. (b) Add a heartbeat ping from a cheap cron source (cron-job.org, GitHub Actions, even Polaris itself once it's ticking on schedule) hitting `/health` every minute or two — keeps the container warm without paying for an idle instance. (c) Pre-warm by hand before any expected webhook firing, which is the workaround we used during the demo (curl `/health` once, redeliver from github.com's UI). Until one of these lands, prod webhook delivery is timing-fragile: any push after a quiet stretch can silently drop on the floor with no retry.
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
