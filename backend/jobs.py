"""In-process async runner for `generation_jobs` (Phase 12C.6).

The runner is a single asyncio task started on FastAPI startup. It polls
the `generation_jobs` table for `status='pending'` rows, claims one at a
time using `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`, dispatches to
the handler for the row's `kind`, and writes terminal state
(`success` + `result_payload`, or `failed` + `error`).

Why in-process, not a separate worker: Lightsei runs as one Railway
service today. A multi-instance queue would be premature. The
SKIP LOCKED claim is included anyway so a future second instance (or
a stray reload) can't double-process the same row.

Why asyncio.to_thread for the handler: the Anthropic SDK is sync. Running
the handler in a thread keeps the event loop free for the rest of
FastAPI (request handlers, /health probes, etc.). The runner task itself
stays on the main loop and just awaits the thread.

No auto-retry in v1. On exception, the row is marked `failed`, the
error is persisted, and the user retries from the UI (which enqueues
a fresh row).
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from db import SessionLocal

logger = logging.getLogger("lightsei.jobs")

# Handler signature: (session, workspace_id, request_payload) -> result_payload.
# The handler may commit incrementally; the runner commits the terminal
# row update itself. Exceptions bubble; the runner catches and marks
# `failed`.
JobHandler = Callable[[Session, str, dict[str, Any]], dict[str, Any]]

_HANDLERS: dict[str, JobHandler] = {}


def register_handler(kind: str, fn: JobHandler) -> None:
    """Register a handler for a job kind. Idempotent; later registers win."""
    _HANDLERS[kind] = fn


def _load_default_handlers() -> None:
    """Pull in modules that register handlers as a side effect.

    Kept in a function so test code can replace the registry without
    triggering the heavy imports. Called from `start_runner()`.
    """
    # Imported for side-effect registration.
    import agent_generator  # noqa: F401
    import eval_runner  # noqa: F401
    import slack_orchestrator  # noqa: F401  (Phase 19.4)
    import team_planner  # noqa: F401
    import widget_orchestrator  # noqa: F401  (Phase 21.6)


def claim_pending_job(session: Session) -> Optional[dict[str, Any]]:
    """Atomically claim the oldest pending job.

    Returns a dict snapshot of the claimed row (id, workspace_id, kind,
    request_payload) or None if no pending rows. The row is updated to
    `status='running'` + `started_at=now` in the same transaction so a
    concurrent claimer can't see it.

    Uses FOR UPDATE SKIP LOCKED so multiple runners don't fight over
    the same row.
    """
    now = datetime.now(timezone.utc)
    row = session.execute(
        text(
            """
            SELECT id, workspace_id, kind, request_payload, attempt_count
              FROM generation_jobs
             WHERE status = 'pending'
             ORDER BY created_at
             LIMIT 1
             FOR UPDATE SKIP LOCKED
            """
        )
    ).mappings().first()
    if row is None:
        session.rollback()
        return None
    session.execute(
        text(
            """
            UPDATE generation_jobs
               SET status = 'running',
                   started_at = :now,
                   attempt_count = attempt_count + 1
             WHERE id = :id
            """
        ),
        {"now": now, "id": row["id"]},
    )
    session.commit()
    return dict(row)


def _finalize(
    session: Session,
    job_id: str,
    *,
    success: bool,
    result_payload: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    """Write terminal state for a job. Caller controls session commit."""
    now = datetime.now(timezone.utc)
    # psycopg won't adapt a bare dict for jsonb; serialize and CAST
    # (same pattern as `enqueue_job`).
    result_json = _json_dumps(result_payload) if result_payload is not None else None
    session.execute(
        text(
            """
            UPDATE generation_jobs
               SET status = :status,
                   result_payload = CAST(:result AS jsonb),
                   error = :error,
                   finished_at = :now
             WHERE id = :id
            """
        ),
        {
            "status": "success" if success else "failed",
            "result": result_json,
            "error": (error or None) if not success else None,
            "now": now,
            "id": job_id,
        },
    )
    session.commit()


async def run_one_job() -> bool:
    """Claim one pending job and run it. Returns True if a job was
    processed (success or failure), False if no work was available.

    Each invocation opens its own session for the claim, and a second
    session for the terminal write. The handler runs in its own
    session, in a worker thread, so a slow Anthropic call doesn't
    block the event loop or hold the claim transaction open.
    """
    claim_session = SessionLocal()
    try:
        try:
            claimed = claim_pending_job(claim_session)
        finally:
            claim_session.close()
    except Exception:
        logger.exception("jobs: claim failed")
        return False

    if claimed is None:
        return False

    job_id = claimed["id"]
    kind = claimed["kind"]
    workspace_id = claimed["workspace_id"]
    payload = claimed["request_payload"] or {}

    handler = _HANDLERS.get(kind)
    if handler is None:
        # Unknown kind: terminal failure so it doesn't get re-picked.
        s = SessionLocal()
        try:
            _finalize(
                s,
                job_id,
                success=False,
                error=f"no handler registered for kind={kind!r}",
            )
        finally:
            s.close()
        logger.error("jobs: no handler for kind=%s job=%s", kind, job_id)
        return True

    # Run the (sync) handler in a thread so we don't block the loop.
    def _run_sync() -> dict[str, Any]:
        s = SessionLocal()
        try:
            result = handler(s, workspace_id, payload)
            # Handler may have committed already; flush + commit any
            # trailing state for safety.
            s.commit()
            return result
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    try:
        result = await asyncio.to_thread(_run_sync)
        s = SessionLocal()
        try:
            _finalize(s, job_id, success=True, result_payload=result)
        finally:
            s.close()
        logger.info("jobs: ok kind=%s job=%s", kind, job_id)
    except Exception as e:
        tb = "".join(traceback.format_exception_only(type(e), e)).strip()
        s = SessionLocal()
        try:
            _finalize(s, job_id, success=False, error=tb)
        finally:
            s.close()
        logger.exception("jobs: failed kind=%s job=%s", kind, job_id)
    return True


# Sleep between empty picks. Short enough that interactive jobs feel
# instant; long enough that an idle backend isn't hammering the DB.
_IDLE_SLEEP_S = 0.5
_BUSY_SLEEP_S = 0.0  # tight loop while there's work; backpressure comes
                     # from claim_pending_job serializing on the row lock.


async def runner_loop() -> None:
    """Forever loop. Cancel via task.cancel() on shutdown."""
    logger.info("jobs: runner started")
    try:
        while True:
            try:
                did_work = await run_one_job()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("jobs: unhandled error in run_one_job")
                did_work = False
            await asyncio.sleep(_BUSY_SLEEP_S if did_work else _IDLE_SLEEP_S)
    except asyncio.CancelledError:
        logger.info("jobs: runner cancelled, exiting")
        raise


_runner_task: Optional[asyncio.Task] = None


def start_runner() -> None:
    """Start the background runner task. Idempotent.

    Called from FastAPI startup. If a runner task is already alive,
    this is a no-op (so dev-reload doesn't stack tasks).
    """
    global _runner_task
    _load_default_handlers()
    if _runner_task is not None and not _runner_task.done():
        return
    loop = asyncio.get_event_loop()
    _runner_task = loop.create_task(runner_loop(), name="lightsei-jobs-runner")


async def stop_runner() -> None:
    """Cancel the runner task and await its exit. Safe to call repeatedly."""
    global _runner_task
    if _runner_task is None:
        return
    _runner_task.cancel()
    try:
        await _runner_task
    except (asyncio.CancelledError, Exception):
        pass
    _runner_task = None


def enqueue_job(
    session: Session,
    *,
    job_id: str,
    workspace_id: str,
    kind: str,
    request_payload: dict[str, Any],
) -> None:
    """Insert a pending job row. Caller commits the session.

    Endpoints call this from inside their existing session/dependency
    so the row is part of the same transaction as input validation.
    """
    now = datetime.now(timezone.utc)
    session.execute(
        text(
            """
            INSERT INTO generation_jobs
              (id, workspace_id, kind, status, request_payload,
               attempt_count, created_at)
            VALUES
              (:id, :wsid, :kind, 'pending', CAST(:payload AS jsonb),
               0, :now)
            """
        ),
        {
            "id": job_id,
            "wsid": workspace_id,
            "kind": kind,
            "payload": _json_dumps(request_payload),
            "now": now,
        },
    )


def _json_dumps(payload: dict[str, Any]) -> str:
    import json
    return json.dumps(payload)
