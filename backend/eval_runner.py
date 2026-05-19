"""Phase 14.3: periodic eval job runner.

Wires the pure sampler from `eval_sampler.py` into the in-process job
runner from `jobs.py` (the same SKIP LOCKED + asyncio.to_thread shape
12C.6.2 introduced for /agents/generate). One `generation_jobs` row
of kind `eval_runs` per workspace per cycle; the handler picks runs
to evaluate, calls Claude as the judge, writes `run_evaluations`
rows, and lands judge spend on the `lightsei.system` synthetic agent
so the workspace monthly budget gate covers it.

Two surfaces:

1. `run_eval_job(session, workspace_id, payload) -> dict` — the
   handler the dispatch registry calls. Pre-checks workspace state
   (anthropic key, budget cap), samples, judges, persists, returns
   a summary dict. Failures inside a single judge call don't stop
   the cycle — they bump `errored` and the next sample is tried.

2. `start_eval_cron()` — startup hook. Loops forever, dropping one
   `eval_runs` job per workspace per `LIGHTSEI_EVAL_INTERVAL_S`
   (default 3600s = 1 hour). Pattern mirrors `jobs.start_runner` /
   `jobs.stop_runner` so the FastAPI startup + shutdown wiring is
   symmetric.

Design choices (sampling rate, judge model, judge depth, cost cap)
live in TASKS.md "Phase 14" intro — locked 2026-05-17.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import eval_sampler
import jobs
import secrets_crypto
from cost import workspace_cost_mtd
from db import SessionLocal, ensure_agent
from models import Run, RunEvaluation, Workspace, WorkspaceSecret
from pricing import compute_cost_usd

logger = logging.getLogger("lightsei.eval")

DEFAULT_EVAL_INTERVAL_S = 3600  # 1 hour


# ---------- Handler ---------- #


def run_eval_job(
    session: Session,
    workspace_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Sample + judge + persist for one workspace.

    Mirrors `agent_generator.run_agent_generation_job` shape:
    pre-checks, then a per-item loop that's robust to individual
    failures. Returns a summary the runner persists as
    `result_payload`; the dashboard surfaces it later if useful.
    """
    # Local imports to mirror agent_generator's pattern + sidestep any
    # import-time circulars when the runner spawns this on a thread.
    import anthropic

    # Phase 17.5: paywall gate. Eval is background work — return a
    # clean skip summary rather than raising 402 (which would just
    # mark the job 'failed' with no actionable signal). Same shape as
    # the over_budget skip below.
    from models import Workspace as _Workspace
    _ws = session.get(_Workspace, workspace_id)
    if (
        _ws is not None
        and _ws.plan_tier == "free"
        and (_ws.free_credits_remaining_usd or Decimal("0")) <= Decimal("0")
    ):
        return {
            "sampled": 0,
            "evaluated": 0,
            "errored": 0,
            "skipped_reason": "out_of_credits",
        }

    # 1. Workspace's Anthropic key.
    secret_row = session.get(WorkspaceSecret, (workspace_id, "ANTHROPIC_API_KEY"))
    if secret_row is None:
        return {
            "sampled": 0,
            "evaluated": 0,
            "errored": 0,
            "skipped_reason": "no_anthropic_key",
        }
    try:
        anthropic_key = secrets_crypto.decrypt(secret_row.encrypted_value)
    except Exception as exc:
        logger.exception("eval: failed to decrypt ANTHROPIC_API_KEY")
        return {
            "sampled": 0,
            "evaluated": 0,
            "errored": 0,
            "skipped_reason": f"decrypt_failed: {exc!r}",
        }

    # 2. Budget gate — same shape as agent_generator. Workspace cap
    # already covers generation + judge together by design (cost lands
    # on lightsei.system for both).
    workspace = session.get(Workspace, workspace_id)
    if workspace is not None and workspace.budget_usd_monthly is not None:
        cost = workspace_cost_mtd(session, workspace_id)
        used = float(cost.get("total_usd") or 0)
        cap = float(workspace.budget_usd_monthly)
        if cap > 0 and used >= cap:
            return {
                "sampled": 0,
                "evaluated": 0,
                "errored": 0,
                "skipped_reason": f"over_budget ({used:.4f} >= {cap:.4f})",
            }

    # 3. Pick samples. Empty pool is the steady-state case — return
    # without spending anything (and without writing a cost Run row).
    per_agent = payload.get("per_agent")  # optional override; tests use it
    if per_agent is not None:
        run_ids = eval_sampler.pick_sample(
            session, workspace_id, per_agent=int(per_agent)
        )
    else:
        run_ids = eval_sampler.pick_sample(session, workspace_id)
    if not run_ids:
        return {
            "sampled": 0,
            "evaluated": 0,
            "errored": 0,
            "skipped_reason": "no_samples",
        }

    # Close the read transaction before the judge calls. The preflight
    # checks and sampler open an implicit Postgres transaction; holding
    # it idle during one or more Anthropic calls trips Railway's
    # idle-in-transaction timeout and loses the eval writes.
    session.commit()

    # 4. Judge loop. max_retries kept modest (2) so a transient 529 on
    # one sample doesn't burn the whole cycle's wall time — the sample
    # will be a candidate again next cycle.
    client = anthropic.Anthropic(api_key=anthropic_key, max_retries=2)

    evaluated = 0
    errored = 0
    total_tokens_in = 0
    total_tokens_out = 0

    for run_id in run_ids:
        try:
            prompt = eval_sampler.build_judge_prompt(session, run_id)
        except ValueError as exc:
            # Run missing or agent deleted between sample + judge.
            logger.info("eval: skipping run_id=%s: %s", run_id, exc)
            errored += 1
            continue

        try:
            resp = client.messages.create(**prompt)
        except anthropic.APIError as exc:
            logger.warning("eval: Anthropic error on run_id=%s: %s", run_id, exc)
            errored += 1
            continue

        verdict = _extract_verdict(resp)
        if verdict is None:
            logger.warning(
                "eval: model did not call submit_verdict on run_id=%s "
                "(stop_reason=%s)",
                run_id,
                getattr(resp, "stop_reason", "?"),
            )
            errored += 1
            continue

        # Token usage for cost accounting + persisting per-row.
        usage = getattr(resp, "usage", None)
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
        # Sonnet 4.6 is the judge model per the locked design choice.
        # Use the resolved model from the response when present (Anthropic
        # may echo a more specific id), since pricing is keyed on the
        # base model name and the base price applies either way.
        judge_model = getattr(resp, "model", None) or eval_sampler.JUDGE_MODEL
        cost_usd = compute_cost_usd(judge_model, tokens_in, tokens_out)
        total_tokens_in += tokens_in
        total_tokens_out += tokens_out

        # Look up the run's agent_name without an extra round-trip: we
        # already filtered to this workspace in pick_sample, but the
        # agent_name needs to land on the RunEvaluation row directly so
        # the dashboard's `(workspace, agent, created_at DESC)` index
        # answers /agents/{name}/quality in one scan.
        run = session.get(Run, run_id)
        if run is None:
            # Vanishingly unlikely (runs would have been deleted between
            # sample and write), but the FK would 23503 anyway — be
            # explicit about the skip.
            errored += 1
            continue

        session.add(
            RunEvaluation(
                id=str(uuid.uuid4()),
                run_id=run_id,
                workspace_id=workspace_id,
                agent_name=run.agent_name,
                judge_model=judge_model,
                verdict=verdict["verdict"],
                reasons=verdict["reasons"],
                confidence=Decimal(str(verdict["confidence"])),
                judge_tokens_in=tokens_in,
                judge_tokens_out=tokens_out,
                judge_cost_usd=Decimal(format(cost_usd, ".6f")),
                created_at=datetime.now(timezone.utc),
            )
        )
        evaluated += 1

    # 5. Spend lands on lightsei.system so /cost reflects judge calls
    # the same way it reflects generation calls. One Run row per
    # cycle (not per evaluation) — the per-row spend is already on the
    # RunEvaluation row for fine-grained accounting; this row is the
    # workspace-rollup attribution.
    if total_tokens_in > 0 or total_tokens_out > 0:
        now_ts = datetime.now(timezone.utc)
        ensure_agent(session, workspace_id, "lightsei.system", now_ts)
        total_cost = compute_cost_usd(
            eval_sampler.JUDGE_MODEL, total_tokens_in, total_tokens_out
        )
        session.add(
            Run(
                id=str(uuid.uuid4()),
                workspace_id=workspace_id,
                agent_name="lightsei.system",
                started_at=now_ts,
                ended_at=now_ts,
                cost_usd=Decimal(format(total_cost, ".6f")),
            )
        )
        # Phase 17.5: free credits decrement.
        import billing_gate as _bg
        _bg.decrement_free_credits(
            session, workspace_id, Decimal(format(total_cost, ".6f")),
        )

    session.commit()
    return {
        "sampled": len(run_ids),
        "evaluated": evaluated,
        "errored": errored,
        "tokens_in": total_tokens_in,
        "tokens_out": total_tokens_out,
    }


def _extract_verdict(resp: Any) -> Optional[dict[str, Any]]:
    """Pull the {verdict, reasons, confidence} dict out of the
    response's `submit_verdict` tool_use block. Returns None if the
    model returned plain text instead of calling the tool — schema
    enforcement happens upstream in the SUBMIT_VERDICT_TOOL definition,
    so a successful call already conforms."""
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) != "tool_use":
            continue
        if getattr(block, "name", None) != "submit_verdict":
            continue
        inp = getattr(block, "input", None)
        if isinstance(inp, dict):
            return inp
    return None


# ---------- Periodic enqueuer ---------- #


def _eval_interval_s() -> float:
    raw = os.environ.get("LIGHTSEI_EVAL_INTERVAL_S")
    if raw is None:
        return float(DEFAULT_EVAL_INTERVAL_S)
    try:
        n = float(raw)
    except ValueError:
        return float(DEFAULT_EVAL_INTERVAL_S)
    # Floor at 10s so a misconfigured env can't hammer Anthropic.
    return max(10.0, n)


def enqueue_eval_job_for_workspace(
    session: Session, *, workspace_id: str
) -> str:
    """Drop one `eval_runs` row for a workspace. Returns the job_id.

    Caller commits the session. The job runner picks it up on the next
    poll (≤ 500ms in prod, ≤ 10ms in tests).
    """
    job_id = str(uuid.uuid4())
    jobs.enqueue_job(
        session,
        job_id=job_id,
        workspace_id=workspace_id,
        kind="eval_runs",
        request_payload={},
    )
    return job_id


def _enqueue_eval_jobs_for_all_workspaces() -> int:
    """One row per workspace per cycle. Returns the count enqueued."""
    s = SessionLocal()
    try:
        workspace_ids = s.execute(select(Workspace.id)).scalars().all()
        for ws_id in workspace_ids:
            enqueue_eval_job_for_workspace(s, workspace_id=ws_id)
        s.commit()
        return len(workspace_ids)
    finally:
        s.close()


_cron_task: Optional[asyncio.Task] = None


async def _eval_cron_loop() -> None:
    """Forever loop. Enqueues immediately on the first iteration so a
    fresh deploy gets evals within minutes rather than waiting a full
    hour; then sleeps for the configured interval between cycles."""
    logger.info("eval: cron started, interval=%.0fs", _eval_interval_s())
    try:
        while True:
            try:
                n = await asyncio.to_thread(
                    _enqueue_eval_jobs_for_all_workspaces
                )
                logger.info("eval: enqueued %d workspace job(s)", n)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("eval: cron enqueue failed")
            await asyncio.sleep(_eval_interval_s())
    except asyncio.CancelledError:
        logger.info("eval: cron cancelled, exiting")
        raise


def start_eval_cron() -> None:
    """Start the periodic enqueuer task. Idempotent — a second call
    while the task is alive is a no-op, so dev-reload doesn't stack
    cron tasks."""
    global _cron_task
    if _cron_task is not None and not _cron_task.done():
        return
    loop = asyncio.get_event_loop()
    _cron_task = loop.create_task(_eval_cron_loop(), name="lightsei-eval-cron")


async def stop_eval_cron() -> None:
    """Cancel + await the cron task. Safe to call repeatedly."""
    global _cron_task
    if _cron_task is None:
        return
    _cron_task.cancel()
    try:
        await _cron_task
    except (asyncio.CancelledError, Exception):
        pass
    _cron_task = None


# ---------- Handler registration ---------- #


def _register() -> None:
    jobs.register_handler("eval_runs", run_eval_job)


_register()
