# Cassiopeia — incident scribe

Phase 13.4. The constellation's note-taker. Cassiopeia polls its command
queue for `cassiopeia.record` commands, appends each event to a running
incident timeline, and emits a `cassiopeia.timeline_entry` event. On
lifecycle milestones (an incident opening or resolving) it dispatches a
`hermes.post`; the noisy middle accrues silently in the event stream.

## Command

`cassiopeia.record` payload (the event is the payload, or under `event`):

```json
{ "incident_id": "INC-42",
  "event": { "actor": "atlas", "message": "tests failing on main",
             "status": "opened", "at": "2026-06-07T03:00:00Z" } }
```

## Events

- `cassiopeia.timeline_entry` — `{incident_id, entry, entry_count, status,
  milestone, timeline: [..]}`
- `cassiopeia.crash`

## Dispatch

`hermes.post` on `status` in {opened, declared, open, started} (severity
error) and {resolved, closed, mitigated} (severity info),
`source_agent="cassiopeia"`. Other entries stay silent.

## Env

| Var | Default | Meaning |
| --- | --- | --- |
| `LIGHTSEI_API_KEY` | (required) | auth |
| `LIGHTSEI_BASE_URL` | `https://api.lightsei.com` | backend |
| `LIGHTSEI_AGENT_NAME` | `cassiopeia` | agent identity |
| `CASSIOPEIA_POLL_S` | `5` | seconds between claim attempts |
| `CASSIOPEIA_HERMES_CHANNEL` | `default` | channel passed to Hermes |

Timeline state is in-process (single-worker v1); a multi-instance scribe
would persist the timeline in the backend.

## Run

```
LIGHTSEI_API_KEY=... python bot.py
```
