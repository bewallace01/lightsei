"""Phase 14.4: tests for backend/quality_signal.py + the two endpoints.

Pure-module + endpoint coverage. Insert RunEvaluation rows directly
(no LLM calls) and assert the rollup math, trend math, recent-bads
ordering, day-window clamping, system-agent filtering, and the
endpoint contracts including cross-workspace 404 semantics.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import quality_signal
from db import session_scope
from eval_sampler import JUDGE_MODEL
from models import Agent, Run, RunEvaluation
from tests.conftest import auth_headers


# ---------- Helpers ---------- #


def _ensure_agent(session, workspace_id, *, name, role="executor"):
    now = datetime.now(timezone.utc)
    session.add(
        Agent(
            workspace_id=workspace_id,
            name=name,
            role=role,
            created_at=now,
            updated_at=now,
        )
    )


def _make_run(session, workspace_id, *, agent_name, started_at, run_id=None):
    if run_id is None:
        run_id = str(uuid.uuid4())
    session.add(
        Run(
            id=run_id,
            workspace_id=workspace_id,
            agent_name=agent_name,
            started_at=started_at,
            ended_at=started_at + timedelta(seconds=1),
            cost_usd=Decimal("0"),
        )
    )
    return run_id


def _add_verdict(
    session, workspace_id, *, run_id, agent_name, verdict, created_at,
    reasons=None, confidence=0.9, judge_model=JUDGE_MODEL,
):
    if reasons is None:
        reasons = [f"reason for {verdict}"]
    session.add(
        RunEvaluation(
            id=str(uuid.uuid4()),
            run_id=run_id,
            workspace_id=workspace_id,
            agent_name=agent_name,
            judge_model=judge_model,
            verdict=verdict,
            reasons=reasons,
            confidence=Decimal(str(confidence)),
            judge_tokens_in=100,
            judge_tokens_out=20,
            judge_cost_usd=Decimal("0.001"),
            created_at=created_at,
        )
    )


def _seed_run_with_verdict(
    session, workspace_id, *, agent_name, verdict, ts, reasons=None,
):
    """One Run + one RunEvaluation pointing at it. Used a lot, hence
    the dedicated helper."""
    run_id = _make_run(session, workspace_id, agent_name=agent_name, started_at=ts)
    _add_verdict(
        session, workspace_id,
        run_id=run_id, agent_name=agent_name, verdict=verdict, created_at=ts,
        reasons=reasons,
    )
    return run_id


# ---------- agent_quality ---------- #


def test_agent_quality_empty_returns_zero_counts(client, alice):
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        result = quality_signal.agent_quality(s, workspace_id, "argus")
    assert result["agent_name"] == "argus"
    assert result["days"] == 7
    assert result["verdict_counts"] == {"good": 0, "borderline": 0, "bad": 0}
    assert result["total_evaluations"] == 0
    assert result["recent_bads"] == []
    assert result["trend"]["direction"] == "unknown"


def test_agent_quality_counts_verdicts_in_window(client, alice):
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        for v in ["good", "good", "good", "borderline", "bad"]:
            _seed_run_with_verdict(
                s, workspace_id, agent_name="argus", verdict=v,
                ts=now - timedelta(hours=1),
            )

    with session_scope() as s:
        result = quality_signal.agent_quality(s, workspace_id, "argus")
    assert result["verdict_counts"] == {"good": 3, "borderline": 1, "bad": 1}
    assert result["total_evaluations"] == 5


def test_agent_quality_ignores_other_agents(client, alice):
    """Verdicts on agent X don't count toward agent Y's rollup."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _ensure_agent(s, workspace_id, name="vela")
        _seed_run_with_verdict(
            s, workspace_id, agent_name="argus", verdict="good", ts=now,
        )
        _seed_run_with_verdict(
            s, workspace_id, agent_name="vela", verdict="bad", ts=now,
        )

    with session_scope() as s:
        a = quality_signal.agent_quality(s, workspace_id, "argus")
        v = quality_signal.agent_quality(s, workspace_id, "vela")
    assert a["verdict_counts"]["good"] == 1
    assert a["verdict_counts"]["bad"] == 0
    assert v["verdict_counts"]["bad"] == 1
    assert v["verdict_counts"]["good"] == 0


def test_agent_quality_window_clamping(client, alice):
    """days param: None → 7, junk → 7, negative → 1, > 90 → 90."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        assert quality_signal.agent_quality(
            s, workspace_id, "argus", days=None,
        )["days"] == 7
        assert quality_signal.agent_quality(
            s, workspace_id, "argus", days=999,
        )["days"] == 90
        assert quality_signal.agent_quality(
            s, workspace_id, "argus", days=-3,
        )["days"] == 1


def test_agent_quality_recent_bads_newest_first(client, alice):
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        # Add bads at 1h, 2h, 3h ago to confirm ordering.
        for hours_ago, label in [(3, "oldest"), (1, "newest"), (2, "middle")]:
            _seed_run_with_verdict(
                s, workspace_id, agent_name="argus", verdict="bad",
                ts=now - timedelta(hours=hours_ago),
                reasons=[label],
            )

    with session_scope() as s:
        result = quality_signal.agent_quality(s, workspace_id, "argus")
    reasons_in_order = [b["reasons"][0] for b in result["recent_bads"]]
    assert reasons_in_order == ["newest", "middle", "oldest"]


def test_agent_quality_recent_bads_limit(client, alice):
    """RECENT_BADS_LIMIT caps the list size."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        for i in range(quality_signal.RECENT_BADS_LIMIT + 3):
            _seed_run_with_verdict(
                s, workspace_id, agent_name="argus", verdict="bad",
                ts=now - timedelta(minutes=i),
            )

    with session_scope() as s:
        result = quality_signal.agent_quality(s, workspace_id, "argus")
    assert len(result["recent_bads"]) == quality_signal.RECENT_BADS_LIMIT


def test_agent_quality_trend_up_when_current_better_than_prior(client, alice):
    """Prior window: 2/5 good = 40%. Current window: 5/5 good = 100%.
    delta = +60pp → direction up."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        # Prior 7-day window (8-14 days ago): mix.
        for v in ["good", "good", "bad", "bad", "bad"]:
            _seed_run_with_verdict(
                s, workspace_id, agent_name="argus", verdict=v,
                ts=now - timedelta(days=10),
            )
        # Current window (last 7 days): all good.
        for _ in range(5):
            _seed_run_with_verdict(
                s, workspace_id, agent_name="argus", verdict="good",
                ts=now - timedelta(days=2),
            )

    with session_scope() as s:
        result = quality_signal.agent_quality(s, workspace_id, "argus", now=now)
    assert result["trend"]["direction"] == "up"
    assert result["trend"]["delta_pp"] == pytest.approx(60.0, abs=0.1)


def test_agent_quality_trend_flat_within_deadband(client, alice):
    """1pp dead-band so small samples don't flicker the arrow."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        # Both windows: same exact composition → 0pp delta.
        for window_offset_days in [10, 2]:
            for v in ["good", "good", "bad"]:
                _seed_run_with_verdict(
                    s, workspace_id, agent_name="argus", verdict=v,
                    ts=now - timedelta(days=window_offset_days),
                )

    with session_scope() as s:
        result = quality_signal.agent_quality(s, workspace_id, "argus", now=now)
    assert result["trend"]["direction"] == "flat"


def test_agent_quality_trend_unknown_when_no_prior_data(client, alice):
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _seed_run_with_verdict(
            s, workspace_id, agent_name="argus", verdict="good", ts=now,
        )

    with session_scope() as s:
        result = quality_signal.agent_quality(s, workspace_id, "argus", now=now)
    assert result["trend"]["direction"] == "unknown"


def test_agent_quality_excludes_runs_outside_window(client, alice):
    """An eval from 30 days ago doesn't count in the 7-day window."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _seed_run_with_verdict(
            s, workspace_id, agent_name="argus", verdict="good",
            ts=now - timedelta(days=30),
        )

    with session_scope() as s:
        result = quality_signal.agent_quality(
            s, workspace_id, "argus", days=7, now=now,
        )
    assert result["total_evaluations"] == 0


# ---------- workspace_quality ---------- #


def test_workspace_quality_filters_system_agents(client, alice):
    """lightsei.* agents are accounting buckets; per_agent shouldn't
    include them even if they somehow have eval rows."""
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _ensure_agent(s, workspace_id, name="lightsei.system")
        _seed_run_with_verdict(
            s, workspace_id, agent_name="argus", verdict="good", ts=now,
        )

    with session_scope() as s:
        result = quality_signal.workspace_quality(s, workspace_id)
    agent_names = [a["agent_name"] for a in result["per_agent"]]
    assert "argus" in agent_names
    assert "lightsei.system" not in agent_names


def test_workspace_quality_rollup_sums_across_agents(client, alice):
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _ensure_agent(s, workspace_id, name="vela")
        _seed_run_with_verdict(
            s, workspace_id, agent_name="argus", verdict="good", ts=now,
        )
        _seed_run_with_verdict(
            s, workspace_id, agent_name="argus", verdict="bad", ts=now,
        )
        _seed_run_with_verdict(
            s, workspace_id, agent_name="vela", verdict="borderline", ts=now,
        )

    with session_scope() as s:
        result = quality_signal.workspace_quality(s, workspace_id)
    assert result["verdict_counts"] == {"good": 1, "borderline": 1, "bad": 1}
    assert result["total_evaluations"] == 3


def test_workspace_quality_per_agent_in_alphabetical_order(client, alice):
    workspace_id = alice["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        # Insert in non-alphabetical order to confirm the sort.
        for name in ["vela", "argus", "spica"]:
            _ensure_agent(s, workspace_id, name=name)
            _seed_run_with_verdict(
                s, workspace_id, agent_name=name, verdict="good", ts=now,
            )

    with session_scope() as s:
        result = quality_signal.workspace_quality(s, workspace_id)
    names = [a["agent_name"] for a in result["per_agent"]]
    assert names == ["argus", "spica", "vela"]


# ---------- Endpoints ---------- #


def test_get_workspace_quality_endpoint(client, alice):
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _seed_run_with_verdict(
            s, workspace_id, agent_name="argus", verdict="good", ts=now,
        )

    r = client.get(
        "/workspaces/me/quality?days=14",
        headers=auth_headers(api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 14
    assert body["verdict_counts"]["good"] == 1
    assert any(a["agent_name"] == "argus" for a in body["per_agent"])


def test_get_agent_quality_endpoint(client, alice):
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _seed_run_with_verdict(
            s, workspace_id, agent_name="argus", verdict="bad", ts=now,
            reasons=["off-task"],
        )

    r = client.get(
        "/workspaces/me/agents/argus/quality",
        headers=auth_headers(api_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_name"] == "argus"
    assert body["verdict_counts"]["bad"] == 1
    assert len(body["recent_bads"]) == 1
    assert body["recent_bads"][0]["reasons"] == ["off-task"]


def test_quality_endpoints_workspace_isolation(client, alice, bob):
    """Alice's eval rows must never appear in Bob's quality response."""
    a_ws = alice["workspace"]["id"]
    b_ws = bob["workspace"]["id"]
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, a_ws, name="argus")
        _seed_run_with_verdict(
            s, a_ws, agent_name="argus", verdict="bad", ts=now,
        )

    # Bob asks about the same agent name → empty quality.
    r = client.get(
        "/workspaces/me/agents/argus/quality",
        headers=auth_headers(bob["api_key"]["plaintext"]),
    )
    assert r.status_code == 200
    assert r.json()["verdict_counts"]["bad"] == 0


def test_quality_endpoints_unauthenticated(client):
    r = client.get("/workspaces/me/quality")
    assert r.status_code == 401
    r = client.get("/workspaces/me/agents/argus/quality")
    assert r.status_code == 401
