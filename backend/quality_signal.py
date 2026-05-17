"""Phase 14.4: read-side queries over `run_evaluations`.

Pure module — no LLM calls, no writes. The eval runner (Phase 14.3)
fills `run_evaluations`; this module composes the rollups + recent-bad
samples the dashboard renders. Two surfaces:

- `agent_quality(session, workspace_id, agent_name, days=7)` — one agent.
- `workspace_quality(session, workspace_id, days=7)` — all non-system agents,
  same shape per agent, plus a workspace-wide verdict_counts roll-up.

The /workspaces/me/agents/{name}/quality + /workspaces/me/quality
endpoints in main.py are thin shells around these.

`trend_7d` compares the current window's good-rate against the prior
window of the same length. Returns:
  - `delta_pp`: percentage-point change in good-rate (positive = improving).
  - `direction`: 'up' | 'down' | 'flat' | 'unknown' (unknown when either
    window has zero evals — can't divide).
The dashboard surfaces direction as a small arrow; delta_pp for hover text.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from eval_sampler import SYSTEM_AGENT_PREFIX
from models import Agent, Run, RunEvaluation


# Hard cap on the window so a misbehaving caller can't ask for "all
# evaluations ever" and blow up the query. 90 days is a reasonable
# upper bound — quality trends older than that aren't actionable.
MAX_WINDOW_DAYS = 90

# How many bad evaluations to surface on the "recent bads" list per agent.
# Keep tight so the dashboard can render inline without pagination.
RECENT_BADS_LIMIT = 5


def _normalize_days(days: Optional[int]) -> int:
    """Clamp the window to [1, MAX_WINDOW_DAYS]. None → 7."""
    if days is None:
        return 7
    try:
        n = int(days)
    except (TypeError, ValueError):
        return 7
    return max(1, min(MAX_WINDOW_DAYS, n))


def _verdict_counts_for_window(
    session: Session,
    workspace_id: str,
    *,
    agent_name: Optional[str],
    start: datetime,
    end: datetime,
) -> dict[str, int]:
    """Group-by verdict over [start, end). Empty values default to 0
    so the caller can always assume all three keys exist."""
    q = (
        select(RunEvaluation.verdict, func.count())
        .where(
            RunEvaluation.workspace_id == workspace_id,
            RunEvaluation.created_at >= start,
            RunEvaluation.created_at < end,
        )
        .group_by(RunEvaluation.verdict)
    )
    if agent_name is not None:
        q = q.where(RunEvaluation.agent_name == agent_name)
    rows = session.execute(q).all()
    counts = {"good": 0, "borderline": 0, "bad": 0}
    for verdict, count in rows:
        if verdict in counts:
            counts[verdict] = count
    return counts


def _trend(
    session: Session,
    workspace_id: str,
    *,
    agent_name: Optional[str],
    now: datetime,
    days: int,
) -> dict[str, Any]:
    """Compare current window's good-rate against the prior window of
    same length. 'unknown' when either window has zero evaluations
    (no meaningful rate to compare)."""
    window = timedelta(days=days)
    current = _verdict_counts_for_window(
        session, workspace_id, agent_name=agent_name,
        start=now - window, end=now,
    )
    prior = _verdict_counts_for_window(
        session, workspace_id, agent_name=agent_name,
        start=now - window * 2, end=now - window,
    )

    cur_total = sum(current.values())
    prior_total = sum(prior.values())
    if cur_total == 0 or prior_total == 0:
        return {"delta_pp": 0.0, "direction": "unknown"}

    cur_rate = current["good"] / cur_total
    prior_rate = prior["good"] / prior_total
    delta_pp = round((cur_rate - prior_rate) * 100, 1)
    # 1 pp dead-band so noise on small samples doesn't flicker the arrow.
    if abs(delta_pp) < 1.0:
        direction = "flat"
    elif delta_pp > 0:
        direction = "up"
    else:
        direction = "down"
    return {"delta_pp": delta_pp, "direction": direction}


def _recent_bads(
    session: Session,
    workspace_id: str,
    *,
    agent_name: Optional[str],
    start: datetime,
    end: datetime,
    limit: int = RECENT_BADS_LIMIT,
) -> list[dict[str, Any]]:
    """Most-recent bad verdicts in the window, newest first. Each row
    carries enough to render an inline "here's why" without a follow-up
    fetch."""
    q = (
        select(RunEvaluation)
        .where(
            RunEvaluation.workspace_id == workspace_id,
            RunEvaluation.verdict == "bad",
            RunEvaluation.created_at >= start,
            RunEvaluation.created_at < end,
        )
        .order_by(RunEvaluation.created_at.desc())
        .limit(limit)
    )
    if agent_name is not None:
        q = q.where(RunEvaluation.agent_name == agent_name)
    rows = session.execute(q).scalars().all()

    # For the per-run "what happened" link the dashboard wants the
    # original run's timing too — pull in one extra round-trip rather
    # than encoding it via a join (small N, simpler code).
    run_ids = [r.run_id for r in rows]
    run_meta: dict[str, datetime] = {}
    if run_ids:
        run_rows = session.execute(
            select(Run.id, Run.started_at).where(Run.id.in_(run_ids))
        ).all()
        run_meta = {rid: started for rid, started in run_rows}

    return [
        {
            "run_id": r.run_id,
            "agent_name": r.agent_name,
            "reasons": r.reasons,
            "confidence": float(r.confidence),
            "judge_model": r.judge_model,
            "created_at": r.created_at.isoformat(),
            "run_started_at": (
                run_meta[r.run_id].isoformat()
                if r.run_id in run_meta and run_meta[r.run_id] is not None
                else None
            ),
        }
        for r in rows
    ]


def agent_quality(
    session: Session,
    workspace_id: str,
    agent_name: str,
    *,
    days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Quality summary for one agent over the last `days` (default 7,
    capped at MAX_WINDOW_DAYS)."""
    days = _normalize_days(days)
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    counts = _verdict_counts_for_window(
        session, workspace_id, agent_name=agent_name,
        start=start, end=now,
    )
    return {
        "agent_name": agent_name,
        "days": days,
        "verdict_counts": counts,
        "total_evaluations": sum(counts.values()),
        "recent_bads": _recent_bads(
            session, workspace_id, agent_name=agent_name,
            start=start, end=now,
        ),
        "trend": _trend(
            session, workspace_id, agent_name=agent_name,
            now=now, days=days,
        ),
    }


def workspace_quality(
    session: Session,
    workspace_id: str,
    *,
    days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Per-agent + workspace-wide quality rollup.

    Returns:
        - `per_agent`: list of {agent_name, verdict_counts,
          total_evaluations, trend} sorted by agent_name.
        - `verdict_counts`: rollup across all agents.
        - `recent_bads`: top-N bads workspace-wide so the home page
          can surface "Atlas had a bad run an hour ago" without
          re-querying per agent.
        - `days`: echo of the window the caller chose.
    """
    days = _normalize_days(days)
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    agent_names = session.execute(
        select(Agent.name)
        .where(Agent.workspace_id == workspace_id)
        .order_by(Agent.name)
    ).scalars().all()
    agent_names = [n for n in agent_names if not n.startswith(SYSTEM_AGENT_PREFIX)]

    per_agent: list[dict[str, Any]] = []
    for name in agent_names:
        counts = _verdict_counts_for_window(
            session, workspace_id, agent_name=name,
            start=start, end=now,
        )
        per_agent.append({
            "agent_name": name,
            "verdict_counts": counts,
            "total_evaluations": sum(counts.values()),
            "trend": _trend(
                session, workspace_id, agent_name=name,
                now=now, days=days,
            ),
        })

    workspace_counts = _verdict_counts_for_window(
        session, workspace_id, agent_name=None,
        start=start, end=now,
    )
    return {
        "days": days,
        "verdict_counts": workspace_counts,
        "total_evaluations": sum(workspace_counts.values()),
        "per_agent": per_agent,
        "recent_bads": _recent_bads(
            session, workspace_id, agent_name=None,
            start=start, end=now,
        ),
    }
