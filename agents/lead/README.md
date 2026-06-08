# Lead-Management assistant

Part of the AI Business Team. When a new lead comes in (from the website
assistant's widget, a form, etc.), this assistant scores its quality,
decides whether it's due for follow-up, and suggests the next action —
then alerts the owner for the leads worth acting on now and stays quiet
on the rest. So no lead falls through the cracks.

Pure scoring + follow-up logic, no LLM.

## Command

`lead.process` payload (the lead is the payload, or under `lead`):

```json
{ "name": "Pat", "email": "pat@co.com", "phone": "555-1212",
  "company": "Co", "message": "interested in a demo, budget ~$2k/mo",
  "status": "new", "last_contact_at": "2026-06-01T10:00:00Z" }
```

## Scoring

0-100 from contactability (email/phone) + qualification (company,
message intent keywords, budget). `hot` >= 70, `warm` 40-69, `cold` < 40.

## Events

- `lead.scored` — `{lead, score, quality, reasons, needs_followup,
  suggested_action, severity}`
- `lead.crash`

## Dispatch

`hermes.post` for **hot/warm leads that are due for follow-up**
(`source_agent="lead"`). Cold leads + not-yet-due leads stay silent.

## Env

| Var | Default | Meaning |
| --- | --- | --- |
| `LIGHTSEI_API_KEY` | (required) | auth |
| `LIGHTSEI_BASE_URL` | `https://api.lightsei.com` | backend |
| `LIGHTSEI_AGENT_NAME` | `lead` | agent identity |
| `LEAD_POLL_S` | `5` | seconds between claim attempts |
| `LEAD_HERMES_CHANNEL` | `default` | channel passed to Hermes |
| `LEAD_FOLLOWUP_HOURS` | `48` | hours since last contact before follow-up is due |

## Run

```
LIGHTSEI_API_KEY=... python bot.py
```
