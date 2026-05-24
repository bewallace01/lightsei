"""Phase 22.4: tests for the scheduled_run handler + run-status mirror.

Two surfaces:

1. Handler (`backend/scheduled_run.py`): pre-creates a Run row with
   triggered_by_trigger_id + trigger_kind set, inserts a Command
   row (kind='trigger.fire'), patches trigger's last_run_* fields.
   Gracefully handles missing trigger / missing agent.
2. Status mirror in /events: when a run with triggered_by_trigger_id
   receives a run_completed / run_failed / run_ended event, the
   trigger's last_run_status flips to 'succeeded' / 'failed'.

Tests drive the handler directly (not through the jobs runner) so
assertions on row state aren't racing the runner. See
[[feedback_jobs_runner_test_race]].
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from db import session_scope
from models import Agent, Command, Run, Trigger, Workspace
from scheduled_run import run_scheduled_job
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_workspace(s) -> str:
    ws_id = str(uuid.uuid4())
    s.add(Workspace(
        id=ws_id, name=f"sched-{ws_id[:8]}", created_at=_now(),
    ))
    s.flush()
    return ws_id


def _make_agent(s, workspace_id: str, name: str = "morning-digest", *,
                sensitivity_level: str = "internal") -> None:
    s.add(Agent(
        workspace_id=workspace_id,
        name=name,
        role="executor",
        sensitivity_level=sensitivity_level,
        capabilities=[],
        command_handlers=[],
        created_at=_now(),
        updated_at=_now(),
    ))


def _make_cron_trigger(
    s, workspace_id: str, *, agent_name: str = "morning-digest",
    name: str = "9am",
) -> str:
    tid = str(uuid.uuid4())
    now = _now()
    s.add(Trigger(
        id=tid,
        workspace_id=workspace_id,
        agent_name=agent_name,
        kind="cron",
        schedule="0 9 * * *",
        name=name,
        enabled=True,
        next_run_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    ))
    s.flush()
    return tid


def _make_webhook_trigger(
    s, workspace_id: str, *, agent_name: str = "morning-digest",
) -> str:
    tid = str(uuid.uuid4())
    now = _now()
    s.add(Trigger(
        id=tid,
        workspace_id=workspace_id,
        agent_name=agent_name,
        kind="webhook",
        webhook_token_hash=uuid.uuid4().hex,
        name="zapier",
        enabled=True,
        created_at=now,
        updated_at=now,
    ))
    s.flush()
    return tid


# ---------- Handler happy path ---------- #


def test_handler_creates_run_with_trigger_link():
    """Pre-created Run row carries triggered_by_trigger_id +
    trigger_kind so /runs?trigger_id= filters cleanly without
    waiting for the bot to emit anything."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        _make_agent(s, ws_id)
        tid = _make_cron_trigger(s, ws_id)

    with session_scope() as s:
        result = run_scheduled_job(s, ws_id, {"trigger_id": tid})
    assert result["status"] == "dispatched"
    assert result["agent_name"] == "morning-digest"
    run_id = result["run_id"]

    with session_scope() as s:
        run = s.get(Run, run_id)
        assert run is not None
        assert run.workspace_id == ws_id
        assert run.agent_name == "morning-digest"
        assert run.triggered_by_trigger_id == tid
        assert run.trigger_kind == "cron"
        assert run.ended_at is None  # still pending bot's run_ended
        assert run.sensitivity_level == "internal"


def test_handler_inserts_trigger_fire_command():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        _make_agent(s, ws_id)
        tid = _make_cron_trigger(s, ws_id, name="morning")

    with session_scope() as s:
        result = run_scheduled_job(s, ws_id, {"trigger_id": tid})

    with session_scope() as s:
        cmd = s.get(Command, result["command_id"])
        assert cmd is not None
        assert cmd.kind == "trigger.fire"
        assert cmd.agent_name == "morning-digest"
        assert cmd.workspace_id == ws_id
        # Operator opted in by creating the trigger; no second human gate.
        assert cmd.approval_state == "auto_approved"
        assert cmd.status == "pending"
        assert cmd.payload["run_id"] == result["run_id"]
        assert cmd.payload["trigger_id"] == tid
        assert cmd.payload["trigger_kind"] == "cron"
        assert cmd.payload["trigger_name"] == "morning"
        assert "scheduled_at" in cmd.payload


def test_handler_passes_through_webhook_payload():
    """Webhook-fired triggers carry the POST body forward to the
    bot via cmd.payload['webhook_payload']."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        _make_agent(s, ws_id)
        tid = _make_webhook_trigger(s, ws_id)

    body = {"channel": "#sales", "user": "ada"}
    with session_scope() as s:
        result = run_scheduled_job(s, ws_id, {
            "trigger_id": tid, "webhook_payload": body,
        })

    with session_scope() as s:
        cmd = s.get(Command, result["command_id"])
        assert cmd.payload["webhook_payload"] == body
        assert cmd.payload["trigger_kind"] == "webhook"

        run = s.get(Run, result["run_id"])
        assert run.trigger_kind == "webhook"


def test_handler_omits_webhook_payload_when_unset():
    """Cron runs have no webhook payload — payload key is absent so
    bots that check for it can branch cleanly."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        _make_agent(s, ws_id)
        tid = _make_cron_trigger(s, ws_id)

    with session_scope() as s:
        result = run_scheduled_job(s, ws_id, {"trigger_id": tid})

    with session_scope() as s:
        cmd = s.get(Command, result["command_id"])
        assert "webhook_payload" not in cmd.payload


def test_handler_updates_trigger_last_run_fields():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        _make_agent(s, ws_id)
        tid = _make_cron_trigger(s, ws_id)

    with session_scope() as s:
        result = run_scheduled_job(s, ws_id, {"trigger_id": tid})

    with session_scope() as s:
        t = s.get(Trigger, tid)
        assert t.last_run_id == result["run_id"]
        assert t.last_run_status == "dispatched"
        assert t.last_run_at is not None


# ---------- Handler degenerate paths ---------- #


def test_handler_drops_silently_when_trigger_missing():
    """Race: scheduler enqueued the job, then the operator deleted
    the trigger before the runner picked it up. The handler returns
    skipped without creating Run / Command rows."""
    with session_scope() as s:
        ws_id = _make_workspace(s)

    fake_tid = str(uuid.uuid4())
    with session_scope() as s:
        result = run_scheduled_job(s, ws_id, {"trigger_id": fake_tid})
    assert result["status"] == "skipped"
    assert result["reason"] == "trigger_missing"

    with session_scope() as s:
        runs = s.execute(select(Run)).scalars().all()
        cmds = s.execute(select(Command)).scalars().all()
        assert runs == []
        assert cmds == []


def test_handler_marks_agent_missing_when_agent_deleted():
    """Trigger exists but the agent it references doesn't. Flip the
    trigger to last_run_status='agent_missing' so the operator notices
    in the dashboard list — no Run / Command rows are created."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        _make_agent(s, ws_id)
        tid = _make_cron_trigger(s, ws_id)
        # Operator deletes the agent without removing the trigger.
        s.delete(s.get(Agent, (ws_id, "morning-digest")))

    with session_scope() as s:
        result = run_scheduled_job(s, ws_id, {"trigger_id": tid})
    assert result["status"] == "failed"
    assert result["reason"] == "agent_missing"

    with session_scope() as s:
        t = s.get(Trigger, tid)
        assert t.last_run_status == "agent_missing"
        runs = s.execute(select(Run)).scalars().all()
        assert runs == []


def test_handler_rejects_payload_workspace_mismatch():
    """Defensive: the scheduler enqueues with the trigger's workspace_id,
    but the handler still verifies the payload-vs-trigger workspace
    match so a stray payload can't bypass tenant scope."""
    with session_scope() as s:
        ws_a = _make_workspace(s)
        ws_b = _make_workspace(s)
        _make_agent(s, ws_a)
        tid = _make_cron_trigger(s, ws_a)

    with session_scope() as s:
        result = run_scheduled_job(s, ws_b, {"trigger_id": tid})
    assert result["status"] == "failed"
    assert result["reason"] == "workspace_mismatch"


def test_handler_rejects_missing_trigger_id_in_payload():
    with session_scope() as s:
        ws_id = _make_workspace(s)

    with session_scope() as s:
        result = run_scheduled_job(s, ws_id, {})
    assert result["status"] == "failed"
    assert result["reason"] == "missing_trigger_id"


# ---------- Schema: FK SET NULL on trigger delete ---------- #


def test_trigger_delete_sets_run_link_to_null():
    """runs.triggered_by_trigger_id FK is SET NULL on trigger delete
    so historical runs survive trigger cleanup. trigger_kind snapshot
    remains so the /runs badge still renders."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        _make_agent(s, ws_id)
        tid = _make_cron_trigger(s, ws_id)

    with session_scope() as s:
        result = run_scheduled_job(s, ws_id, {"trigger_id": tid})
    run_id = result["run_id"]

    with session_scope() as s:
        s.delete(s.get(Trigger, tid))

    with session_scope() as s:
        run = s.get(Run, run_id)
        assert run is not None
        assert run.triggered_by_trigger_id is None  # FK SET NULL
        assert run.trigger_kind == "cron"  # snapshot survives


# ---------- /events status mirror ---------- #


def _post_event(client, api_key, *, run_id, agent_name, kind,
                payload=None):
    return client.post(
        "/events",
        headers=auth_headers(api_key),
        json={
            "run_id": run_id,
            "agent_name": agent_name,
            "kind": kind,
            "payload": payload or {},
        },
    )


def test_status_mirror_run_completed_flips_to_succeeded(client, alice):
    """run_completed event on a triggered run mirrors back to the
    trigger as last_run_status='succeeded'."""
    ws_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, ws_id)
        tid = _make_cron_trigger(s, ws_id)

    with session_scope() as s:
        result = run_scheduled_job(s, ws_id, {"trigger_id": tid})
    run_id = result["run_id"]

    r = _post_event(
        client, api_key,
        run_id=run_id, agent_name="morning-digest", kind="run_completed",
    )
    assert r.status_code == 200, r.text

    with session_scope() as s:
        t = s.get(Trigger, tid)
        assert t.last_run_status == "succeeded"


def test_status_mirror_run_failed_flips_to_failed(client, alice):
    ws_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, ws_id)
        tid = _make_cron_trigger(s, ws_id)

    with session_scope() as s:
        result = run_scheduled_job(s, ws_id, {"trigger_id": tid})
    run_id = result["run_id"]

    r = _post_event(
        client, api_key,
        run_id=run_id, agent_name="morning-digest", kind="run_failed",
    )
    assert r.status_code == 200, r.text

    with session_scope() as s:
        t = s.get(Trigger, tid)
        assert t.last_run_status == "failed"


def test_status_mirror_skipped_on_manual_run(client, alice):
    """A run with NULL triggered_by_trigger_id doesn't touch any
    trigger row — the mirror is scoped to triggered runs only."""
    ws_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, ws_id)
        # Create an unrelated trigger so we can confirm it stays put.
        tid = _make_cron_trigger(s, ws_id)
        t = s.get(Trigger, tid)
        t.last_run_status = "dispatched"  # set so we can detect a stray write

    manual_run_id = str(uuid.uuid4())
    r = _post_event(
        client, api_key,
        run_id=manual_run_id, agent_name="morning-digest",
        kind="run_completed",
    )
    assert r.status_code == 200

    with session_scope() as s:
        t = s.get(Trigger, tid)
        assert t.last_run_status == "dispatched"  # unchanged
