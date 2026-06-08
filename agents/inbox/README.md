# Inbox assistant

Part of the AI Business Team. Makes the inbox manageable: for each
incoming email it categorizes the message, flags how urgent it is, drafts
a reply, and writes a one-line summary, then alerts the owner only on the
urgent / needs-a-human ones. Polls `inbox.process`, asks Claude to
triage, emits `inbox.processed`, and pings Hermes on urgent mail.

Payload-driven: the email arrives in the command payload. A separate
Gmail-poll feeder (a scheduled connector poll) fetches new mail and
enqueues one `inbox.process` per message; this assistant is the triager.
Same LLM-in-bot pattern as the marketing + BI assistants.

## Command

`inbox.process` payload (the email is the payload, or under `email`):

```json
{ "from": "customer@x.com", "subject": "Refund please",
  "body": "I was charged twice and I'm pretty upset..." }
```

## Triage

Returns `category` (sales/support/billing/spam/personal/other), `urgency`
(high/normal/low), a one-line `summary`, a ready `draft_reply`, and
`needs_human`.

## Events

- `inbox.processed` — `{from, subject, category, urgency, summary,
  draft_reply, needs_human, input_tokens, output_tokens, model, severity}`
- `inbox.crash` — triage failed or no key.

## Dispatch

`hermes.post` only on **urgent** or **needs-human** mail
(`source_agent="inbox"`). Everything else is triaged + drafted silently.

## Env

| Var | Default | Meaning |
| --- | --- | --- |
| `LIGHTSEI_API_KEY` | (required) | bot auth |
| `ANTHROPIC_API_KEY` | (required for triage) | the workspace's Claude key |
| `LIGHTSEI_BASE_URL` | `https://api.lightsei.com` | backend |
| `LIGHTSEI_AGENT_NAME` | `inbox` | agent identity |
| `INBOX_POLL_S` | `5` | seconds between claim attempts |
| `INBOX_HERMES_CHANNEL` | `default` | channel passed to Hermes |
| `INBOX_MODEL` | `claude-sonnet-4-6` | Claude model |
| `INBOX_MAX_TOKENS` | `700` | output cap |

## Run

```
LIGHTSEI_API_KEY=... ANTHROPIC_API_KEY=... python bot.py
```
