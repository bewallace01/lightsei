# Sirius — alert triager / on-call

Phase 13.3. The constellation's on-call. Sirius polls its command queue
for `sirius.triage` commands, classifies each alert's severity,
suppresses duplicates seen within a recent window, and dispatches a
`hermes.post` for page/notify actions (silent for log/suppress). Emits a
`sirius.triaged` event for every alert.

## Command

`sirius.triage` payload (the alert is the payload, or under `alert`):

```json
{ "title": "DB unreachable", "source": "prometheus", "severity": "critical",
  "fingerprint": "db-down" }
```

## Severity + action

- Explicit `severity`/`level` wins (critical/error → high, warning → medium, info → low); otherwise inferred from the text.
- `high → page` · `medium → notify` · `low → log` · duplicate-in-window → `suppress`.

## Events

- `sirius.triaged` — `{severity, action, fingerprint, duplicate, reason}`
- `sirius.crash`

## Dispatch

`hermes.post` for `page` (severity error) and `notify` (severity info),
`source_agent="sirius"`. `log` and `suppress` stay silent.

## Dedup

In-process fingerprint window (`SIRIUS_DEDUP_WINDOW_S`, default 300s). A
fingerprint is the explicit `fingerprint`/`dedup_key`, else a hash of
`(source, title)`. Single-worker v1; a multi-instance on-call would move
this to the backend.

## Env

| Var | Default | Meaning |
| --- | --- | --- |
| `LIGHTSEI_API_KEY` | (required) | auth |
| `LIGHTSEI_BASE_URL` | `https://api.lightsei.com` | backend |
| `LIGHTSEI_AGENT_NAME` | `sirius` | agent identity |
| `SIRIUS_POLL_S` | `5` | seconds between claim attempts |
| `SIRIUS_HERMES_CHANNEL` | `default` | channel passed to Hermes |
| `SIRIUS_DEDUP_WINDOW_S` | `300` | suppress repeats of a fingerprint within |

## Run

```
LIGHTSEI_API_KEY=... python bot.py
```
