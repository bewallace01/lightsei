# Phase 22 — scheduled bots + webhook triggers demo

End-to-end demo for Phase 22. An operator on an internal ops team
deploys "Morning Briefing" (the bot described in
`daily-digest-readme.md`), wires a cron trigger to fire it every
weekday at 9am, and adds a webhook trigger for on-demand runs.

Pairs with the Phase 16 wedge demo (`examples/p16-demo/`), the
Phase 20 connector demo (`examples/p20-demo/`), and the Phase 21
widget demo (`examples/p21-demo/`). Together: 16 proves trust
zones stop leaks, 20 proves bots can do real work via connectors,
21 proves the same machinery extends to customer-facing surfaces,
22 makes Lightsei proactive — bots can run on their own schedule
or be fired by external systems.

## What you'll need

- A Lightsei workspace on prod. Sign up at https://app.lightsei.com.
- An Anthropic API key set as the workspace secret `ANTHROPIC_API_KEY`
  (used by team-from-readme to generate the bot's plan).
- Python 3.11+ on your laptop with the Lightsei SDK installed
  (`pip install -e ./sdk`).
- ~10 minutes for the demo. Anthropic cost: ~$0.30 for the
  team-from-readme generation. The bot itself doesn't call an LLM in
  this demo (it emits a fake digest); a real bot would.
- Optional: configured Gmail + Calendar connectors via
  `/integrations` if you want to swap `_build_digest` for the real
  Phase 20 connector calls.

## Files in this directory

- `daily-digest-readme.md` — what an operator drops into
  team-from-readme to generate the bot.
- `digest_bot.py` — the deployable bot. Uses `@lightsei.on_trigger`
  + reads `lightsei.trigger.kind` / `scheduled_at` / `webhook_payload`.
  ~130 lines.
- `README.md` — this file.

## Demo flow (5 acts)

### Act 1 — deploy the bot

Open `/agents/team-from-readme` in the dashboard. Paste the contents
of `daily-digest-readme.md` into the textarea. Click **Plan a team**.

The planner returns a bot named after a star (probably "vega" or
"orion"), `pii` zone, with `connector:gmail` + `connector:google_calendar`
+ `slack:respond` capabilities pre-granted. Click **Generate code**.
Review. Click **Deploy**.

Once deployed, the bot's process is running in the Lightsei worker;
it's polling for commands. (You can also deploy `digest_bot.py` directly
via the SDK or `/agents/new` — name the agent whatever you want, set
sensitivity to `pii`, grant the same capabilities.)

In another terminal, run the bot locally if you deployed `digest_bot.py`:

```bash
export LIGHTSEI_API_KEY=<your workspace api key>
export LIGHTSEI_AGENT_NAME=morning-briefing
python examples/p22-demo/digest_bot.py
```

The bot prints `[digest_bot] registered as 'morning-briefing'; waiting
for trigger fires.` and idles.

### Act 2 — set a cron trigger

Navigate to the agent's detail page (`/agents/morning-briefing` or
whatever name your bot took). Scroll to the **Triggers** section.

Click **+ New trigger**. In the modal:

- Leave the **Cron** tab selected.
- Name: `weekday 9am`.
- Click the **Weekdays at 9am** preset card. The cron expression
  `0 9 * * 1-5` appears beneath it, and a "Next fires:" preview shows
  the next three Monday-through-Friday 9am datetimes.
- Click **Create trigger**.

The trigger lands at the top of the list with a violet `cron` badge
and a `dispatched` status pill once a fire has happened (initially no
pill — the trigger hasn't fired yet).

### Act 3 — fast-forward + watch it fire

Waiting for the actual 9am tomorrow makes for a slow demo. Two options:

**Option A (fast):** in your Postgres console, set the trigger's
`next_run_at` to a minute from now:

```sql
UPDATE triggers
   SET next_run_at = NOW() + INTERVAL '60 seconds'
 WHERE name = 'weekday 9am';
```

Within ~120 seconds (the scheduler's 60s tick + ~60s of slack) the
trigger fires. Tail the bot's stdout — you'll see:

```
[digest_bot] fired by trigger='weekday 9am' kind=cron
[digest_bot]   scheduled_at=2026-05-24 09:00:00+00:00
[digest_bot] digest body:
# Morning briefing for Monday May 24

## Calendar
- 10:00 - 10:30 - 1:1 with manager
...
```

**Option B (real):** wait. The scheduler logs `scheduler: tick fired 1
trigger(s)` in the backend log when it dispatches.

Open `/runs`. The latest row is the digest run, with a small violet
`cron` badge under the agent name (hover for the trigger name). The
agent detail page's Triggers panel now shows the trigger's
`last_run_status` as `succeeded`.

### Act 4 — add a webhook trigger

Back on the agent detail page, click **+ New trigger** again. Switch
to the **Webhook** tab.

- Name: `on demand`.
- Click **Create trigger**.

The modal flips to a token-reveal screen: a long URL-safe string, a
copyable `curl` example, and an "I've copied it" dismiss button. Copy
the token now — Lightsei only stores the sha256 hash; the plaintext
is gone after you dismiss the modal.

Click **I've copied it**. The trigger lands in the list with a sky-blue
`webhook` badge (next to the existing cron trigger).

### Act 5 — fire the webhook + check /runs filter

From your shell:

```bash
curl -X POST https://api.lightsei.com/triggers/<token>/fire \
  -H "content-type: application/json" \
  -d '{"reason": "after-vacation catch-up"}'
```

Response is immediate:

```json
{"run_id":"3e2a...","status":"queued","trigger_id":"..."}
```

The bot's stdout shows:

```
[digest_bot] fired by trigger='on demand' kind=webhook
[digest_bot]   webhook payload reason='after-vacation catch-up'
```

Open `/runs`. The newest row is the webhook-fired run, with a sky-blue
`webhook` badge (hover for the trigger name).

Click the badge — the page filters to `/runs?trigger_id=<id>`. The
banner at the top reads `Filtered to trigger: on demand`. Only the
webhook-fired runs are visible. Click **Clear filter** to go back to
the full list.

## What this demo proves

- **Lightsei's product surface is no longer reactive-only.** Until
  Phase 22, every bot run was kicked off by a human action (Slack
  mention, widget message, manual deploy button, CLI invocation). The
  cron trigger fires Morning Briefing on its own at 9am; the webhook
  trigger lets external systems initiate runs without any Lightsei
  client. The integration story now extends both directions: Phase 20
  brought external tools into the bot's reach; Phase 22 lets external
  systems initiate bot work.
- **The trust-zone + capability model still holds.** Morning Briefing
  is `pii`-zoned with explicit `connector:gmail` + `connector:google_calendar`
  + `slack:respond` capabilities. A scheduled trigger fires the bot
  through the exact same dispatch + capability + zone gates as a
  manual run — there's no separate "trigger bypass" path. A `public`
  bot with no Gmail capability can't be triggered into reading email
  just because the trigger's webhook payload asked nicely.
- **Triggers are observable.** Every fire is a real Run row in /runs
  with a trigger badge. The trigger panel on the agent detail page
  shows the most recent status. A trigger that points at a missing
  bot flips to `agent_missing` instead of silently failing.

## Cleanup

Delete the triggers from the agent detail page (Delete button on each
row). Past runs stay in /runs with the badge preserved via the
`trigger_kind` snapshot — the FK is SET NULL on trigger delete so
history doesn't disappear with the trigger.

If you want to remove the deployed bot too, click **Delete agent** at
the top of the agent detail page.

## Parked for Phase 22B

- Event-based triggers (Gmail label applied, Drive change, Calendar
  event tagged) — each event source needs its own polling or webhook
  integration; the connector pattern from Phase 20 will extend here
  cleanly.
- Chained triggers ("when bot A finishes successfully, fire bot B").
- Conditional triggers ("only fire if last run's output contained X").
- Trigger run-history page (today, history is just `/runs?trigger_id=`).
- Trigger templates ("hourly inbox scrub" prebuilt for common bot
  types, similar to team-from-readme presets).
- Webhook HMAC signing (beyond the URL-token model — GitHub-style
  signed bodies).
