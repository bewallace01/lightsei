# Lightsei — Tasks

Read MEMORY.md first if it's been a while. (Older Done Log entries call the project "Beacon" — that was the working code-name through Phase 4. Same product.)

## NOW

> **Phase 5.1: deployments schema + zip upload**

Phase 5 is committed: PaaS-for-agents. See "Runtime decision (2026-04-27)" in MEMORY.md for the architecture call (build single-host worker now, swap for managed runtime in 5B).

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

### 5.1 Deployments schema + zip upload (NOW)

- Migration creating `deployments` (id, workspace_id, agent_name, status, desired_state, source_blob_id, error, claimed_by, claimed_at, heartbeat_at, started_at, stopped_at, created_at, updated_at) and `deployment_blobs` (id, workspace_id, size_bytes, sha256, data BYTEA, created_at).
- SQLAlchemy models.
- Bump body-size middleware to allow 10 MB on `multipart/form-data` requests (JSON stays at 1 MB).
- Endpoints: `POST /workspaces/me/deployments` (multipart upload), `GET /workspaces/me/deployments`, `GET /workspaces/me/deployments/{id}`, `DELETE /workspaces/me/deployments/{id}`.
- Tests: roundtrip upload, list scoped to workspace, blob > cap → 413, cross-workspace 404.

### 5.2 Worker-facing endpoints
Atomic claim (SKIP LOCKED), status updates, heartbeat-from-worker, log append. Backend only at this stage; no worker process yet.

### 5.3 Worker process
Standalone `worker/runner.py` that polls `/worker/deployments/claim`, downloads the blob, builds a venv, spawns `python bot.py` with workspace secrets injected, manages lifecycle, posts logs. Lifts the shape from `worker/run_local.py`.

### 5.4 Streaming logs
Worker tees stdout/stderr to the backend; dashboard polls a tail endpoint with auto-scroll. Cap at last 1000 lines per deployment for v1.

### 5.5 SDK CLI: `lightsei deploy`
Zips a directory (excluding `__pycache__`, `.venv`, `node_modules`, `.git`), POSTs to the backend, polls until `running` or fails fast.

### 5.6 Dashboard "Deployments" tab
On the agent page, a new tab listing deployments with status pill, started_at, stop/redeploy buttons, and a logs viewer.

### 5.7 Phase 5 demo
End-to-end deploy of a real bot through the CLI, with the worker running on the user's machine pointed at prod. Screenshots in the Done Log.

---

## Phase 6+: TBD

Open candidates for the next phase, set after Phase 5 ships:
- Phase 5B: cut single-host worker over to Fly Machines / Modal sandboxes (gates external users).
- GitHub OAuth + push-to-deploy.
- Buildpacks / Dockerfile support beyond the fixed Python runtime.
- N replicas + cron scheduling.

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
