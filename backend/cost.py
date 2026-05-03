"""Cost rollup helpers shared by /agents/{name}/cost and the cost-cap rule."""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from pricing import compute_cost_usd


def utc_day_start() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def utc_day_start_iso() -> str:
    return utc_day_start().isoformat()


def agent_cost_since(
    session: Session,
    workspace_id: str,
    agent_name: str,
    since: datetime,
) -> dict[str, Any]:
    """Total + per-model cost for `agent_name` in `workspace_id` since `since`."""
    rows = session.execute(
        text(
            """
            SELECT
                payload ->> 'model' AS model,
                COALESCE((payload ->> 'input_tokens')::int, 0) AS input_tokens,
                COALESCE((payload ->> 'output_tokens')::int, 0) AS output_tokens
            FROM events
            WHERE workspace_id = :wsid
              AND agent_name = :agent_name
              AND kind = 'llm_call_completed'
              AND timestamp >= :since
            """
        ),
        {"wsid": workspace_id, "agent_name": agent_name, "since": since},
    ).all()

    by_model: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    total_input = 0
    total_output = 0

    for r in rows:
        model = r.model or "unknown"
        input_tokens = r.input_tokens or 0
        output_tokens = r.output_tokens or 0
        priced_model = model if model != "unknown" else None
        cost = compute_cost_usd(priced_model, input_tokens, output_tokens)
        bucket = by_model.setdefault(
            model,
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
        )
        bucket["calls"] += 1
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["cost_usd"] += cost
        total_cost += cost
        total_input += input_tokens
        total_output += output_tokens

    return {
        "calls": len(rows),
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": round(total_cost, 6),
        "by_model": {
            m: {**v, "cost_usd": round(v["cost_usd"], 6)}
            for m, v in by_model.items()
        },
    }


def agent_cost_today(
    session: Session, workspace_id: str, agent_name: str
) -> float:
    return agent_cost_since(session, workspace_id, agent_name, utc_day_start())[
        "cost_usd"
    ]


# ---------- Phase 11B.1: workspace-level cost rollups ---------- #


def utc_month_start() -> datetime:
    """First instant of the current UTC month."""
    now = datetime.now(timezone.utc)
    return now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )


def _days_in_month(dt: datetime) -> int:
    """Days in the month containing `dt`. Pure arithmetic — no calendar
    library — because we only need it for the projection denominator."""
    if dt.month == 12:
        next_month_start = dt.replace(
            year=dt.year + 1, month=1, day=1
        )
    else:
        next_month_start = dt.replace(month=dt.month + 1, day=1)
    return (next_month_start - dt.replace(day=1)).days


def workspace_cost_mtd(
    session: Session, workspace_id: str
) -> dict[str, Any]:
    """Aggregate workspace spend month-to-date.

    Reads from `runs.cost_usd` directly — far cheaper than re-summing
    events × pricing on every dashboard render. Returns:

      {
        mtd_usd:           total spend since first of this UTC month,
        projected_eom_usd: naive linear extrapolation,
        run_count:         number of runs that contributed,
        by_agent:          [{agent_name, mtd_usd, run_count, last_run_at}],
        budget_usd_monthly: workspace cap or None,
        budget_used_pct:   mtd / cap * 100, or None if no cap,
      }
    """
    from models import Workspace  # local import: avoids circular imports

    month_start = utc_month_start()
    now = datetime.now(timezone.utc)

    # Per-agent rollup. One scan of runs filtered by (workspace, started_at).
    agent_rows = session.execute(
        text(
            """
            SELECT
                agent_name,
                SUM(cost_usd) AS mtd_usd,
                COUNT(*) AS run_count,
                MAX(started_at) AS last_run_at
            FROM runs
            WHERE workspace_id = :wsid
              AND started_at >= :month_start
            GROUP BY agent_name
            ORDER BY SUM(cost_usd) DESC
            """
        ),
        {"wsid": workspace_id, "month_start": month_start},
    ).all()

    by_agent = [
        {
            "agent_name": r.agent_name,
            "mtd_usd": float(round(r.mtd_usd or 0, 6)),
            "run_count": int(r.run_count or 0),
            "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
        }
        for r in agent_rows
    ]
    mtd = sum(a["mtd_usd"] for a in by_agent)
    run_count = sum(a["run_count"] for a in by_agent)

    # Naive linear extrapolation. Day-of-month = 1 means we can't
    # extrapolate from a single sample, so we just echo the MTD as the
    # projection until we have more data. Same answer applies for runs
    # that haven't started yet (mtd == 0).
    days_so_far = max(1, (now - month_start).days + 1)
    days_in_month = _days_in_month(now)
    projected_eom = (
        mtd if days_so_far <= 1 or mtd == 0
        else round(mtd / days_so_far * days_in_month, 6)
    )

    ws = session.get(Workspace, workspace_id)
    budget = ws.budget_usd_monthly if ws is not None else None
    budget_pct: Optional[float] = None
    if budget is not None and float(budget) > 0:
        budget_pct = round(mtd / float(budget) * 100, 2)

    # by_model breakdown — single events scan filtered to llm_call_completed
    # since the migration's backfill already used the same pricing the
    # rollup uses, so the by_agent total and the by_model total agree to
    # within rounding.
    model_rows = session.execute(
        text(
            """
            SELECT
                COALESCE(payload->>'model', 'unknown') AS model,
                COUNT(*) AS calls,
                SUM(COALESCE((payload->>'input_tokens')::int, 0)) AS in_tok,
                SUM(COALESCE((payload->>'output_tokens')::int, 0)) AS out_tok,
                SUM(
                    COALESCE((payload->>'input_tokens')::numeric, 0)
                      * COALESCE(mp.input_per_million_usd, 0) / 1000000.0
                    +
                    COALESCE((payload->>'output_tokens')::numeric, 0)
                      * COALESCE(mp.output_per_million_usd, 0) / 1000000.0
                ) AS cost
            FROM events e
            LEFT JOIN model_pricing mp
              ON mp.model = e.payload->>'model'
            WHERE e.workspace_id = :wsid
              AND e.kind = 'llm_call_completed'
              AND e.timestamp >= :month_start
            GROUP BY 1
            ORDER BY cost DESC NULLS LAST
            """
        ),
        {"wsid": workspace_id, "month_start": month_start},
    ).all()

    by_model = [
        {
            "model": r.model,
            "calls": int(r.calls or 0),
            "input_tokens": int(r.in_tok or 0),
            "output_tokens": int(r.out_tok or 0),
            "mtd_usd": float(round(r.cost or 0, 6)),
        }
        for r in model_rows
    ]

    return {
        "mtd_usd": float(round(mtd, 6)),
        "projected_eom_usd": float(projected_eom),
        "run_count": run_count,
        "by_agent": by_agent,
        "by_model": by_model,
        "budget_usd_monthly": float(budget) if budget is not None else None,
        "budget_used_pct": budget_pct,
        "month_start": month_start.isoformat(),
        "as_of": now.isoformat(),
    }


def add_run_cost_from_event(
    session: Session, run_id: str, payload: dict[str, Any]
) -> float:
    """Increment `runs.cost_usd` by the cost implied by one
    `llm_call_completed` event payload. Returns the delta in USD.

    Called from /events at ingest time. Skips silently when the run
    row is missing (race with a deleted run, or an event for a run
    we never created — both are rare and shouldn't crash ingest).
    """
    from models import Run  # local import: avoids circular imports

    model = payload.get("model")
    in_tok = payload.get("input_tokens")
    out_tok = payload.get("output_tokens")
    delta = compute_cost_usd(model, in_tok, out_tok)
    if delta <= 0:
        return 0.0
    run = session.get(Run, run_id)
    if run is None:
        return 0.0
    # Decimal + float won't auto-coerce in SQLAlchemy; build a Decimal
    # from the float string repr so we don't get binary-float drift.
    from decimal import Decimal as _Decimal
    run.cost_usd = (run.cost_usd or _Decimal("0")) + _Decimal(
        format(delta, ".6f")
    )
    return delta


# Optional import gate so tests that import cost.py for token-only
# helpers don't pull the Workspace / Run models eagerly.
try:
    from typing import Optional  # noqa: F401  (used in workspace_cost_mtd)
except ImportError:  # pragma: no cover
    pass
