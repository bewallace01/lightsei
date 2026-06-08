# Website assistant

Part of the AI Business Team. The Website assistant keeps a small
business's site healthy: is it up, are there broken links a customer
might hit, is the contact / lead-capture form still on the page? It polls
for `website.check` commands, runs the checks, emits a structured event,
and alerts the owner via Hermes only when something is wrong.

No LLM, no connectors — pure HTTP checks.

## Command

`website.check` payload:

```json
{ "url": "https://example.com" }
```

## Events

- `website.check_complete` — `{url, up, status_code, latency_ms,
  broken_links: [{url, status}], forms_found, links_checked, severity}`
- `website.crash`

## Dispatch

`hermes.post` only on a **down site** or **broken links**
(`source_agent="website"`). A healthy site stays silent.

## Env

| Var | Default | Meaning |
| --- | --- | --- |
| `LIGHTSEI_API_KEY` | (required) | auth |
| `LIGHTSEI_BASE_URL` | `https://api.lightsei.com` | backend |
| `LIGHTSEI_AGENT_NAME` | `website` | agent identity |
| `WEBSITE_POLL_S` | `5` | seconds between claim attempts |
| `WEBSITE_HERMES_CHANNEL` | `default` | channel passed to Hermes |
| `WEBSITE_MAX_LINKS` | `25` | cap on links probed per check |
| `WEBSITE_TIMEOUT_S` | `10` | per-request timeout |

## Run

```
LIGHTSEI_API_KEY=... python bot.py
```
