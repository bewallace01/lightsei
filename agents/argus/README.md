# Argus — security + secret scanner

Phase 13.1. The constellation's watchman. Argus polls its command queue
for `argus.scan` commands, scans the supplied text for hardcoded
secrets, and emits a structured `argus.scan_complete` event. When it
finds a high-severity secret it dispatches a `hermes.post` security
alert so a human hears about it.

Argus never posts to a channel itself, and it never stores a raw
secret: every matched value is masked before it leaves the process.

## Command

`argus.scan` payload (either shape):

```json
{ "text": "<blob to scan>", "path": "config.py", "commit": "<sha>" }
```
```json
{ "files": [{ "path": "config.py", "content": "..." }], "commit": "<sha>" }
```

## Events

- `argus.scan_complete` — every scan. `{files_scanned, findings_count,
  high_severity_count, findings: [{type, severity, line, masked, path}], severity}`
- `argus.crash` — the scanner raised. `{command_id, error, traceback}`

## Dispatch

On any high-severity finding: `hermes.post` with a one-line alert
(`source_agent="argus"`). Medium findings stay in the event only.

## What it catches

AWS access/secret keys, private-key blocks, GitHub / Slack / Stripe /
Anthropic / generic provider (`sk-...`) tokens, and generic
`secret = "..."` assignments (entropy-filtered to drop placeholders).

## Env

| Var | Default | Meaning |
| --- | --- | --- |
| `LIGHTSEI_API_KEY` | (required) | auth |
| `LIGHTSEI_BASE_URL` | `https://api.lightsei.com` | backend |
| `LIGHTSEI_AGENT_NAME` | `argus` | agent identity |
| `ARGUS_POLL_S` | `5` | seconds between claim attempts |
| `ARGUS_HERMES_CHANNEL` | `default` | channel passed to Hermes |

## Run

```
LIGHTSEI_API_KEY=... python bot.py
```
