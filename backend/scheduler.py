"""Phase 22.3: cron-trigger scheduler.

Sibling to `backend/jobs.py`. The jobs runner picks up `scheduled_run`
rows that this module enqueues; both run as asyncio tasks on the
backend's startup hook so they share the same event loop and DB pool.

Loop shape:

  while not cancelled:
      now = utcnow()
      with session: tick(session, now)
      sleep(TICK_INTERVAL_S)

Each tick:

  1. SELECT triggers WHERE enabled AND kind='cron'
     AND next_run_at <= now AND next_run_at >= now - GRACE_WINDOW
     FOR UPDATE SKIP LOCKED.
  2. For each row: enqueue a `scheduled_run` job carrying
     {trigger_id}; advance the trigger's next_run_at via croniter;
     stamp last_run_at = now.
  3. Commit.

`SELECT ... FOR UPDATE SKIP LOCKED` keeps two scheduler instances
from double-firing the same trigger. We run one instance today, but
the pattern is correct for the day we scale out.

Startup-time sweep (`fast_forward_stale_triggers`): triggers whose
`next_run_at` is older than the grace window get their next_run_at
fast-forwarded to the next future fire without enqueueing a run.
Prevents the "worker was down for a day, dumps 1000 backfill runs
when it wakes up" stampede.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

import jobs as _jobs
import triggers as _trigmod
from db import SessionLocal

logger = logging.getLogger("lightsei.scheduler")


# Tick cadence. Override via env so tests + local dev can dial it
# down to a few hundred ms without modifying source.
TICK_INTERVAL_S = float(os.environ.get("LIGHTSEI_SCHEDULER_TICK_S", "60"))

# How far back the scheduler is willing to backfill a missed fire.
# A trigger whose next_run_at is older than this lookbehind gets
# fast-forwarded (no run) instead of fired. 24h is generous enough
# that a normal redeploy + outage window catches up cleanly.
GRACE_WINDOW = timedelta(hours=24)


def tick(session: Session, now: datetime) -> int:
    """Run one scheduler tick. Returns the number of triggers fired.

    Pure-ish: takes the session + clock so tests can drive deterministic
    scenarios without monkey-patching utcnow(). Caller is responsible
    for opening + closing the session; tick() does the commit so the
    SKIP LOCKED claim releases promptly.
    """
    if now.tzinfo is None:
        raise ValueError("scheduler.tick requires a timezone-aware now")

    grace_floor = now - GRACE_WINDOW
    rows = session.execute(
        text(
            """
            SELECT id, workspace_id, agent_name, schedule
              FROM triggers
             WHERE enabled = true
               AND kind = 'cron'
               AND next_run_at IS NOT NULL
               AND next_run_at <= :now
               AND next_run_at >= :grace_floor
             ORDER BY next_run_at
             FOR UPDATE SKIP LOCKED
            """
        ),
        {"now": now, "grace_floor": grace_floor},
    ).mappings().all()

    fired = 0
    for row in rows:
        trigger_id = row["id"]
        workspace_id = row["workspace_id"]
        schedule = row["schedule"]
        # Enqueue the run. 22.4 will register the scheduled_run handler;
        # until then the row sits pending and either a handler appears
        # before the next jobs-runner cycle or fails on unknown kind.
        _jobs.enqueue_job(
            session,
            job_id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            kind="scheduled_run",
            request_payload={"trigger_id": trigger_id},
        )
        # Advance next_run_at + stamp last_run_at. Computing next_run_at
        # from `now` (not from the previous next_run_at) means a missed
        # tick window doesn't compound; the trigger fires once then
        # snaps to the next future slot.
        try:
            next_run = _trigmod.compute_next_run_at(schedule, now)
        except ValueError:
            # Malformed schedule that somehow got past create-time
            # validation. Disable the trigger so it stops blocking the
            # scan + log loudly.
            logger.error(
                "scheduler: disabling trigger %s with bad schedule %r",
                trigger_id, schedule,
            )
            session.execute(
                text(
                    "UPDATE triggers SET enabled = false, "
                    "updated_at = :now WHERE id = :id"
                ),
                {"now": now, "id": trigger_id},
            )
            continue
        session.execute(
            text(
                """
                UPDATE triggers
                   SET next_run_at = :next_run,
                       last_run_at = :now,
                       updated_at = :now
                 WHERE id = :id
                """
            ),
            {"next_run": next_run, "now": now, "id": trigger_id},
        )
        fired += 1

    session.commit()
    if fired:
        logger.info("scheduler: tick fired %d trigger(s)", fired)
    return fired


def fast_forward_stale_triggers(session: Session, now: datetime) -> int:
    """Startup sweep: triggers whose next_run_at is older than the
    grace window get pushed to the next future fire without a run.

    Returns the number of triggers advanced. Idempotent; safe to run
    on every startup.
    """
    if now.tzinfo is None:
        raise ValueError(
            "fast_forward_stale_triggers requires a timezone-aware now"
        )

    grace_floor = now - GRACE_WINDOW
    rows = session.execute(
        text(
            """
            SELECT id, schedule
              FROM triggers
             WHERE enabled = true
               AND kind = 'cron'
               AND next_run_at IS NOT NULL
               AND next_run_at < :grace_floor
             FOR UPDATE SKIP LOCKED
            """
        ),
        {"grace_floor": grace_floor},
    ).mappings().all()

    advanced = 0
    for row in rows:
        trigger_id = row["id"]
        try:
            next_run = _trigmod.compute_next_run_at(row["schedule"], now)
        except ValueError:
            logger.error(
                "scheduler: disabling trigger %s with bad schedule %r "
                "during stale sweep",
                trigger_id, row["schedule"],
            )
            session.execute(
                text(
                    "UPDATE triggers SET enabled = false, "
                    "updated_at = :now WHERE id = :id"
                ),
                {"now": now, "id": trigger_id},
            )
            continue
        session.execute(
            text(
                """
                UPDATE triggers
                   SET next_run_at = :next_run,
                       updated_at = :now
                 WHERE id = :id
                """
            ),
            {"next_run": next_run, "now": now, "id": trigger_id},
        )
        advanced += 1

    session.commit()
    if advanced:
        logger.info(
            "scheduler: fast-forwarded %d stale trigger(s) past grace window",
            advanced,
        )
    return advanced


async def scheduler_loop() -> None:
    """Forever loop. Cancel via task.cancel() on shutdown."""
    logger.info("scheduler: loop started (tick=%.1fs)", TICK_INTERVAL_S)
    try:
        while True:
            try:
                await asyncio.to_thread(_run_tick_in_thread)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler: unhandled error in tick")
            await asyncio.sleep(TICK_INTERVAL_S)
    except asyncio.CancelledError:
        logger.info("scheduler: loop cancelled, exiting")
        raise


def _run_tick_in_thread() -> None:
    """Sync wrapper for use inside asyncio.to_thread. Each tick gets
    its own short-lived session so a stuck DB call can't hold the
    transaction open across the sleep.

    The feeder rides this same loop (one extra pass per tick): it makes
    the business personas proactive by enqueueing scheduled digests. It
    gets its own session + commit so a feeder failure can never roll
    back the trigger scheduler's work, and vice versa.
    """
    now = datetime.now(timezone.utc)
    s = SessionLocal()
    try:
        tick(s, now)
    finally:
        s.close()

    s = SessionLocal()
    try:
        import feeder
        enqueued = feeder.tick(s, now)
        s.commit()
        if enqueued:
            logger.info("scheduler: feeder enqueued %d digest(s)", enqueued)
    except Exception:
        s.rollback()
        logger.exception("scheduler: feeder tick failed")
    finally:
        s.close()


_loop_task: Optional[asyncio.Task] = None


def start_scheduler() -> None:
    """Start the background scheduler task. Idempotent.

    Runs the startup-time stale sweep before launching the loop so a
    long downtime doesn't dump a stampede of backfill runs on the
    first tick.
    """
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    # One-shot stale sweep on startup. Synchronous; cheap (single
    # UPDATE per stale trigger) and avoids a race with the first tick.
    s = SessionLocal()
    try:
        fast_forward_stale_triggers(s, datetime.now(timezone.utc))
    except Exception:
        logger.exception("scheduler: startup stale sweep failed")
    finally:
        s.close()

    loop = asyncio.get_event_loop()
    _loop_task = loop.create_task(
        scheduler_loop(), name="lightsei-trigger-scheduler",
    )


async def stop_scheduler() -> None:
    """Cancel the scheduler task and await its exit. Safe to call
    repeatedly."""
    global _loop_task
    if _loop_task is None:
        return
    _loop_task.cancel()
    try:
        await _loop_task
    except (asyncio.CancelledError, Exception):
        pass
    _loop_task = None
