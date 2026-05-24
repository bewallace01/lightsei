"""Phase 22.4: `scheduled_run` job handler.

Registered with the in-process jobs runner (backend/jobs.py). When
backend/scheduler.py's tick() enqueues a `scheduled_run` row, this
handler picks it up and:

  1. Loads the trigger by id; if gone (race with a delete), drops
     the run silently.
  2. Loads the agent referenced by the trigger; if gone, marks the
     trigger's last_run_status='agent_missing' so the operator sees
     the misconfiguration in the dashboard list.
  3. Pre-creates a Run row with `triggered_by_trigger_id` +
     `trigger_kind` set, so /runs?trigger_id= filters cleanly and
     the run-card badge has the snapshot it needs.
  4. Inserts a Command row (kind='trigger.fire') the deployed bot
     picks up via SDK polling. The bot's @lightsei.on_trigger
     handler (added in 22.5) does the work and emits events under
     the pre-allocated run_id, which the /events endpoint stitches
     into the existing Run row.
  5. Updates the trigger's last_run_id + last_run_status='dispatched'
     so the dashboard list renders the most recent fire without a
     JOIN. The terminal status is mirrored back when the run ends
     (in backend/main.py's /events handler).

approval_state on the Command is 'auto_approved': the operator
opted in by creating the trigger; the dispatch chain doesn't need
a second human gate.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("lightsei.scheduled_run")


# Commands sit pending until the bot claims them (~SDK poll cadence).
# 24h TTL gives a deployment outage room to recover without the
# command silently expiring. Matches the Slack orchestrator's choice.
_COMMAND_TTL = timedelta(hours=24)


def run_scheduled_job(
    session: Session,
    workspace_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Handler entry point for `kind='scheduled_run'`.

    Returns a dict that the generation_jobs row stores as
    `result_payload`. Always returns; never raises. Dispatch failures
    are routine operational state — the trigger keeps firing on its
    cadence, the operator notices "agent_missing" in the dashboard
    list and fixes it.
    """
    # Local imports keep test-time import surface narrow + avoid the
    # main → models → handler → main circular import that's bitten us
    # before (same pattern as slack_orchestrator).
    from models import Agent, Command, Run, Trigger

    trigger_id = payload.get("trigger_id")
    webhook_payload = payload.get("webhook_payload")

    if not trigger_id:
        logger.error("scheduled_run: missing trigger_id in payload=%r", payload)
        return {"status": "failed", "reason": "missing_trigger_id"}

    trigger = session.get(Trigger, trigger_id)
    if trigger is None:
        logger.info(
            "scheduled_run: trigger %s no longer exists (raced with delete)",
            trigger_id,
        )
        return {"status": "skipped", "reason": "trigger_missing"}

    # Defensive: the scheduler should only enqueue for our workspace,
    # but check anyway so a stray payload can't bypass tenant scope.
    if trigger.workspace_id != workspace_id:
        logger.error(
            "scheduled_run: trigger %s workspace mismatch (job=%s trigger=%s)",
            trigger_id, workspace_id, trigger.workspace_id,
        )
        return {"status": "failed", "reason": "workspace_mismatch"}

    agent = session.get(Agent, (workspace_id, trigger.agent_name))
    if agent is None:
        logger.warning(
            "scheduled_run: trigger %s references missing agent %s/%s",
            trigger_id, workspace_id, trigger.agent_name,
        )
        trigger.last_run_status = "agent_missing"
        trigger.last_run_at = _utcnow()
        trigger.updated_at = _utcnow()
        return {
            "status": "failed",
            "reason": "agent_missing",
            "agent_name": trigger.agent_name,
        }

    now = _utcnow()
    # The webhook endpoint (22.6) pre-allocates a run_id at enqueue
    # time so it can return it to the caller immediately. Fall back to
    # minting one here when the scheduler tick (22.3) enqueued the job
    # — that path doesn't need to know the id up front.
    run_id = payload.get("run_id") or str(uuid.uuid4())
    scheduled_at_iso = (trigger.next_run_at.isoformat()
                        if trigger.next_run_at else None)

    # 1. Pre-create the Run row so /runs can render it immediately
    #    with the trigger badge. ended_at stays NULL until the bot's
    #    run_ended event flows through /events.
    session.add(Run(
        id=run_id,
        workspace_id=workspace_id,
        agent_name=trigger.agent_name,
        started_at=now,
        ended_at=None,
        sensitivity_level=agent.sensitivity_level,
        triggered_by_trigger_id=trigger.id,
        trigger_kind=trigger.kind,
    ))

    # 2. Insert the Command. The bot's @on_trigger handler (22.5)
    #    receives the payload; SDK reads `lightsei.trigger.kind`
    #    + `lightsei.trigger.webhook_payload` from it.
    cmd_id = str(uuid.uuid4())
    cmd_payload: dict[str, Any] = {
        "run_id": run_id,
        "trigger_id": trigger.id,
        "trigger_name": trigger.name,
        "trigger_kind": trigger.kind,
        "scheduled_at": scheduled_at_iso,
    }
    if webhook_payload is not None:
        cmd_payload["webhook_payload"] = webhook_payload

    session.add(Command(
        id=cmd_id,
        workspace_id=workspace_id,
        agent_name=trigger.agent_name,
        kind="trigger.fire",
        payload=cmd_payload,
        status="pending",
        approval_state="auto_approved",  # operator opted in via trigger
        created_at=now,
        expires_at=now + _COMMAND_TTL,
    ))

    # 3. Snapshot dispatch state on the trigger so the dashboard
    #    list renders the most recent fire without a JOIN. The
    #    terminal status is mirrored back from /events when the
    #    run ends.
    trigger.last_run_id = run_id
    trigger.last_run_status = "dispatched"
    trigger.last_run_at = now
    trigger.updated_at = now

    session.flush()
    logger.info(
        "scheduled_run: dispatched trigger=%s agent=%s run=%s cmd=%s",
        trigger_id, trigger.agent_name, run_id, cmd_id,
    )
    return {
        "status": "dispatched",
        "trigger_id": trigger.id,
        "run_id": run_id,
        "command_id": cmd_id,
        "agent_name": trigger.agent_name,
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _register() -> None:
    import jobs
    jobs.register_handler("scheduled_run", run_scheduled_job)


_register()
