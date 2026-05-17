"""Phase 14.1: tests for backend/eval_sampler.py.

Pure module tests — no LLM calls, no HTTP. Two surfaces under test:

1. `pick_sample` — sampling rule. Per-agent round-robin, recency-bias,
   skips system agents + already-evaluated runs + uncompleted runs +
   out-of-window runs.
2. `build_judge_prompt` — judge LLM turn composition. Agent role +
   system prompt + plan event + output event, forced tool_choice on
   the schema-strict SUBMIT_VERDICT_TOOL.

Schema enforcement (`SUBMIT_VERDICT_TOOL`) is also tested directly
since a bad schema means the judge's "good" default sneaks past in
prod and pollutes the quality signal.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from jsonschema import Draft202012Validator, ValidationError

from db import session_scope
from eval_sampler import (
    DEFAULT_PER_AGENT_PER_CYCLE,
    JUDGE_MODEL,
    SUBMIT_VERDICT_TOOL,
    _per_agent_cap,
    build_judge_prompt,
    pick_sample,
)
from models import Agent, Event, Run, RunEvaluation


# ---------- Helpers ---------- #


def _ensure_agent(
    session,
    workspace_id: str,
    *,
    name: str,
    role: str = "executor",
    system_prompt: str | None = None,
    model: str | None = None,
) -> None:
    """Create an Agent row directly. Tests need to control role +
    system_prompt independently of any /agents endpoint side effects."""
    now = datetime.now(timezone.utc)
    session.add(
        Agent(
            workspace_id=workspace_id,
            name=name,
            role=role,
            system_prompt=system_prompt,
            model=model,
            created_at=now,
            updated_at=now,
        )
    )


def _make_run(
    session,
    workspace_id: str,
    *,
    agent_name: str,
    started_at: datetime,
    ended_at: datetime | None,
    run_id: str | None = None,
) -> str:
    if run_id is None:
        run_id = str(uuid.uuid4())
    session.add(
        Run(
            id=run_id,
            workspace_id=workspace_id,
            agent_name=agent_name,
            started_at=started_at,
            ended_at=ended_at,
            cost_usd=Decimal("0"),
        )
    )
    return run_id


def _add_event(
    session,
    workspace_id: str,
    *,
    run_id: str,
    agent_name: str,
    kind: str,
    payload: dict,
    timestamp: datetime,
) -> None:
    session.add(
        Event(
            workspace_id=workspace_id,
            run_id=run_id,
            agent_name=agent_name,
            kind=kind,
            payload=payload,
            timestamp=timestamp,
        )
    )


def _add_verdict(
    session,
    workspace_id: str,
    *,
    run_id: str,
    agent_name: str,
    judge_model: str = JUDGE_MODEL,
    verdict: str = "good",
) -> None:
    session.add(
        RunEvaluation(
            id=str(uuid.uuid4()),
            run_id=run_id,
            workspace_id=workspace_id,
            agent_name=agent_name,
            judge_model=judge_model,
            verdict=verdict,
            reasons=["test reason"],
            confidence=Decimal("0.9"),
            judge_tokens_in=10,
            judge_tokens_out=20,
            judge_cost_usd=Decimal("0.001"),
            created_at=datetime.now(timezone.utc),
        )
    )


# ---------- pick_sample ---------- #


def test_pick_sample_empty_workspace_returns_empty(client, alice):
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        assert pick_sample(s, workspace_id) == []


def test_pick_sample_skips_system_agents(client, alice):
    """lightsei.* rows are accounting buckets, not bots. Sampling them
    would feed empty Run records to the judge (no events) and waste
    judge spend."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="lightsei.system")
        _make_run(
            s, workspace_id,
            agent_name="lightsei.system",
            started_at=now - timedelta(minutes=5),
            ended_at=now,
        )
    with session_scope() as s:
        assert pick_sample(s, workspace_id) == []


def test_pick_sample_respects_per_agent_cap(client, alice):
    """N completed runs for one agent, cap at K → returns exactly K,
    ordered by recency (most-recent first)."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        # 5 completed runs at descending timestamps so we can verify order.
        for i in range(5):
            _make_run(
                s, workspace_id,
                agent_name="argus",
                run_id=f"argus-run-{i}",
                started_at=now - timedelta(minutes=10 + i),
                ended_at=now - timedelta(minutes=5 + i),
            )
    with session_scope() as s:
        sampled = pick_sample(s, workspace_id, per_agent=3)
    assert sampled == ["argus-run-0", "argus-run-1", "argus-run-2"]


def test_pick_sample_skips_already_evaluated_runs(client, alice):
    """Runs with a verdict from JUDGE_MODEL are dropped from the pool;
    re-evaluating the same run with the same judge is wasted spend."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="vela")
        _make_run(
            s, workspace_id, agent_name="vela", run_id="vela-evaluated",
            started_at=now - timedelta(minutes=10),
            ended_at=now - timedelta(minutes=5),
        )
        _make_run(
            s, workspace_id, agent_name="vela", run_id="vela-fresh",
            started_at=now - timedelta(minutes=8),
            ended_at=now - timedelta(minutes=3),
        )
        _add_verdict(s, workspace_id, run_id="vela-evaluated", agent_name="vela")
    with session_scope() as s:
        sampled = pick_sample(s, workspace_id)
    assert sampled == ["vela-fresh"]


def test_pick_sample_dedup_is_keyed_on_judge_model(client, alice):
    """Verdicts from a different judge_model don't block re-eval — the
    dedup is per (run_id, judge_model) so a future Opus re-eval cycle
    can re-rate the same runs without conflict."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="vega")
        _make_run(
            s, workspace_id, agent_name="vega", run_id="vega-r1",
            started_at=now - timedelta(minutes=10),
            ended_at=now - timedelta(minutes=5),
        )
        _add_verdict(
            s, workspace_id, run_id="vega-r1", agent_name="vega",
            judge_model="claude-opus-4-7",  # different from JUDGE_MODEL
        )
    with session_scope() as s:
        sampled = pick_sample(s, workspace_id)
    assert sampled == ["vega-r1"]


def test_pick_sample_skips_runs_outside_window(client, alice):
    """Runs older than the window (default 1h) aren't candidates —
    fresh evals on stale runs miss the point."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _make_run(
            s, workspace_id, agent_name="argus", run_id="argus-old",
            started_at=now - timedelta(hours=3),
            ended_at=now - timedelta(hours=2),
        )
        _make_run(
            s, workspace_id, agent_name="argus", run_id="argus-fresh",
            started_at=now - timedelta(minutes=10),
            ended_at=now - timedelta(minutes=5),
        )
    with session_scope() as s:
        sampled = pick_sample(s, workspace_id)
    assert sampled == ["argus-fresh"]


def test_pick_sample_skips_uncompleted_runs(client, alice):
    """Runs without an ended_at are still in flight — judging them
    would mean reading a half-emitted output event."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _make_run(
            s, workspace_id, agent_name="argus", run_id="argus-running",
            started_at=now - timedelta(minutes=5),
            ended_at=None,
        )
        _make_run(
            s, workspace_id, agent_name="argus", run_id="argus-done",
            started_at=now - timedelta(minutes=10),
            ended_at=now - timedelta(minutes=2),
        )
    with session_scope() as s:
        sampled = pick_sample(s, workspace_id)
    assert sampled == ["argus-done"]


def test_pick_sample_covers_multiple_agents(client, alice):
    """Two agents, two runs each, cap=2 per agent → all 4 runs returned,
    grouped by agent name in iteration order."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _ensure_agent(s, workspace_id, name="vela")
        for i in range(2):
            _make_run(
                s, workspace_id, agent_name="argus", run_id=f"argus-{i}",
                started_at=now - timedelta(minutes=10 + i),
                ended_at=now - timedelta(minutes=5 + i),
            )
            _make_run(
                s, workspace_id, agent_name="vela", run_id=f"vela-{i}",
                started_at=now - timedelta(minutes=10 + i),
                ended_at=now - timedelta(minutes=5 + i),
            )
    with session_scope() as s:
        sampled = pick_sample(s, workspace_id, per_agent=2)
    # Agents iterated in alphabetical order; within each, recency DESC.
    assert sampled == ["argus-0", "argus-1", "vela-0", "vela-1"]


def test_pick_sample_workspace_isolation(client, alice, bob):
    """Alice's sampling never sees Bob's runs even if their agents
    have overlapping names."""
    a_ws = alice["workspace"]["id"]
    b_ws = bob["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, a_ws, name="argus")
        _ensure_agent(s, b_ws, name="argus")
        _make_run(
            s, a_ws, agent_name="argus", run_id="alice-argus",
            started_at=now - timedelta(minutes=10),
            ended_at=now - timedelta(minutes=5),
        )
        _make_run(
            s, b_ws, agent_name="argus", run_id="bob-argus",
            started_at=now - timedelta(minutes=10),
            ended_at=now - timedelta(minutes=5),
        )
    with session_scope() as s:
        assert pick_sample(s, a_ws) == ["alice-argus"]
        assert pick_sample(s, b_ws) == ["bob-argus"]


def test_per_agent_cap_reads_env_var(monkeypatch):
    """The runner-level default comes from env so ops can tune
    without a redeploy. Invalid values fall back to the constant."""
    monkeypatch.setenv("LIGHTSEI_EVAL_PER_AGENT_PER_CYCLE", "7")
    assert _per_agent_cap() == 7
    monkeypatch.setenv("LIGHTSEI_EVAL_PER_AGENT_PER_CYCLE", "garbage")
    assert _per_agent_cap() == DEFAULT_PER_AGENT_PER_CYCLE
    monkeypatch.setenv("LIGHTSEI_EVAL_PER_AGENT_PER_CYCLE", "0")
    assert _per_agent_cap() == 1  # floor of 1


# ---------- build_judge_prompt ---------- #


def test_build_judge_prompt_orchestrator_picks_plan_event(client, alice):
    """For an orchestrator like polaris, the `<name>.plan` event is
    the canonical 'what did the bot intend' signal — prefer it even
    if other non-envelope events exist in the run."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(
            s, workspace_id, name="polaris", role="orchestrator",
            system_prompt="You are Polaris.",
        )
        _make_run(
            s, workspace_id, agent_name="polaris", run_id="polaris-r1",
            started_at=now - timedelta(minutes=5), ended_at=now,
        )
        # Envelope event first (should be skipped).
        _add_event(
            s, workspace_id, run_id="polaris-r1", agent_name="polaris",
            kind="run_started", payload={},
            timestamp=now - timedelta(minutes=5),
        )
        # Bot event that ISN'T the plan (should not be picked as plan).
        _add_event(
            s, workspace_id, run_id="polaris-r1", agent_name="polaris",
            kind="polaris.tick_dry_run", payload={"hashes": {"foo": "bar"}},
            timestamp=now - timedelta(minutes=4),
        )
        # The real plan event.
        _add_event(
            s, workspace_id, run_id="polaris-r1", agent_name="polaris",
            kind="polaris.plan",
            payload={"summary": "do the thing"},
            timestamp=now - timedelta(minutes=3),
        )
    with session_scope() as s:
        prompt = build_judge_prompt(s, "polaris-r1")
    user_msg = prompt["messages"][0]["content"]
    assert "polaris.plan" in user_msg
    assert "do the thing" in user_msg
    assert "## System prompt" in user_msg
    assert "You are Polaris." in user_msg
    assert "role: orchestrator" in user_msg


def test_build_judge_prompt_executor_picks_first_bot_event(client, alice):
    """Executor bots don't have a `<name>.plan` convention. Fall back
    to the first non-envelope event from the agent — that's the
    tick/handler entrypoint (e.g. antares.tick, vela.check)."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(
            s, workspace_id, name="vela", role="executor",
            system_prompt="You are Vela.",
        )
        _make_run(
            s, workspace_id, agent_name="vela", run_id="vela-r1",
            started_at=now - timedelta(minutes=5), ended_at=now,
        )
        _add_event(
            s, workspace_id, run_id="vela-r1", agent_name="vela",
            kind="run_started", payload={},
            timestamp=now - timedelta(minutes=5),
        )
        _add_event(
            s, workspace_id, run_id="vela-r1", agent_name="vela",
            kind="vela.check", payload={"target": "https://example.com"},
            timestamp=now - timedelta(minutes=4),
        )
        _add_event(
            s, workspace_id, run_id="vela-r1", agent_name="vela",
            kind="vela.check_result", payload={"status": 200},
            timestamp=now - timedelta(minutes=3),
        )
    with session_scope() as s:
        prompt = build_judge_prompt(s, "vela-r1")
    user_msg = prompt["messages"][0]["content"]
    # Plan = first bot event (vela.check), output = last (vela.check_result).
    assert "vela.check" in user_msg
    assert "https://example.com" in user_msg
    assert "vela.check_result" in user_msg
    assert "status" in user_msg


def test_build_judge_prompt_single_event_run_uses_same_event_for_both(client, alice):
    """If the run has only one non-envelope event, plan == output.
    Common for polaris-style 'emit one plan, done' bots."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(
            s, workspace_id, name="polaris", role="orchestrator",
            system_prompt="You are Polaris.",
        )
        _make_run(
            s, workspace_id, agent_name="polaris", run_id="polaris-solo",
            started_at=now - timedelta(minutes=5), ended_at=now,
        )
        _add_event(
            s, workspace_id, run_id="polaris-solo", agent_name="polaris",
            kind="polaris.plan", payload={"summary": "single event run"},
            timestamp=now - timedelta(minutes=4),
        )
    with session_scope() as s:
        prompt = build_judge_prompt(s, "polaris-solo")
    user_msg = prompt["messages"][0]["content"]
    # Both sections show the same event.
    assert user_msg.count("polaris.plan") == 2
    assert user_msg.count("single event run") == 2


def test_build_judge_prompt_forces_submit_verdict_tool(client, alice):
    """Schema-strict forced tool_choice. A judge that responds in
    plain text would default to 'good' downstream — we don't want
    that fallback path to exist."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _make_run(
            s, workspace_id, agent_name="argus", run_id="argus-r1",
            started_at=now - timedelta(minutes=5), ended_at=now,
        )
        _add_event(
            s, workspace_id, run_id="argus-r1", agent_name="argus",
            kind="argus.scan", payload={},
            timestamp=now - timedelta(minutes=4),
        )
    with session_scope() as s:
        prompt = build_judge_prompt(s, "argus-r1")
    assert prompt["tool_choice"] == {"type": "tool", "name": "submit_verdict"}
    assert prompt["tools"] == [SUBMIT_VERDICT_TOOL]
    assert prompt["model"] == JUDGE_MODEL


def test_build_judge_prompt_raises_on_missing_run(client, alice):
    with session_scope() as s:
        with pytest.raises(ValueError, match="run .* not found"):
            build_judge_prompt(s, "no-such-run")


def test_build_judge_prompt_raises_on_deleted_agent(client, alice):
    """Run exists, agent row was deleted between sample + judge. The
    runner catches this and persists the error rather than crashing
    the loop."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        # No _ensure_agent() — agent row is missing on purpose.
        _make_run(
            s, workspace_id, agent_name="ghost", run_id="ghost-r1",
            started_at=now - timedelta(minutes=5), ended_at=now,
        )
    with session_scope() as s:
        with pytest.raises(ValueError, match="agent 'ghost' not found"):
            build_judge_prompt(s, "ghost-r1")


# ---------- SUBMIT_VERDICT_TOOL schema ---------- #


def test_submit_verdict_schema_accepts_good_verdict():
    schema = SUBMIT_VERDICT_TOOL["input_schema"]
    Draft202012Validator(schema).validate({
        "verdict": "good",
        "reasons": ["on-task", "well-formatted"],
        "confidence": 0.9,
    })


def test_submit_verdict_schema_rejects_unknown_verdict():
    schema = SUBMIT_VERDICT_TOOL["input_schema"]
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({
            "verdict": "great",  # not in enum
            "reasons": ["foo"],
            "confidence": 0.5,
        })


def test_submit_verdict_schema_requires_min_one_reason():
    schema = SUBMIT_VERDICT_TOOL["input_schema"]
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({
            "verdict": "good",
            "reasons": [],
            "confidence": 0.5,
        })


def test_submit_verdict_schema_caps_at_five_reasons():
    schema = SUBMIT_VERDICT_TOOL["input_schema"]
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({
            "verdict": "bad",
            "reasons": ["a", "b", "c", "d", "e", "f"],
            "confidence": 0.5,
        })


def test_submit_verdict_schema_rejects_confidence_out_of_range():
    schema = SUBMIT_VERDICT_TOOL["input_schema"]
    for bad in [-0.1, 1.1]:
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate({
                "verdict": "good",
                "reasons": ["ok"],
                "confidence": bad,
            })


def test_submit_verdict_schema_rejects_extra_fields():
    """additionalProperties: false. We want the judge's response
    shape to stay locked so we can add fields deliberately."""
    schema = SUBMIT_VERDICT_TOOL["input_schema"]
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({
            "verdict": "good",
            "reasons": ["ok"],
            "confidence": 0.5,
            "extra": "not allowed",
        })
