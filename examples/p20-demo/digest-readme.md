# Halo

Halo is a small SaaS company. We're 12 people on a hybrid schedule, with engineering, design, customer success, and ops sharing the same Slack workspace.

## What we do

Halo sells a build-monitoring product to engineering teams at mid-sized SaaS companies. Customers point our agent at their CI, we surface flaky tests + slow stages + cost regressions, and route fixes to the right team.

## How the team works

We run on:

- **Slack**. Most internal coordination, decisions, async standup, and customer-facing shared channels.
- **Gmail** (Google Workspace). Customer email, invoicing, vendor mail, account-management.
- **Google Calendar**. Team meetings, customer kickoffs, on-call handoffs, all-hands.
- **Google Drive**. Working docs, design files, contracts, customer-facing reports. Drive is also where weekly customer reports get drafted.

There's no formal "morning briefing" — folks open Slack, scan their inbox, glance at the day's calendar, and try to figure out what changed in the team's shared Drive. The signal is in three places, and people miss things.

## Where we want bots

### 1. Weekly digest (the bot this README is for)

Once a week, drop a digest into the team's `#weekly-pulse` Slack channel that pulls:

- Upcoming events in the next 7 days from the team's shared Google Calendar (meetings, customer touchpoints, on-call rotations).
- Unread email in our shared `team@halo.dev` Gmail inbox (max 10) — surfacing who is waiting on a reply.
- Files modified in the team's working Google Drive folder in the last 7 days (max 20) — surfacing what work is in flight.

Format the result as a short, readable Slack message. No analysis or recommendations — just the surfaces, so the team has the same picture at the same time.

Sensitivity: this bot touches employee email, internal calendar, internal docs. Internal at minimum — not public, but not customer-PII either. The bot lives in the `internal` zone and gets `connector:gmail`, `connector:google_calendar`, `connector:google_drive`, and `slack:respond` capabilities. No `internet` capability — the bot has no reason to call external APIs.

### 2. (Not for this README, future bot.)

Other bots we might build later: customer-report drafter (uses Drive), CSM-side at-risk-account digest (uses HubSpot via a future connector). Out of scope today.

## How the digest runs

The bot is a daemon (the Lightsei worker keeps it alive). It registers a single command handler — `weekly_digest.run` — that an internal scheduler or operator-initiated request triggers once a week. Output goes to one Slack channel; failure mode is "logs the error, posts nothing." No retries — if Calendar / Gmail / Drive is down, next week's digest fills in.

## What's off-limits

This is a `internal`-zone bot, so:

- No outbound internet calls.
- No dispatching to bots in other trust zones (the SDK refuses the call).
- No reading customer PII out of HubSpot — that data lives in a different SOC 2 boundary and a different zone (`pii`).

If someone wants to add customer data to the digest later, that's a separate bot, in the `pii` zone, with a separate operator-driven dispatch path. The trust zone is the wedge here: the digest bot can never accidentally leak customer data because the framework refuses the cross-zone call.
