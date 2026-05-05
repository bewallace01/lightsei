"""Phase 12D.1: cost-intelligence insights computed from existing data.

Pure analytics module — no I/O beyond the SQLAlchemy session passed in.
The endpoint in main.py just calls these and serializes the result.

Each insight returns a small dict with at least:
  - `kind`:    short machine-readable id (e.g. "cache_skip_savings")
  - `headline`:short user-facing one-liner
  - `detail`:  the numbers behind the headline
  - `apply`:   optional dict pointing at a one-click fix
                ({"href": "/agents/polaris", "label": "...", "patch": {...}})

Returning a homogeneous shape makes the dashboard's renderer trivial:
map over the list, render the headline + detail + maybe an action link.

Time windows: most insights look at the last 30 days because the
free-tier of any decent observability bucket has plenty of rows there.
Per-trigger ROI uses 7 days since rate-of-invocation tracking is more
useful on a tighter window.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from pricing import PRICING


# ---------- Helpers ---------- #


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _model_cost_usd(
    model: str | None,
    input_tokens: int | float | None,
    output_tokens: int | float | None,
) -> float:
    """Reuse pricing.py's table; any unknown model is $0 (matches
    cost.py's choice of failing-quietly rather than guessing)."""
    if not model:
        return 0.0
    prices = PRICING.get(model)
    if prices is None:
        return 0.0
    in_per_m, out_per_m = prices
    return ((input_tokens or 0) * in_per_m + (output_tokens or 0) * out_per_m) / 1_000_000.0


# ---------- Insight 1: cache-skip savings ---------- #


def cache_skip_savings(session: Session, workspace_id: str) -> dict[str, Any]:
    """Count `polaris.tick_skipped` events with reason="docs unchanged"
    in the last 30 days. Multiply by the median cost of a non-skip
    polaris.plan event to estimate dollars Polaris's hash cache saved.

    Median over mean to handle the long tail of huge-context outliers.
    """
    cutoff = _now() - timedelta(days=30)

    skip_count = session.execute(
        text(
            """
            SELECT COUNT(*) AS n
            FROM events
            WHERE workspace_id = :wsid
              AND kind = 'polaris.tick_skipped'
              AND timestamp >= :cutoff
              AND payload->>'reason' = 'docs unchanged'
            """
        ),
        {"wsid": workspace_id, "cutoff": cutoff},
    ).scalar_one()

    plan_rows = session.execute(
        text(
            """
            SELECT
              payload->>'model' AS model,
              (payload->>'tokens_in')::int AS tokens_in,
              (payload->>'tokens_out')::int AS tokens_out
            FROM events
            WHERE workspace_id = :wsid
              AND kind = 'polaris.plan'
              AND timestamp >= :cutoff
              AND payload ? 'tokens_in'
            """
        ),
        {"wsid": workspace_id, "cutoff": cutoff},
    ).all()

    plan_costs = [
        _model_cost_usd(r.model, r.tokens_in, r.tokens_out)
        for r in plan_rows
    ]
    plan_costs = [c for c in plan_costs if c > 0]
    median_cost = statistics.median(plan_costs) if plan_costs else 0.0

    estimated_saved = float(skip_count) * median_cost

    return {
        "kind": "cache_skip_savings",
        "headline": (
            f"Polaris's hash cache skipped {skip_count} ticks in the last 30 days, "
            f"saving an estimated ${estimated_saved:.2f}."
        ),
        "detail": {
            "skipped_ticks": int(skip_count),
            "median_plan_cost_usd": round(median_cost, 4),
            "estimated_saved_usd": round(estimated_saved, 4),
            "window_days": 30,
        },
        "apply": None,
    }


# ---------- Insight 2: plan volatility ---------- #


def _plan_signature(payload: dict) -> str:
    """Hash a polaris.plan's summary + next_actions (the two fields the
    user actually reads). Non-content noise (model id, tokens, raw text
    blob) is excluded so meaningful equality survives provider swaps.
    """
    summary = (payload.get("summary") or "").strip()
    next_actions = payload.get("next_actions") or []
    # JSON-canonicalize next_actions so dict-key ordering doesn't bust equality.
    canonical = json.dumps(next_actions, sort_keys=True, default=str)
    return f"{len(summary)}|{summary[:200]}|{canonical[:1000]}"


def plan_volatility(session: Session, workspace_id: str) -> dict[str, Any]:
    """Look at the last 10 polaris.plan events. Count consecutive plans
    with identical signatures starting from the most recent. If the
    streak is ≥3, surface as "consider a longer tick interval."
    """
    rows = session.execute(
        text(
            """
            SELECT id, payload
            FROM events
            WHERE workspace_id = :wsid
              AND kind = 'polaris.plan'
            ORDER BY timestamp DESC
            LIMIT 10
            """
        ),
        {"wsid": workspace_id},
    ).all()

    signatures = [_plan_signature(r.payload or {}) for r in rows]
    streak = 0
    if signatures:
        first = signatures[0]
        for sig in signatures:
            if sig == first:
                streak += 1
            else:
                break

    apply_action: dict[str, Any] | None = None
    headline: str
    if streak >= 3:
        headline = (
            f"The last {streak} Polaris plans are byte-identical. "
            "Consider doubling your tick interval to skip work that "
            "produces the same answer."
        )
        apply_action = {
            "href": "/agents/polaris",
            "label": "Tune Polaris's schedule →",
        }
    else:
        headline = (
            "Polaris's recent plans are diverging — schedule looks healthy."
        )

    return {
        "kind": "plan_volatility",
        "headline": headline,
        "detail": {
            "plans_compared": len(signatures),
            "identical_streak": streak,
        },
        "apply": apply_action,
    }


# ---------- Insight 3: model-tier mismatch ---------- #

# Cheapest reasonable downgrade per "expensive" model. Suggests the
# next-tier-down within the same provider so swaps are conservative.
# Empty value = no recommendation (already cheap or unknown).
_TIER_DOWNGRADE: dict[str, str] = {
    # Anthropic
    "claude-opus-4-7": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-haiku-4-5",
    # OpenAI
    "gpt-4": "gpt-4o",
    "gpt-4o": "gpt-4o-mini",
    "gpt-4-turbo": "gpt-4o",
    "o1": "o1-mini",
    # Google
    "gemini-1.5-pro": "gemini-1.5-flash",
    "gemini-2.5-pro": "gemini-2.5-flash",
}


def model_tier_mismatch(session: Session, workspace_id: str) -> list[dict[str, Any]]:
    """Per agent: look at the input-token distribution of recent
    llm_call_completed events. If the MAX input over the last 30 days
    is under 16k tokens AND the agent is using an expensive model with
    a known downgrade, recommend the cheaper tier.

    Conservative on purpose: a single tail call at 80k tokens is enough
    to suppress the recommendation. A bad downgrade that breaks a real
    workload is much worse UX than a missed recommendation — the user
    can always swap manually if they know better.

    Only suggest swaps within the same provider, and only when the
    cheaper option is on PRICING (so projected savings are grounded in
    real numbers, not hand-waved).
    """
    cutoff = _now() - timedelta(days=30)

    rows = session.execute(
        text(
            """
            SELECT
              agent_name,
              payload->>'model' AS model,
              (payload->>'input_tokens')::int AS tin,
              (payload->>'output_tokens')::int AS tout
            FROM events
            WHERE workspace_id = :wsid
              AND kind = 'llm_call_completed'
              AND timestamp >= :cutoff
              AND payload ? 'input_tokens'
            """
        ),
        {"wsid": workspace_id, "cutoff": cutoff},
    ).all()

    # Bucket by (agent, model).
    buckets: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for r in rows:
        if not r.model:
            continue
        buckets.setdefault((r.agent_name, r.model), []).append((r.tin or 0, r.tout or 0))

    insights: list[dict[str, Any]] = []
    for (agent, model), tokens in buckets.items():
        downgrade = _TIER_DOWNGRADE.get(model)
        if downgrade is None or downgrade not in PRICING:
            continue

        ins = [t[0] for t in tokens]
        if not ins:
            continue
        max_in = max(ins)
        if max_in > 16000:
            # Even one call needed the bigger model; don't recommend
            # a downgrade that might break that workload.
            continue

        # Projected cost on the cheaper model.
        current_cost = sum(_model_cost_usd(model, i, o) for i, o in tokens)
        projected_cost = sum(_model_cost_usd(downgrade, i, o) for i, o in tokens)
        if current_cost <= 0 or projected_cost >= current_cost:
            continue

        savings_pct = round(
            100 * (current_cost - projected_cost) / current_cost, 1
        )
        insights.append(
            {
                "kind": "model_tier_mismatch",
                "headline": (
                    f"{agent} on {model} could likely run on {downgrade} — "
                    f"observed inputs always under 16k tokens. "
                    f"Projected savings: ${current_cost - projected_cost:.2f} "
                    f"({savings_pct}%) over the last 30 days."
                ),
                "detail": {
                    "agent": agent,
                    "current_model": model,
                    "suggested_model": downgrade,
                    "calls_observed": len(tokens),
                    "max_input_tokens": max_in,
                    "current_cost_usd": round(current_cost, 4),
                    "projected_cost_usd": round(projected_cost, 4),
                    "savings_usd": round(current_cost - projected_cost, 4),
                    "savings_pct": savings_pct,
                },
                "apply": {
                    "href": f"/agents/{agent}",
                    "label": f"Switch {agent} to {downgrade} →",
                    "patch": {"model": downgrade},
                },
            }
        )
    return insights


# ---------- Insight 4: failed-call cost ---------- #


def failed_call_cost(session: Session, workspace_id: str) -> dict[str, Any]:
    """Tally `llm_call_failed` event volume + estimated input-token cost
    in the last 30 days. Failed calls still bill input tokens (and
    sometimes partial output) at most providers, so this is real money
    that produced nothing."""
    cutoff = _now() - timedelta(days=30)

    rows = session.execute(
        text(
            """
            SELECT
              payload->>'model' AS model,
              (payload->>'input_tokens')::int AS tin,
              payload->>'error' AS err
            FROM events
            WHERE workspace_id = :wsid
              AND kind = 'llm_call_failed'
              AND timestamp >= :cutoff
            """
        ),
        {"wsid": workspace_id, "cutoff": cutoff},
    ).all()

    total_cost = 0.0
    by_reason: dict[str, int] = {}
    for r in rows:
        # Count input-tokens-only at the model's input rate. Output is
        # usually 0 on a failure but if a partial generation slipped
        # through it'd already have landed as `llm_call_completed`.
        cost = _model_cost_usd(r.model, r.tin, 0)
        total_cost += cost
        # Simplify error strings by trimming to the exception class name.
        reason = (r.err or "unknown").split("(")[0].split(":")[0].strip() or "unknown"
        by_reason[reason] = by_reason.get(reason, 0) + 1

    top_reasons = sorted(
        ({"reason": k, "count": v} for k, v in by_reason.items()),
        key=lambda d: -d["count"],
    )[:5]

    return {
        "kind": "failed_call_cost",
        "headline": (
            f"{len(rows)} failed LLM calls in the last 30 days, "
            f"costing an estimated ${total_cost:.2f} for input tokens "
            "the model never returned anything useful for."
        ) if rows else "No failed LLM calls in the last 30 days.",
        "detail": {
            "failed_call_count": len(rows),
            "estimated_wasted_usd": round(total_cost, 4),
            "top_reasons": top_reasons,
            "window_days": 30,
        },
        "apply": None,
    }


# ---------- Insight 5: per-trigger ROI ---------- #


def per_trigger_roi(session: Session, workspace_id: str) -> list[dict[str, Any]]:
    """Per agent: rate of invocation × % "useful" outcome.

    Useful = the agent's run produced an event other than `run_started`,
    `run_ended`, or a `polaris.tick_skipped`. So purely cache-skipped
    polaris ticks count against the useful rate; LLM calls + dispatched
    commands + custom emits all count for it.

    Bots whose useful-rate is below 25% over 7 days are candidates for
    a smarter trigger filter (e.g. polaris's POLARIS_PUSH_RULES) or a
    longer tick interval.
    """
    cutoff = _now() - timedelta(days=7)

    # Total runs per agent.
    run_rows = session.execute(
        text(
            """
            SELECT agent_name, COUNT(*) AS n
            FROM runs
            WHERE workspace_id = :wsid
              AND started_at >= :cutoff
            GROUP BY agent_name
            """
        ),
        {"wsid": workspace_id, "cutoff": cutoff},
    ).all()

    # Useful runs: at least one event whose kind isn't a marker / skip.
    useful_rows = session.execute(
        text(
            """
            SELECT agent_name, COUNT(DISTINCT run_id) AS n
            FROM events
            WHERE workspace_id = :wsid
              AND timestamp >= :cutoff
              AND kind NOT IN ('run_started', 'run_ended', 'polaris.tick_skipped',
                                'llm_call_started')
            GROUP BY agent_name
            """
        ),
        {"wsid": workspace_id, "cutoff": cutoff},
    ).all()

    useful_by_agent = {r.agent_name: int(r.n) for r in useful_rows}

    insights: list[dict[str, Any]] = []
    for r in run_rows:
        total = int(r.n)
        useful = useful_by_agent.get(r.agent_name, 0)
        if total < 5:
            continue  # too few samples to trust the rate
        pct = 100.0 * useful / total
        if pct >= 25:
            continue  # healthy
        insights.append(
            {
                "kind": "per_trigger_roi",
                "headline": (
                    f"{r.agent_name} produced useful output on only "
                    f"{useful}/{total} runs ({pct:.0f}%) in the last 7 days. "
                    "Consider a tighter trigger filter or a longer tick interval."
                ),
                "detail": {
                    "agent": r.agent_name,
                    "total_runs": total,
                    "useful_runs": useful,
                    "useful_pct": round(pct, 1),
                    "window_days": 7,
                },
                "apply": {
                    "href": f"/agents/{r.agent_name}",
                    "label": f"Tune {r.agent_name}'s schedule / filter →",
                },
            }
        )
    return insights


# ---------- Top-level ---------- #


def all_insights(session: Session, workspace_id: str) -> dict[str, Any]:
    """Run every insight and return the homogeneous list.

    Order: high-signal optimizations first (model tier swaps, useful-rate
    flags), then read-only audit (cache savings, failed-call cost),
    then the schedule volatility check. Empty-result insights are
    omitted so the page stays scannable.
    """
    items: list[dict[str, Any]] = []
    items.extend(model_tier_mismatch(session, workspace_id))
    items.extend(per_trigger_roi(session, workspace_id))
    items.append(cache_skip_savings(session, workspace_id))
    items.append(failed_call_cost(session, workspace_id))
    items.append(plan_volatility(session, workspace_id))
    return {"insights": items}
