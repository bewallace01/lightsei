# Marketing assistant

Part of the AI Business Team. Turns a plain-English request into ready
marketing content: ad copy, social posts, campaign ideas, a marketing
email. Polls `marketing.create`, asks Claude to write it, emits
`marketing.created`, and tells the owner via Hermes when the draft is
ready.

First LLM-backed persona: calls Claude with the workspace's own
`ANTHROPIC_API_KEY` (injected by the worker). Clean error if the key is
missing.

## Command

`marketing.create` payload:

```json
{ "task": "ad_copy", "topic": "summer 20% off promo",
  "platform": "Facebook", "tone": "fun", "business_context": "a local cafe" }
```

`task` is one of `ad_copy` (default), `social_post`, `campaign_idea`, `email_copy`.

## Events

- `marketing.created` — `{task, content, input_tokens, output_tokens, model, severity}`
- `marketing.crash` — generation failed or no key.

## Dispatch

`hermes.post` "draft ready" note on success (`source_agent="marketing"`).

## Env

| Var | Default | Meaning |
| --- | --- | --- |
| `LIGHTSEI_API_KEY` | (required) | bot auth |
| `ANTHROPIC_API_KEY` | (required for generation) | the workspace's Claude key |
| `LIGHTSEI_BASE_URL` | `https://api.lightsei.com` | backend |
| `LIGHTSEI_AGENT_NAME` | `marketing` | agent identity |
| `MARKETING_POLL_S` | `5` | seconds between claim attempts |
| `MARKETING_HERMES_CHANNEL` | `default` | channel passed to Hermes |
| `MARKETING_MODEL` | `claude-sonnet-4-6` | Claude model |
| `MARKETING_MAX_TOKENS` | `800` | output cap |

## Run

```
LIGHTSEI_API_KEY=... ANTHROPIC_API_KEY=... python bot.py
```
