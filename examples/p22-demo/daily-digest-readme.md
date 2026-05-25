# Morning Briefing — internal team operations bot

## What it does

Every weekday at 9:00 AM, Morning Briefing reads:

- The owner's Google Calendar for the day ahead (meetings, blocked focus
  time, conflicts).
- Unread Gmail messages labeled "important" since yesterday morning.

It writes a short Markdown digest and posts it to the team's
`#morning-brief` Slack channel.

A team member can also fire Morning Briefing on demand by hitting its
webhook URL with a payload like `{"reason": "after-vacation catch-up"}`.

## Audience

One internal team. Not a customer-facing surface. The bot reads private
data (Gmail, Calendar), so it lives in the **pii** zone with its data
access tightly scoped to those connectors.

## Why a scheduled bot

The data sources are external (Google Workspace) and the trigger
condition is purely time-based ("every weekday morning"). Older Lightsei
surfaces — Slack mentions, widget chats, manual run buttons — all need
a human to initiate. Morning Briefing should run on its own without
anyone asking.

## Trust zone notes

- **Zone:** pii (Gmail + Calendar carry PII).
- **Capabilities required:**
  - `connector:gmail`
  - `connector:google_calendar`
  - `slack:respond`

The trust-zone gate is what makes the scheduled-trigger surface safe:
even though the bot fires on its own with no human in the loop, the
backend still enforces that a `public`-zoned trigger payload can't push
the bot into doing PII work, and the bot still can't reach Drive (zones
the bot doesn't declare).

## Operations

- Operator creates a cron trigger from the agent detail page
  (`Every weekday at 9am` preset).
- Operator can add a webhook trigger alongside the cron, for the
  "after-vacation catch-up" use case.
- Failures (Anthropic timeout, connector auth expired) land on /runs
  with the trigger badge so the operator notices.

## What it does NOT do

- Doesn't take action on the calendar (no event creation, no reschedule
  suggestions).
- Doesn't take action on email (no reply, no archive, no label changes).
- Doesn't aggregate across multiple owners' calendars. v1 is one-bot,
  one-owner; multi-owner is a follow-up.

## Stretch goals (do not include in v1)

- Smart summarization of long email threads ("3 messages about Q3
  budget; latest action item: review proposal by Friday").
- Highlight calendar conflicts ("2pm dentist overlaps with the team
  standup; move dentist?").
- Optional "after-hours quiet mode" that skips runs on weekends + the
  owner's PTO calendar.
