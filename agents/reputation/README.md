# Reputation assistant

Part of the AI Business Team. Watches what customers say: analyzes
incoming reviews, flags negative feedback fast so the owner can respond,
and suggests how to respond. Polls `reputation.check`, emits
`reputation.analyzed`, and alerts via Hermes on negative reviews.
Heuristic sentiment (rating + keywords), no LLM.

## Command

`reputation.check` payload (the review is the payload, or under `review`):

```json
{ "author": "Sam", "rating": 2, "source": "Google",
  "text": "Slow service and the staff were rude." }
```

## Sentiment

Rating is the primary signal (<=2 negative, 3 neutral, >=4 positive),
refined by negative/positive keyword counts in the text.

## Events

- `reputation.analyzed` — `{author, source, rating, sentiment, score,
  reasons, response_hint, severity}`
- `reputation.crash`

## Dispatch

`hermes.post` only on **negative** reviews (`source_agent="reputation"`).
Positive/neutral stay silent.

## Env

| Var | Default | Meaning |
| --- | --- | --- |
| `LIGHTSEI_API_KEY` | (required) | auth |
| `LIGHTSEI_BASE_URL` | `https://api.lightsei.com` | backend |
| `LIGHTSEI_AGENT_NAME` | `reputation` | agent identity |
| `REPUTATION_POLL_S` | `5` | seconds between claim attempts |
| `REPUTATION_HERMES_CHANNEL` | `default` | channel passed to Hermes |

## Run

```
LIGHTSEI_API_KEY=... python bot.py
```
