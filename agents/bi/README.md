# Business-Intelligence assistant

Part of the AI Business Team. The analyst: turns the business's data into
a plain-English read, a weekly summary (what happened, trends,
opportunities, risks) or a direct answer to a question ("How many leads
this week?", "Why did traffic drop?"). Polls `bi.summarize`, asks Claude
to analyze the supplied data, emits `bi.summary`, and notifies the owner
via Hermes.

LLM-backed, same pattern as the marketing assistant. The data comes in
the command payload (a digest job / the dashboard / another assistant
gathers it); BI is the analyst that reads it.

## Command

`bi.summarize` payload:

```json
{ "period": "this week",
  "question": "How many leads came in?",
  "data": { "leads": 12, "hot": 3, "reviews": {"positive": 8, "negative": 1} } }
```

`question` optional — omit it for the weekly summary. `data` can be a
JSON object or a string.

## Events

- `bi.summary` — `{kind: summary|answer, summary, input_tokens, output_tokens, model, severity}`
- `bi.crash` — generation failed or no key.

## Dispatch

`hermes.post` note on success (`source_agent="bi"`).

## Env

| Var | Default | Meaning |
| --- | --- | --- |
| `LIGHTSEI_API_KEY` | (required) | bot auth |
| `ANTHROPIC_API_KEY` | (required for analysis) | the workspace's Claude key |
| `LIGHTSEI_BASE_URL` | `https://api.lightsei.com` | backend |
| `LIGHTSEI_AGENT_NAME` | `bi` | agent identity |
| `BI_POLL_S` | `5` | seconds between claim attempts |
| `BI_HERMES_CHANNEL` | `default` | channel passed to Hermes |
| `BI_MODEL` | `claude-sonnet-4-6` | Claude model |
| `BI_MAX_TOKENS` | `900` | output cap |

## Run

```
LIGHTSEI_API_KEY=... ANTHROPIC_API_KEY=... python bot.py
```
