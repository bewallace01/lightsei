# Vega — PR reviewer

Phase 13.2. The constellation's reviewer. Vega polls its command queue
for `vega.review` commands, runs a heuristic pass over a unified diff,
and emits a structured `vega.review_complete` event. When it has
comments, it dispatches a `hermes.post` summary.

Vega is a heuristic reviewer (not an LLM one): cheap, deterministic, and
never wrong about whether a `print(` is still in the patch.

## Command

`vega.review` payload:

```json
{ "diff": "<unified diff>", "commit": "<sha>" }
```

(`patch` is accepted as an alias for `diff`.)

## Events

- `vega.review_complete` — `{added_lines, comments_count, high_severity_count,
  comments: [{severity, file, line, message}], severity}`
- `vega.crash` — the reviewer raised.

## Dispatch

`hermes.post` one-line verdict whenever there is at least one comment
(`source_agent="vega"`). A clean diff stays silent.

## What it flags

- **high** — `eval`/`exec`, bare/swallowed exceptions, skipped or focused tests
- **medium** — debug statements left in (`print`, `console.log`, `debugger`, `breakpoint`, `pdb.set_trace`)
- **low** — leftover `TODO`/`FIXME`/`XXX`/`HACK`, a source change with no test change, an oversized diff

## Env

| Var | Default | Meaning |
| --- | --- | --- |
| `LIGHTSEI_API_KEY` | (required) | auth |
| `LIGHTSEI_BASE_URL` | `https://api.lightsei.com` | backend |
| `LIGHTSEI_AGENT_NAME` | `vega` | agent identity |
| `VEGA_POLL_S` | `5` | seconds between claim attempts |
| `VEGA_HERMES_CHANNEL` | `default` | channel passed to Hermes |
| `VEGA_LARGE_DIFF` | `400` | added-line count that trips the large-diff note |

## Run

```
LIGHTSEI_API_KEY=... python bot.py
```
