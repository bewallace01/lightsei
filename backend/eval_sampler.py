"""Phase 14.1: sampling rule + judge prompt + tool schema.

Pure module. No DB writes, no LLM calls. The periodic eval job
(Phase 14.3) wires this to the runner pattern from 12C.6.2 and
persists verdicts to `run_evaluations` (Phase 14.2).

Two surfaces here:

1. `pick_sample(session, workspace_id, per_agent=3)` — returns the
   run_ids to evaluate this cycle. Per-agent round-robin: for each
   non-`lightsei.*` agent in the workspace, the last hour's completed
   runs that don't yet have a `run_evaluations` row, recency-ordered,
   capped at `per_agent`. Per-agent cap is the fairness mechanism;
   no cross-agent re-weighting in v1.

2. `build_judge_prompt(session, run_id)` — composes the judge's
   single LLM turn. Pulls the agent's role + system prompt + the
   run's "plan" event (orchestrator: `<name>.plan`; executor: the
   first bot-specific event in the run) + the run's "output" event
   (the last bot-specific event before `run_ended`). Returns a dict
   ready to pass to `client.messages.create(messages=[...])`.

`SUBMIT_VERDICT_TOOL` is the forced-tool schema the judge must call.
Schema-strict so a malformed response surfaces as a judge failure
rather than defaulting to `good` (same pattern as `agent_generator`'s
`submit_bot` tool).

Design choices and switch triggers live in TASKS.md "Phase 14"
intro (locked 2026-05-17). Re-read before changing the sampling
rule, judge model, or judge depth.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session

from models import Agent, Event, Run, RunEvaluation


# Locked in TASKS.md Phase 14 "design choices settled 2026-05-17".
# Switch triggers + reasoning live there.
JUDGE_MODEL = "claude-sonnet-4-6"
DEFAULT_PER_AGENT_PER_CYCLE = 3
DEFAULT_SAMPLE_WINDOW = timedelta(hours=1)

# Synthetic agent name for cost accounting (generation + judge spend
# both attribute here). Same name used by agent_generator and
# team_planner — kept in sync deliberately so the workspace cost
# rollup buckets all "Lightsei system" spend together.
SYSTEM_AGENT_PREFIX = "lightsei."

# SDK-auto events we don't treat as "the bot's work". `pick_sample`
# doesn't filter on these; `build_judge_prompt` does.
_ENVELOPE_EVENT_KINDS = frozenset({
    "run_started",
    "run_ended",
    "llm_call_started",
    "llm_call_completed",
})


SUBMIT_VERDICT_TOOL: dict[str, Any] = {
    "name": "submit_verdict",
    "description": (
        "Submit your verdict on the bot's run. `verdict` is your overall "
        "rating: `good` (the bot's plan and output were on-task and "
        "high-quality), `borderline` (acceptable but with caveats — "
        "rambling, partial, missed a small thing), or `bad` (off-task, "
        "factually wrong, broken output, or otherwise something the user "
        "would want to know about). `reasons` is 1-5 short bullet "
        "strings explaining the verdict. `confidence` is your confidence "
        "in the verdict from 0.0 to 1.0."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["good", "borderline", "bad"],
                "description": "Overall rating of the bot's run.",
            },
            "reasons": {
                "type": "array",
                "items": {"type": "string", "maxLength": 500},
                "minItems": 1,
                "maxItems": 5,
                "description": "1-5 short bullets justifying the verdict.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence in the verdict, 0.0 to 1.0.",
            },
        },
        "required": ["verdict", "reasons", "confidence"],
        "additionalProperties": False,
    },
}


def _per_agent_cap() -> int:
    """Read the per-agent cap from env, falling back to the default.

    Kept as a helper so tests can monkeypatch the env without restarting
    the module.
    """
    raw = os.environ.get("LIGHTSEI_EVAL_PER_AGENT_PER_CYCLE")
    if raw is None:
        return DEFAULT_PER_AGENT_PER_CYCLE
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_PER_AGENT_PER_CYCLE
    return max(1, n)


def pick_sample(
    session: Session,
    workspace_id: str,
    *,
    per_agent: Optional[int] = None,
    window: timedelta = DEFAULT_SAMPLE_WINDOW,
    now: Optional[datetime] = None,
) -> list[str]:
    """Return run_ids to evaluate this cycle.

    For each non-system agent in the workspace: take its completed
    runs from the last `window` (default 1 hour) that don't already
    have a verdict, recency-ordered, capped at `per_agent` (default
    `LIGHTSEI_EVAL_PER_AGENT_PER_CYCLE` or 3).

    "Completed" means `runs.ended_at IS NOT NULL`. The dedup check
    looks for any `run_evaluations` row keyed on `(run_id, judge_model)`
    where judge_model matches `JUDGE_MODEL` — switching the judge
    later (Opus re-eval, etc.) is allowed to re-evaluate.

    Args:
        session: Active SQLAlchemy session.
        workspace_id: Workspace to sample within.
        per_agent: Override the per-agent cap; falls back to env / default.
        window: How far back to look. Default 1 hour matches the eval
            job's hourly tick (Phase 14.3).
        now: Override the "current time" anchor. Default `datetime.now(UTC)`.
            Provided for tests; production code should leave it unset.

    Returns:
        Flat list of run_ids, ordered by agent name then ended_at DESC
        within an agent. Empty list if nothing to evaluate.
    """
    if per_agent is None:
        per_agent = _per_agent_cap()
    now = now or datetime.now(timezone.utc)
    cutoff = now - window

    # 1. Find the workspace's non-system agents. The agents list is
    # tiny (≤ a few dozen rows) so we can pull it once and loop in
    # Python rather than building one giant CTE — keeps the per-agent
    # cap easy to reason about.
    agent_names = session.execute(
        select(Agent.name)
        .where(Agent.workspace_id == workspace_id)
        .order_by(Agent.name)
    ).scalars().all()
    agent_names = [
        n for n in agent_names if not n.startswith(SYSTEM_AGENT_PREFIX)
    ]
    if not agent_names:
        return []

    # 2. For each agent, pick up to `per_agent` of its most-recent
    # completed runs that don't already have a verdict from JUDGE_MODEL.
    already_evaluated = (
        select(RunEvaluation.run_id)
        .where(RunEvaluation.judge_model == JUDGE_MODEL)
        .scalar_subquery()
    )

    sampled: list[str] = []
    for name in agent_names:
        rows = session.execute(
            select(Run.id)
            .where(
                Run.workspace_id == workspace_id,
                Run.agent_name == name,
                Run.ended_at.is_not(None),
                Run.ended_at >= cutoff,
                ~Run.id.in_(already_evaluated),
            )
            .order_by(Run.ended_at.desc())
            .limit(per_agent)
        ).scalars().all()
        sampled.extend(rows)
    return sampled


def _pick_plan_event(events: list[Event], agent_name: str) -> Optional[Event]:
    """Pick the event that represents the bot's intent / plan for the run.

    Orchestrator-shaped: prefer `<agent_name>.plan` if present (e.g.
    `polaris.plan`). Otherwise the first non-envelope event in the
    run from this agent — for executor bots this is typically the
    tick / command-handler entrypoint (e.g. `antares.tick`,
    `vela.check`).
    """
    plan_kind = f"{agent_name}.plan"
    for ev in events:
        if ev.kind == plan_kind:
            return ev
    for ev in events:
        if ev.kind in _ENVELOPE_EVENT_KINDS:
            continue
        if ev.agent_name != agent_name:
            continue
        return ev
    return None


def _pick_output_event(events: list[Event], agent_name: str) -> Optional[Event]:
    """Pick the event that represents the bot's final action / output.

    Heuristic: the last non-envelope event in the run from this agent.
    For single-event runs (common for polaris-like "emit one plan and
    done" bots) this is the same event as the plan; the judge still
    gets meaningful signal.
    """
    for ev in reversed(events):
        if ev.kind in _ENVELOPE_EVENT_KINDS:
            continue
        if ev.agent_name != agent_name:
            continue
        return ev
    return None


def _event_summary(ev: Event) -> dict[str, Any]:
    """Serialize an event for the judge prompt. Drops the db-internal
    `id` and the cross-run `workspace_id` since the judge doesn't need
    them. Keeps payload as-is so the judge sees the bot's real output."""
    return {
        "kind": ev.kind,
        "timestamp": ev.timestamp.isoformat() if ev.timestamp else None,
        "payload": ev.payload,
    }


def build_judge_prompt(
    session: Session,
    run_id: str,
) -> dict[str, Any]:
    """Compose the judge LLM turn for a single run.

    Returns a dict with `system` (the judge's framing), `messages`
    (the single user turn carrying the run's plan + output), `model`,
    `tools`, and `tool_choice`. Ready to splat into
    `anthropic.Anthropic().messages.create(**prompt)`.

    Raises ValueError if the run can't be loaded or the agent has been
    deleted between sampling and judging. The eval runner catches and
    persists the error rather than crashing the loop.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")

    agent = session.execute(
        select(Agent).where(
            Agent.workspace_id == run.workspace_id,
            Agent.name == run.agent_name,
        )
    ).scalar_one_or_none()
    if agent is None:
        raise ValueError(
            f"agent {run.agent_name!r} not found in workspace "
            f"{run.workspace_id!r} (deleted between sample + judge?)"
        )

    events = session.execute(
        select(Event)
        .where(Event.run_id == run_id)
        .order_by(Event.timestamp, Event.id)
    ).scalars().all()

    plan_event = _pick_plan_event(events, run.agent_name)
    output_event = _pick_output_event(events, run.agent_name)

    # Even for single-event runs, expose both fields so the judge prompt
    # shape stays uniform — easier to reason about + simpler tests.
    plan_payload = _event_summary(plan_event) if plan_event else None
    output_payload = _event_summary(output_event) if output_event else None

    system = (
        "You are a quality judge for an AI agent platform called Lightsei. "
        "Your job is to read what one bot did in one run, and rate it. "
        "You see (1) the bot's role and system prompt — what it was "
        "designed to do, (2) its 'plan' event — what it intended to do "
        "this run, and (3) its 'output' event — what it actually "
        "produced or did.\n\n"
        "Rate the run as `good` (on-task and high-quality), `borderline` "
        "(acceptable with caveats — rambling, partial, missed a small "
        "thing), or `bad` (off-task, factually wrong, broken output, or "
        "otherwise something the user would want flagged). Give 1-5 "
        "short reasons. Confidence is your confidence in the verdict "
        "from 0.0 to 1.0; use low confidence when the run is hard to "
        "judge (e.g. a tick that intentionally did nothing because no "
        "input changed — that's typically `good` at low confidence "
        "rather than a strong verdict either way).\n\n"
        "You MUST call the `submit_verdict` tool to deliver your answer. "
        "Do not respond in plain text."
    )

    user_msg = (
        f"# Agent\n\n"
        f"- name: {run.agent_name}\n"
        f"- role: {agent.role or 'unspecified'}\n"
        f"- model: {agent.model or 'unspecified'}\n\n"
        f"## System prompt\n\n"
        f"```\n{(agent.system_prompt or '').strip() or '(none set)'}\n```\n\n"
        f"# Run\n\n"
        f"- run_id: {run_id}\n"
        f"- started_at: {run.started_at.isoformat() if run.started_at else 'unknown'}\n"
        f"- ended_at: {run.ended_at.isoformat() if run.ended_at else 'unknown'}\n\n"
        f"## Plan event\n\n"
        f"```json\n{json.dumps(plan_payload, indent=2, default=str)}\n```\n\n"
        f"## Output event\n\n"
        f"```json\n{json.dumps(output_payload, indent=2, default=str)}\n```\n\n"
        "Read the above and call `submit_verdict`."
    )

    return {
        "model": JUDGE_MODEL,
        "max_tokens": 1024,
        "system": system,
        "tools": [SUBMIT_VERDICT_TOOL],
        "tool_choice": {"type": "tool", "name": "submit_verdict"},
        "messages": [{"role": "user", "content": user_msg}],
    }
