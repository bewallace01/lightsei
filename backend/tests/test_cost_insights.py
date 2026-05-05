"""Phase 12D.1: cost-intelligence insights.

The pure module + the /workspaces/me/cost/insights endpoint. Each
insight is independent so we test them as discrete units against
hand-seeded events; the endpoint itself is exercised once at the
bottom for shape + workspace isolation.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text

from cost_insights import (
    all_insights,
    cache_skip_savings,
    failed_call_cost,
    model_tier_mismatch,
    per_trigger_roi,
    plan_volatility,
)
from db import session_scope
from models import Event, Run
from tests.conftest import auth_headers


# ---------- Helpers ---------- #


def _emit(session, workspace_id, *, run_id, kind, payload, agent_name="polaris",
          ts=None):
    """Insert an Event row directly so tests don't go through HTTP."""
    if ts is None:
        ts = datetime.now(timezone.utc)
    session.add(
        Event(
            workspace_id=workspace_id,
            run_id=run_id,
            agent_name=agent_name,
            kind=kind,
            payload=payload,
            timestamp=ts,
        )
    )


def _make_run(session, workspace_id, *, agent_name="polaris", run_id=None,
              cost_usd=0, started_at=None):
    if run_id is None:
        run_id = str(uuid.uuid4())
    if started_at is None:
        started_at = datetime.now(timezone.utc)
    session.add(
        Run(
            id=run_id,
            workspace_id=workspace_id,
            agent_name=agent_name,
            started_at=started_at,
            ended_at=None,
            cost_usd=Decimal(str(cost_usd)),
        )
    )
    return run_id


# ---------- cache_skip_savings ---------- #


def test_cache_skip_savings_zero_when_nothing_skipped(client, alice):
    """Empty workspace → 0 skipped, $0 saved, but the insight still
    renders so the user knows the cache is in place."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        result = cache_skip_savings(s, workspace_id)
    assert result["kind"] == "cache_skip_savings"
    assert result["detail"]["skipped_ticks"] == 0
    assert result["detail"]["estimated_saved_usd"] == 0


def test_cache_skip_savings_multiplies_skips_by_median(client, alice):
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        # Three plans with token costs that map to a non-trivial median.
        # claude-opus-4-7 input=$15/M, output=$75/M.
        for tokens_in, tokens_out in [(1000, 100), (2000, 200), (3000, 300)]:
            run_id = _make_run(s, workspace_id)
            _emit(
                s, workspace_id, run_id=run_id, kind="polaris.plan",
                payload={
                    "model": "claude-opus-4-7",
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                },
            )
        # Two skipped ticks.
        for _ in range(2):
            run_id = _make_run(s, workspace_id)
            _emit(
                s, workspace_id, run_id=run_id, kind="polaris.tick_skipped",
                payload={"reason": "docs unchanged"},
            )

    with session_scope() as s:
        result = cache_skip_savings(s, workspace_id)
    assert result["detail"]["skipped_ticks"] == 2
    # Median of the three plan costs × 2 skipped.
    expected_per_plan_cost = (2000 * 15 + 200 * 75) / 1_000_000
    expected_savings = round(2 * expected_per_plan_cost, 4)
    assert result["detail"]["estimated_saved_usd"] == expected_savings


# ---------- plan_volatility ---------- #


def test_plan_volatility_no_streak_when_plans_diverge(client, alice):
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        for i in range(4):
            run_id = _make_run(s, workspace_id)
            _emit(
                s, workspace_id, run_id=run_id, kind="polaris.plan",
                payload={
                    "summary": f"plan {i} — different content each time",
                    "next_actions": [{"task": f"action-{i}", "blocked_by": None}],
                },
                ts=datetime.now(timezone.utc) - timedelta(hours=4 - i),
            )

    with session_scope() as s:
        result = plan_volatility(s, workspace_id)
    assert result["detail"]["identical_streak"] == 1  # most-recent-only matches itself
    assert result["apply"] is None


def test_plan_volatility_flags_streak(client, alice):
    workspace_id = alice["workspace"]["id"]
    same_payload = {
        "summary": "Phase 12 just shipped; nothing new to plan.",
        "next_actions": [
            {"task": "wait for the next push", "blocked_by": None},
        ],
    }
    with session_scope() as s:
        for i in range(4):
            run_id = _make_run(s, workspace_id)
            _emit(
                s, workspace_id, run_id=run_id, kind="polaris.plan",
                payload=dict(same_payload),
                ts=datetime.now(timezone.utc) - timedelta(hours=4 - i),
            )
    with session_scope() as s:
        result = plan_volatility(s, workspace_id)
    assert result["detail"]["identical_streak"] >= 3
    assert result["apply"] is not None
    assert "/agents/polaris" in result["apply"]["href"]


# ---------- model_tier_mismatch ---------- #


def test_model_tier_mismatch_recommends_haiku_when_inputs_small(client, alice):
    """All Atlas's recent llm_call_completed events used Opus on small
    inputs. The insight should propose haiku and project savings."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        for _ in range(20):
            run_id = _make_run(s, workspace_id, agent_name="atlas")
            _emit(
                s, workspace_id, run_id=run_id, kind="llm_call_completed",
                agent_name="atlas",
                payload={
                    "model": "claude-opus-4-7",
                    "input_tokens": 800,  # tiny — definitely fits in haiku
                    "output_tokens": 200,
                },
            )

    with session_scope() as s:
        results = model_tier_mismatch(s, workspace_id)

    atlas = [r for r in results if r["detail"]["agent"] == "atlas"]
    assert len(atlas) == 1
    assert atlas[0]["detail"]["current_model"] == "claude-opus-4-7"
    assert atlas[0]["detail"]["suggested_model"] == "claude-sonnet-4-6"
    assert atlas[0]["detail"]["savings_pct"] > 0
    assert atlas[0]["apply"]["patch"] == {"model": "claude-sonnet-4-6"}


def test_model_tier_mismatch_skips_when_inputs_too_large(client, alice):
    """If even one of the tail inputs exceeds 16k tokens the recommendation
    gets gated — we don't want to suggest a downgrade for an agent whose
    workload sometimes legitimately needs the bigger model."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        for i in range(20):
            run_id = _make_run(s, workspace_id, agent_name="polaris")
            _emit(
                s, workspace_id, run_id=run_id, kind="llm_call_completed",
                agent_name="polaris",
                payload={
                    "model": "claude-opus-4-7",
                    "input_tokens": 80_000 if i == 0 else 800,  # one giant call
                    "output_tokens": 200,
                },
            )

    with session_scope() as s:
        results = model_tier_mismatch(s, workspace_id)
    assert not [r for r in results if r["detail"]["agent"] == "polaris"]


# ---------- failed_call_cost ---------- #


def test_failed_call_cost_zero_when_no_failures(client, alice):
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        result = failed_call_cost(s, workspace_id)
    assert result["detail"]["failed_call_count"] == 0
    assert result["detail"]["estimated_wasted_usd"] == 0


def test_failed_call_cost_groups_by_reason(client, alice):
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        for err in ("RateLimitError", "RateLimitError", "ConnectionError"):
            run_id = _make_run(s, workspace_id)
            _emit(
                s, workspace_id, run_id=run_id, kind="llm_call_failed",
                payload={
                    "model": "claude-opus-4-7",
                    "input_tokens": 1000,
                    "error": f"{err}('something')",
                },
            )

    with session_scope() as s:
        result = failed_call_cost(s, workspace_id)
    assert result["detail"]["failed_call_count"] == 3
    assert result["detail"]["estimated_wasted_usd"] > 0
    reasons = {r["reason"]: r["count"] for r in result["detail"]["top_reasons"]}
    assert reasons["RateLimitError"] == 2
    assert reasons["ConnectionError"] == 1


# ---------- per_trigger_roi ---------- #


def test_per_trigger_roi_flags_low_useful_rate(client, alice):
    """Polaris ran 10 times in a week, 9 cache-skipped + 1 useful = 10%
    useful rate. Should flag with an apply link to the schedule."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        # 10 runs.
        run_ids = [
            _make_run(s, workspace_id, agent_name="polaris")
            for _ in range(10)
        ]
        # 9 of them only emitted polaris.tick_skipped (not "useful").
        for rid in run_ids[:9]:
            _emit(
                s, workspace_id, run_id=rid, kind="polaris.tick_skipped",
                agent_name="polaris", payload={"reason": "docs unchanged"},
            )
        # The 10th emitted a polaris.plan (useful).
        _emit(
            s, workspace_id, run_id=run_ids[9], kind="polaris.plan",
            agent_name="polaris", payload={"summary": "x", "next_actions": []},
        )

    with session_scope() as s:
        results = per_trigger_roi(s, workspace_id)
    polaris = [r for r in results if r["detail"]["agent"] == "polaris"]
    assert len(polaris) == 1
    assert polaris[0]["detail"]["useful_runs"] == 1
    assert polaris[0]["detail"]["total_runs"] == 10
    assert polaris[0]["detail"]["useful_pct"] == 10.0


def test_per_trigger_roi_skips_when_too_few_runs(client, alice):
    """Below 5 runs the rate isn't reliable — don't flag."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        for _ in range(3):
            _make_run(s, workspace_id, agent_name="atlas")
    with session_scope() as s:
        results = per_trigger_roi(s, workspace_id)
    assert not [r for r in results if r["detail"]["agent"] == "atlas"]


# ---------- Endpoint shape + workspace isolation ---------- #


def test_endpoint_returns_homogeneous_list(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.get("/workspaces/me/cost/insights", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert "insights" in body
    assert isinstance(body["insights"], list)
    # Even on an empty workspace we get the 3 always-rendered insights:
    # cache_skip_savings, failed_call_cost, plan_volatility.
    kinds = {i["kind"] for i in body["insights"]}
    assert "cache_skip_savings" in kinds
    assert "failed_call_cost" in kinds
    assert "plan_volatility" in kinds
    # Every insight has the homogeneous shape.
    for ins in body["insights"]:
        assert "kind" in ins
        assert "headline" in ins
        assert "detail" in ins
        assert "apply" in ins  # may be None


def test_endpoint_workspace_isolated(client, alice, bob):
    """Bob's failures don't show up in Alice's insights."""
    workspace_id_b = bob["workspace"]["id"]
    with session_scope() as s:
        for _ in range(5):
            run_id = _make_run(s, workspace_id_b, agent_name="bobs-bot")
            _emit(
                s, workspace_id_b, run_id=run_id, kind="llm_call_failed",
                agent_name="bobs-bot",
                payload={"model": "claude-opus-4-7", "input_tokens": 5000,
                         "error": "BobError"},
            )

    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.get("/workspaces/me/cost/insights", headers=h)
    body = r.json()
    failed = next(i for i in body["insights"] if i["kind"] == "failed_call_cost")
    assert failed["detail"]["failed_call_count"] == 0  # alice has none


def test_all_insights_combines_lists_and_singletons(client, alice):
    """all_insights() is the wrapper main.py calls. Confirm it includes
    both list-shaped insights (model_tier_mismatch, per_trigger_roi)
    expanded and singleton ones."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        result = all_insights(s, workspace_id)
    assert isinstance(result["insights"], list)
    # Three singletons always run.
    kinds = [i["kind"] for i in result["insights"]]
    assert "cache_skip_savings" in kinds
    assert "failed_call_cost" in kinds
    assert "plan_volatility" in kinds
