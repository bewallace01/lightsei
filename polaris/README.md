# Polaris

Polaris is the project orchestrator bot. It reads a Lightsei project's
`MEMORY.md` and `TASKS.md` on a schedule, calls Claude with an orchestrator
prompt, and emits a structured plan: a state summary, three to five next
actions, parking-lot promotion candidates, and any drift it spots between
the docs and the Done Log.

It is itself a Lightsei bot deployed via the Phase 5 PaaS — Lightsei's
first production user is Lightsei. Phase 6A is read-only: Polaris produces
visible plans only, no PRs and no command dispatch. Acting on plans lands
in Phase 6B+ as the missing guardrail layers (output validation, behavioral
rules, continuous eval) come online.

## How it works

1. The bot runs under `@lightsei.track` on a configurable interval
   (`POLARIS_POLL_S`, default 1 hour).
2. Each tick reads `MEMORY.md` + `TASKS.md` from `POLARIS_DOCS_DIR` and
   sha256-hashes both. If both hashes match the last successful tick, it
   emits a lightweight `polaris.tick_skipped` event and skips the LLM
   call. The hashes live in process memory; a redeploy resets them, so
   a fresh deploy always regenerates a plan.
3. On a real tick, the bot calls Claude (`claude-opus-4-7` by default)
   with a forced `submit_plan` tool call. The tool's `strict: true`
   input schema guarantees the response matches the plan structure
   exactly; there is no JSON parser and no parse-error retry path.
4. The plan emits as a `polaris.plan` event carrying the structured
   fields plus doc hashes, model name, and token counts. The
   dashboard's `/polaris` view reads it via
   `GET /agents/polaris/latest-plan`.

## Deploying

From the repo root:

```bash
# 1. Build the wheel
cd sdk && python -m build --wheel && cp dist/*.whl ../polaris/ && cd ..

# 2. Copy the project's planning docs into the bundle
cp MEMORY.md TASKS.md polaris/

# 3. Set workspace secrets (one-time)
#    LIGHTSEI_API_KEY  — your bk_* workspace key (the bot uses this to
#                        post events back to Lightsei)
#    ANTHROPIC_API_KEY — used by the bot for the planning call

# 4. Deploy
lightsei deploy ./polaris --agent polaris
```

Optional env overrides on the deployed bot:
- `POLARIS_POLL_S` (default 3600) — seconds between ticks
- `POLARIS_MODEL` (default `claude-opus-4-7`) — Claude model id
- `POLARIS_DOCS_DIR` (default `.`) — where to find `MEMORY.md` /
  `TASKS.md` inside the bundle
- `POLARIS_DRY_RUN=1` — skip the Anthropic call entirely (verification
  only; emits `polaris.tick_dry_run` instead of `polaris.plan`)

## What a plan looks like

This is a real plan Polaris produced against this project's own
`MEMORY.md` and `TASKS.md` during Phase 6.5 prompt iteration. The
project was mid-Phase 6 (tasks 6.1-6.4 shipped, 6.5 active):

> **Summary.** Phase 6 (Polaris dogfood bot) is mid-flight: scaffold,
> plan event schema, latest-plan endpoint, and the /polaris dashboard
> view all shipped 2026-04-27. NOW is 6.5 system-prompt iteration
> before the Phase 6 demo.

> **Next actions:**
>
> 1. **Phase 6.5: draft polaris/system_prompt.md and hand-test it
>    against this project's MEMORY.md + TASKS.md by hitting the
>    Anthropic API directly until structured JSON output is stable.**
>    The 6.2 prompt is explicitly flagged as a placeholder. Iterating
>    outside the bot loop is faster than redeploying for each prompt
>    tweak, and the 6.6 demo bar is whether next_actions[0] matches
>    user intuition.
>
> 2. **Pin temperature ~0.2 and decide between Anthropic
>    structured-output / JSON mode versus a strict format example plus
>    parse-and-retry in polaris/bot.py.** 6.2 already shipped a
>    tolerant parser and parse-failure retry semantics; this step
>    locks in determinism so 6.6's idempotent re-runs land cleanly.
>
> 3. **Capture one representative plan and embed it verbatim in
>    polaris/README.md as the canonical Polaris output example.**
>    Required by the 6.5 task list and gives 6.6 a known-good baseline
>    to compare the deployed bot's first plan against.
>
> 4. **Phase 6.6 demo: build the wheel, copy MEMORY.md + TASKS.md
>    into polaris/, set ANTHROPIC_API_KEY + LIGHTSEI_API_KEY
>    workspace secrets, deploy via `lightsei deploy ./polaris --agent
>    polaris`, screenshot /polaris, log result.** Closes Phase 6A.
>    The sanity check (does next_actions[0] match user intuition)
>    gates whether to loop back to 6.5.
>    *Blocked by: 6.5 prompt must be stable first; ANTHROPIC_API_KEY
>    must be set as a workspace secret.*

> Parking lot promotions: none.
> Drift: none.

The first action correctly identified the active task (6.5 prompt
iteration). Items 2 and 3 echo specifics from the 6.5 task description
that have since been overtaken by Phase 6.5's actual choices (we picked
strict tool use over parse-and-retry, and Opus 4.7 has no `temperature`
parameter at all) — that is faithful to the task list as written and
will resolve once this Done Log entry lands.

Cost: ~31,000 input tokens / ~830 output tokens at `effort: "high"`
on `claude-opus-4-7`. About $0.18 per plan; tick interval defaults to
hourly, so roughly $4/day if the docs change every tick. The hash-skip
path keeps the steady-state cost near zero.
