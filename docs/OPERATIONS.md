# Operations runbook

How Lightsei runs in production, and what to check when something breaks.
This is the doc you want when the dashboard works but the team won't come
online.

## Production topology

Everything runs on **Railway**, in one project, as separate services:

| Service | What it is | Public URL | Start command |
|---|---|---|---|
| **backend** | FastAPI app (`backend/main.py`) | `https://api.lightsei.com` | `uvicorn main:app` |
| **dashboard** | Next.js app (`dashboard/`) | `https://app.lightsei.com` | `next start` |
| **worker** | Bot runtime (`worker/runner.py`) | none (no inbound) | `python runner.py` (see `worker/Procfile`) |
| **Postgres** | The database | internal | — |

The three app services are **independent processes**. The dashboard talks
to the backend over HTTPS; the worker talks to the backend over HTTPS; the
backend talks to Postgres. A bot deployed by the worker also calls the
backend (heartbeat, emit events, claim commands).

### How deploys happen

- **Merge to `main` → Railway auto-deploys** the affected service(s). There
  is no deploy workflow in `.github/workflows` (CI there only runs tests);
  Railway watches the repo.
- **Database migrations apply automatically** on backend startup. The very
  first thing `main.py`'s startup hook does is `upgrade_to_head()`
  (`backend/main.py`, `@app.on_event("startup")`). A failed migration
  crashes boot, so if `GET /health` is up, the schema is at head.
- The backend startup hook also launches: the in-process **jobs runner**,
  the **eval cron**, and the **feeder/trigger scheduler** (which is what
  makes the proactive feeders run). So the feeders run inside the backend
  process, not the worker.

## The worker (the part that bites)

The worker is what turns a *queued deployment* into a *running bot*. It:

1. Polls `POST /worker/deployments/claim` every ~5s, claiming the oldest
   eligible deployment (`FOR UPDATE SKIP LOCKED`).
2. Fetches the bundle blob, unzips it, builds a per-deployment venv, `pip
   install`s its `requirements.txt`, and spawns `bot.py`.
3. Streams logs + heartbeats back. Runs up to `LIGHTSEI_WORKER_MAX_CONCURRENT`
   bots at once (default **4**).

It does **not** care whether a deployment's `source` is `cli` or `builtin`
(onboarding's auto-deploy) — it builds from `source_blob_id` either way.

### Required env vars on the worker service

| Var | Required? | Notes |
|---|---|---|
| `LIGHTSEI_WORKER_TOKEN` | **yes** | Must equal the backend's `LIGHTSEI_WORKER_TOKEN`. Missing → the worker **exits with code 2 on startup** (crash-loop). Mismatched → every claim returns 401. |
| `LIGHTSEI_BASE_URL` | **effectively yes** | Must be `https://api.lightsei.com`. **Defaults to `http://localhost:8000` if unset** — the worker then silently polls localhost and claims nothing while real deployments sit queued. |
| `LIGHTSEI_WORKER_MAX_CONCURRENT` | no | Default 4. Raise it if you run more than 4 bots per workspace and they queue. |

The worker does NOT need the secrets key — it fetches already-decrypted
workspace secrets from the backend (`/worker/workspaces/{id}/secrets`).

### Where to find it in Railway

Railway project → the **worker** service (start command `python
runner.py`, *not* the uvicorn one) → **Deployments → View Logs**.

A healthy worker logs this on boot:

```
lightsei worker <uuid> starting; base_url=https://api.lightsei.com scratch=...
```

## Troubleshooting

### Deployments stuck `queued`, heartbeat `—`, for a long time

(Seen on Advanced → Deployments, or `/deployments`.) The team panel shows
everything "starting…" and nothing goes green.

**Meaning:** the worker is not claiming jobs. The app side is fine — the
deployments are correctly queued. `heartbeat = —` means no worker has ever
touched them.

**Diagnose, in order:**

1. **Is the worker service running?** If it's stopped/crashed/removed in
   Railway, that's the whole answer. Start/redeploy it.
2. **Read its boot log** (above). If `base_url=http://localhost:8000` →
   set `LIGHTSEI_BASE_URL=https://api.lightsei.com` and redeploy. This is
   the most common silent failure.
3. **`LIGHTSEI_WORKER_TOKEN must be set`** in the logs, restarting in a
   loop → set the token (matching the backend) and redeploy.
4. **401s on `/worker/deployments/claim`** → token mismatch between worker
   and backend.
5. **Stuck `building` (not `queued`)** with no error → a `pip install` is
   hanging or slow; check the deployment's logs (Deployments → click the
   row). An actual failure surfaces as status `error` with the message.
6. **At capacity** → if `LIGHTSEI_WORKER_MAX_CONCURRENT` bots are already
   running (e.g. a demo workspace's bots holding all the slots), new ones
   queue. Stop unused deployments or raise the cap.

### LLM assistants deployed but crashing

The LLM-backed personas (`bi`/Altair, `marketing`/Nova, `inbox`/Mira) need
`ANTHROPIC_API_KEY` set as a **workspace secret** (Account → secrets) with
credits. Without it they run but emit `*.crash` and complete the command
with an error. The heuristic ones (`website`/Rigel, `lead`/Orion,
`reputation`/Lyra) don't need a key.

### Connectors return 503

Google OAuth (`/auth/google/start`, the Gmail / Google Business connectors)
returns 503 when `LIGHTSEI_GOOGLE_CLIENT_ID` / `LIGHTSEI_GOOGLE_CLIENT_SECRET`
aren't set on the backend. Set them to enable Google sign-in + connectors.

### Quick health probes (read-only, safe)

```bash
curl https://api.lightsei.com/health                 # backend up + DB pool
curl -o /dev/null -w "%{http_code}\n" https://app.lightsei.com/   # dashboard
```

There is **no public health endpoint for the worker** — you can only tell
it's healthy by (a) its Railway logs and (b) deployments transitioning
`queued → building → running` with live heartbeats.
