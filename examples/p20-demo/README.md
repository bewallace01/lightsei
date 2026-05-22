# Phase 20 — weekly-digest bot demo

End-to-end demo for Phase 20 (connector breadth). A non-technical operator connects Gmail, Google Calendar, and Google Drive via OAuth, drops a small README into team-from-README, deploys the resulting weekly-digest bot, and watches the bot pull events + unread mail + recent docs into a single Slack message.

Pairs with the Phase 16 wedge demo: that one proves trust zones stop data leaks, this one proves the same trust-zone machinery doesn't get in the way when bots are configured the right way. Both demos shippable as customer-facing material.

## What you'll need

- A Lightsei workspace on prod (paid plan or free credits remaining). Sign up at https://app.lightsei.com if you don't have one.
- An Anthropic API key set as the workspace secret `ANTHROPIC_API_KEY` on `/account` (used by team-from-README to generate the bot).
- Google account access for Gmail / Calendar / Drive — same account works for all three, single OAuth flow per connector.
- A Slack workspace connected to Lightsei (Phase 19 install). If you haven't connected Slack, do that from `/integrations/slack` first.
- A Slack channel id where the digest will post (e.g. `#weekly-pulse`).
- Python 3.11+ on your laptop with the Lightsei SDK installed (`pip install -e ./sdk`).
- ~10 minutes for the demo, plus ~$0.20-$0.50 of Anthropic cost for team-from-README generation + a few cents per bot run.

## Demo flow (5 acts)

### Act 1 — connect the three Google services

Open `/integrations` in the dashboard. You should see four cards: Slack (connected from earlier), and Gmail / Google Calendar / Google Drive (each showing "Not connected").

For each Google card, click **Connect**. The browser redirects to Google's consent screen. Sign in (or pick the right account), review the requested scopes, click **Allow**. The browser comes back to `/integrations?installed=<type>` with a green flash. The card flips to "Connected as you@example.com".

Repeat for all three. Single Google account, three single-click OAuth flows. Total: ~60 seconds.

### Act 2 — drop the digest README

Navigate to `/agents/team-from-readme`.

Open `examples/p20-demo/digest-readme.md` in your editor. Copy the whole file. Paste into the README text area. Click **Plan a team**.

After ~10 seconds the planner returns a proposed team. You should see one bot whose summary mentions "weekly digest", "Slack", and the three Google services. Name will be something star-themed (Polaris / Vega / Antares, etc.). Role: `specialist`.

If the planner proposes additional bots (an orchestrator, a separate "Drive watcher") rename or remove them — for this demo we only want the single digest bot.

### Act 3 — deploy + verify capabilities

Click **Generate code** on the planned bot. Review (it should look very similar to `digest_bot.py` here, modulo the bot name). Click **Deploy**.

While the bot deploys, open the bot's page (`/agents/<name>`). Confirm:

- Sensitivity zone: `internal` (a chip on the header).
- Capabilities: `connector:gmail`, `connector:google_calendar`, `connector:google_drive`, `slack:respond`. NO `internet` — the bot has no business hitting external APIs.

If `slack:respond` is missing, the Compliance preset's internal hint mapping (Phase 19.5) should have granted it; surface the omission as a bug rather than fixing it inline.

### Act 4 — fire the digest

The deployed bot registers a `weekly_digest.run` command handler. Trigger one manually:

```bash
curl -X POST https://api.lightsei.com/agents/<bot-name>/commands \
  -H "Authorization: Bearer $LIGHTSEI_API_KEY" \
  -H "content-type: application/json" \
  -d '{"kind": "weekly_digest.run", "payload": {}}'
```

Within a few seconds, the bot picks up the command, calls Calendar / Gmail / Drive in parallel, formats the digest, and posts to your `#weekly-pulse` channel.

Open Slack. The message should land — with the next 7 days of events, your unread mail (top 10), and files modified in Drive this week.

Variants worth showing:

- Open `/runs` and find the run that just fired. It should have one event per connector call (`connector_call_completed`) plus the post-to-Slack. Zero Anthropic cost.
- Drop your laptop offline, fire the command again, watch the bot post a digest where each section says `_couldn't fetch X_`. The bot doesn't crash — graceful degradation, per CLAUDE.md.

### Act 5 — prove the zone gate

This is the wedge. The digest bot was allowed to use Gmail because it lives in `internal`. A `public`-zoned bot would be refused even with the same capability granted.

Create a second bot via the dashboard (`/agents → New agent`):

- Name: `researcher`
- Role: `specialist`
- Sensitivity: `public`
- Capabilities: `connector:gmail` (intentionally — pretend an operator misconfigured)

Then run:

```bash
export LIGHTSEI_API_KEY=...
export LIGHTSEI_AGENT_NAME=researcher
python examples/p20-demo/zone_enforcement_demo.py
```

Expected output ends with:

```
[result] BLOCKED — LightseiConnectorZoneError raised
[details] connector 'gmail' refused calls from 'public'-zoned bots.
          Declared zones: ['internal', 'pii', 'sensitive'].
```

The capability check would have let the call through — the operator granted `connector:gmail`. The trust zone is what saves the workspace from leak. Customer-data connectors are out of reach for public-zoned bots by registry construction, not by operator vigilance.

## Cleanup

Delete the digest bot from `/agents/<bot-name>` (the Stop + Delete buttons). Delete the `researcher` bot the same way. Disconnect Gmail / Calendar / Drive from `/integrations` if you don't want them connected to your workspace anymore — Disconnect calls Google's `/revoke` so the upstream refresh token is invalidated.

## Files

- `digest-readme.md` — what an operator drops into team-from-README.
- `digest_bot.py` — the bot that gets generated + deployed.
- `zone_enforcement_demo.py` — proves the wedge end-to-end without Anthropic spend.
- `README.md` — this file.

## Notes

- The SDK signatures in `digest_bot.py` are the real Phase 20.7 surface (`time_min=`, `max_results=`, `query=` etc). The original Phase 20.10 spec sketched some different kwargs; the implementation differs.
- The digest bot has no `internet` capability — every external call goes through the connector endpoint, which is the right enforcement boundary. A bot that needs raw `httpx.get(...)` is a different design.
- This demo is shippable as a customer-facing video / blog. The Phase 16 demo + this demo together cover the two big claims: "your data can't leak by accident" + "operators can wire up real work without writing code".
