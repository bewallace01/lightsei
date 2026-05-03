# Hermes

Notifier bot. The workspace's mouth.

## What it does

Polls its command queue for `hermes.post`, posts the included text to the named notification channel via Lightsei's existing notifications dispatcher (Phase 9), retries once on transport failure, then emits `hermes.posted` (success) or `hermes.send_failed` (terminal failure).

Hermes does **not** format messages beyond passing the upstream agent's `text` straight through. Atlas builds its own `"✅ atlas: 322 passed"` line before calling `send_command("hermes", "hermes.post", {...})`, and Hermes hands that text to whichever channel was named. Each upstream agent decides what to say; Hermes only decides how it gets there.

## Phase 11.4 scope

| In | Out | Event |
|---|---|---|
| `hermes.post` | _(no downstream dispatch — Hermes is a leaf)_ | `hermes.posted` (success) / `hermes.send_failed` (terminal failure) |

DM-style fan-out to per-user phone numbers / Telegram chat IDs is parking-lot work (the personal-channel notifications phase).

## Configuration

| Env | Default | What it controls |
|---|---|---|
| `HERMES_POLL_S` | `5` | seconds between claim attempts |
| `HERMES_DEFAULT_CHANNEL` | `default` | channel name used when payload omits `channel` |
| `HERMES_RETRY_DELAY_S` | `5` | wait between transport-failure retries |

Workspace secret `LIGHTSEI_API_KEY` is required.

## Retry posture

`classify_outcome(http_status)` reduces the channel's response into one of three branches:

- **ok** (2xx) — delivered, complete the command.
- **retry** (5xx, transport failure / `http_status < 0`) — wait `HERMES_RETRY_DELAY_S` and try once more. Transient blips clear in seconds; persistent ones aren't worth burning more budget.
- **fail** (4xx) — terminal. Don't retry — auth or bad URL needs human action. Emit `hermes.send_failed` with the response summary so the dashboard can surface "your Slack token is invalid" without further dispatches.

The retry happens at most once. The total wall-clock for a doomed message is bounded.

## Deploy

```bash
lightsei deploy ./agents/hermes
```

Or via GitHub push-to-deploy once the workspace's integration maps `agents/hermes/`.

## Local test

```bash
cd backend
pytest tests/test_hermes.py
```

Tests use injected mocks for the `lightsei` client + the dispatcher callable so the test suite never makes real Slack/Discord calls.
