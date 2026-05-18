# Phase 16 Compliance Team demo

End-to-end demo that proves the trust-zone wedge: a non-technical user
drops a CRM-shaped README, picks the Compliance preset, and ships a
team where PII data physically cannot leak to the internet-facing
bot via the framework. The only way customer data crosses the zone
boundary is a human operator pressing keys.

This is the demo you show prospects who ask "what stops your AI
agents from leaking our customer data?"

## What you'll need

- A Lightsei workspace on prod (paid plan or free credits remaining).
  Sign up at https://app.lightsei.com if you don't have one.
- An Anthropic API key set as the workspace secret `ANTHROPIC_API_KEY`
  on `/account`.
- Python 3.11+ on your laptop with the Lightsei SDK installed
  (`pip install -e ./sdk httpx`).
- ~5-10 minutes for the demo, plus ~$0.20-$0.50 of LLM cost for the
  planner + the bot runs.

## Demo flow (6 acts)

The demo runs in three layers: dashboard browser flow, deployed bots
on the prod worker, and three Python scripts you run from your laptop
to demonstrate SDK-level enforcement.

### Act 1 — drop the README

Sign in to https://app.lightsei.com and navigate to
**/agents/team-from-readme**.

Open `examples/p16-demo/crm-readme.md` in your editor, copy the whole
file, paste into the README text area. Click **Plan a team**.

After ~10 seconds the planner returns a proposed team. You should see
two bots (plus optionally an orchestrator):

- A CRM-side bot whose summary mentions HubSpot, at-risk accounts,
  Slack outreach — i.e. the workflow inside Coral's SOC 2 boundary.
- A research-side bot whose summary mentions LinkedIn, Crunchbase,
  prospect lists — i.e. the public-side workflow.

If the names don't include "crm" and "research" explicitly, that's
fine. Skip ahead — rename them in the next step.

### Act 2 — pick the Compliance preset + deploy

Below the proposed team, the preset picker shows three options. Click
**Compliance team** (description: "Your customer data does not leave
the team").

The preview panel updates to show one row per sensitivity hint
(P16.x — the planner labels each bot with a `sensitivity_hint` and
the deploy uses it to land each bot in the right zone):

- `pii`: pii zone, NO capabilities, no cross-zone dispatch.
- `sensitive`: sensitive zone, NO capabilities, no cross-zone dispatch.
- `internal`: internal zone, `send_command + internet`, no cross-zone.
- `public`: public zone, `internet`, no cross-zone.

Click **Deploy team**. The Compliance preset picks the right zone per
bot automatically based on what the planner inferred from the README.
For the Coral README you should end up with something like:

- PII chain: orchestrator (`internal`) → CRM specialists (`pii`, no caps) → notifier (`internal`)
- Public chain: research bots (`public`, internet)
- No cross-zone edges anywhere.

(If you want different names from what the planner picked, rename them
via **/agents/{name}** edit before deploying. The demo scripts below
assume `coral-crm-bot` and `coral-research-bot` — you may want to
rename the PII-side specialist and one public-side bot to match.)

### Act 3 — visit /zones, see the topology

Navigate to **/zones**. You should see two vertical lanes (PII /
Public) with each bot in its lane. There should be NO edges between
the lanes; the "dispatches across zones" section should be empty.

This is the wedge made visual. A prospect can look at this page and
see that their CRM data lives in an isolated bucket with no path out.

### Act 4 — try a forbidden internet call from the CRM bot

From your laptop:

    export LIGHTSEI_API_KEY=bk_...    # workspace api key from /account
    export LIGHTSEI_API_URL=https://api.lightsei.com
    python examples/p16-demo/capability_gate_demo.py

Expected output: `BLOCKED — LightseiCapabilityError raised before the
network call`. The SDK refuses the `httpx.get` to api.linkedin.com
because the CRM bot's capability list is empty.

This is what stops prompt-injected CRM bots from exfiltrating via the
internet. The framework refuses; no developer discipline required.

### Act 5 — try a forbidden cross-zone dispatch

From your laptop:

    python examples/p16-demo/cross_zone_dispatch_demo.py

Expected output: `BLOCKED — LightseiCrossZoneError raised before the
network call`. The CRM bot can't dispatch to the research bot because
the zones differ and `dispatches_cross_zone=False` on the source.

If a prompt injection convinces the CRM bot to forward customer data
"to research" — the framework refuses. The agent can't even craft the
HTTP call.

### Act 6 — human-mediated handoff via lightsei.handoff_span

For this act, trigger one run each of the two bots first (use the
agent detail page's "run now" button, or wait for the cron).

Then from your laptop:

    python examples/p16-demo/handoff_span_demo.py

Expected output: `OK — handoff event written` plus a link to the
run-detail page.

Open the link. The trace view now shows the two runs as a connected
chain — with an explicit `handoff` event in between, carrying the
operator's sanitized prompt and a note explaining the translation.

This is the only way data crosses the zone boundary: a human operator
reads the CRM-side output, decides what's safe to forward, and types
the sanitized prompt to the research bot. The framework respected the
boundary; the trace shows the operator's translation so the chain
reassembles in the dashboard for audit purposes.

## The pitch line

"Your customer data lives in an isolated zone the framework refuses
to let out. Not by convention — by gate. A prompt injection on your
CRM bot literally cannot make a network call. A runaway agent loop
literally cannot dispatch across the boundary. The only way data
crosses zones is a human operator who decides what's safe to forward,
and we log that translation for audit."

## Cleanup

When you're done demoing:
- Delete the deployed bots from `/agents` (or stop their schedules to
  pause spend without losing the wiring).
- The handoff event stays in the trace history (it's audit-relevant).
- The workspace itself can stay; you may want to keep it as a
  demo-only workspace for future pitches.

## Known follow-ups (parked in TASKS.md)

- The cross-zone refusal message is good but could be even more
  specific about which dispatch path got blocked. Minor polish.
