"""Phase 15.2: run_behavioral_violations table + model.

Verifies the migration applied (conftest migrates to head) and the
RunBehavioralViolation model round-trips, including the unique
(run_id, rule) guard and the ON DELETE CASCADE from runs.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from db import session_scope
from models import RunBehavioralViolation, Run, Workspace


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_run() -> tuple[str, str]:
    ws_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(id=ws_id, name=f"ws-{ws_id[:8]}", created_at=_now()))
        s.flush()
        s.add(Run(id=run_id, workspace_id=ws_id, agent_name="polaris", started_at=_now()))
    return ws_id, run_id


def _violation(ws_id, run_id, *, rule="loop", severity="warn"):
    return RunBehavioralViolation(
        id=str(uuid.uuid4()),
        run_id=run_id,
        workspace_id=ws_id,
        agent_name="polaris",
        rule=rule,
        severity=severity,
        reason="action repeated 6 times",
        details={"count": 6, "threshold": 5},
        created_at=_now(),
    )


def test_insert_and_read_back():
    ws_id, run_id = _make_run()
    with session_scope() as s:
        s.add(_violation(ws_id, run_id))
    with session_scope() as s:
        row = s.execute(
            select(RunBehavioralViolation).where(
                RunBehavioralViolation.run_id == run_id
            )
        ).scalar_one()
        assert row.rule == "loop"
        assert row.severity == "warn"
        assert row.details["count"] == 6


def test_unique_run_rule_guard():
    ws_id, run_id = _make_run()
    with session_scope() as s:
        s.add(_violation(ws_id, run_id, rule="loop"))
    # A different rule on the same run is fine.
    with session_scope() as s:
        s.add(_violation(ws_id, run_id, rule="runaway_tokens"))
    # A second 'loop' on the same run violates the unique index.
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(_violation(ws_id, run_id, rule="loop"))


def test_cascade_on_run_delete():
    ws_id, run_id = _make_run()
    with session_scope() as s:
        s.add(_violation(ws_id, run_id))
    with session_scope() as s:
        s.delete(s.get(Run, run_id))
    with session_scope() as s:
        remaining = s.execute(
            select(RunBehavioralViolation).where(
                RunBehavioralViolation.run_id == run_id
            )
        ).first()
        assert remaining is None
