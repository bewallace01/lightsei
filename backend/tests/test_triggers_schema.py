"""Phase 22.1: tests for the triggers schema.

Three surfaces:

1. `triggers` row roundtrip + defaults (enabled server_default,
   cron vs webhook field separation, jsonb-style nullable fields).
2. FK behavior: cascade on workspace delete, SET NULL on the
   `last_run_id` link when the referenced run is deleted (so a
   trigger row survives run cleanup).
3. The partial-unique index on `webhook_token_hash` (NULL rows
   coexist; populated hashes must be unique) + the validation
   helper `is_valid_trigger_kind`.

Same shape as `test_widget_schema.py`. Endpoint + scheduler tests
live in their own files starting from 22.2.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from db import session_scope
from models import (
    Run,
    Trigger,
    Workspace,
    _VALID_TRIGGER_KINDS,
    is_valid_trigger_kind,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_workspace(s) -> str:
    ws_id = str(uuid.uuid4())
    s.add(Workspace(
        id=ws_id,
        name=f"triggers-schema-{ws_id[:8]}",
        created_at=_utcnow(),
    ))
    s.flush()
    return ws_id


def _make_cron_trigger(
    s,
    workspace_id: str,
    *,
    agent_name: str = "morning-digest",
    name: str = "weekday 9am",
    schedule: str = "0 9 * * 1-5",
    next_run_at: datetime | None = None,
) -> str:
    trigger_id = str(uuid.uuid4())
    now = _utcnow()
    s.add(Trigger(
        id=trigger_id,
        workspace_id=workspace_id,
        agent_name=agent_name,
        kind="cron",
        schedule=schedule,
        name=name,
        next_run_at=next_run_at or (now + timedelta(hours=1)),
        created_at=now,
        updated_at=now,
    ))
    s.flush()
    return trigger_id


def _make_webhook_trigger(
    s,
    workspace_id: str,
    *,
    agent_name: str = "morning-digest",
    name: str = "zapier hook",
    token_hash: str | None = None,
) -> str:
    trigger_id = str(uuid.uuid4())
    now = _utcnow()
    s.add(Trigger(
        id=trigger_id,
        workspace_id=workspace_id,
        agent_name=agent_name,
        kind="webhook",
        webhook_token_hash=token_hash or uuid.uuid4().hex,
        name=name,
        created_at=now,
        updated_at=now,
    ))
    s.flush()
    return trigger_id


def _make_run(s, workspace_id: str, *, agent_name: str = "morning-digest") -> str:
    run_id = str(uuid.uuid4())
    now = _utcnow()
    s.add(Run(
        id=run_id,
        workspace_id=workspace_id,
        agent_name=agent_name,
        started_at=now,
        ended_at=now,
        cost_usd=Decimal("0"),
    ))
    s.flush()
    return run_id


# ---------- Roundtrip + defaults ---------- #


def test_cron_trigger_roundtrip():
    """Cron row: schedule set, next_run_at set, webhook fields null,
    enabled defaults true via server_default."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        trigger_id = _make_cron_trigger(s, ws_id, schedule="*/15 * * * *")

    with session_scope() as s:
        row = s.get(Trigger, trigger_id)
        assert row is not None
        assert row.workspace_id == ws_id
        assert row.kind == "cron"
        assert row.schedule == "*/15 * * * *"
        assert row.webhook_token_hash is None
        assert row.enabled is True  # server_default
        assert row.next_run_at is not None
        assert row.last_run_at is None
        assert row.last_run_id is None
        assert row.last_run_status is None


def test_webhook_trigger_roundtrip():
    """Webhook row: token_hash set, schedule + next_run_at null."""
    token = "deadbeef" * 8
    with session_scope() as s:
        ws_id = _make_workspace(s)
        trigger_id = _make_webhook_trigger(s, ws_id, token_hash=token)

    with session_scope() as s:
        row = s.get(Trigger, trigger_id)
        assert row.kind == "webhook"
        assert row.webhook_token_hash == token
        assert row.schedule is None
        assert row.next_run_at is None


def test_enabled_server_default_is_true():
    """A trigger inserted without an explicit `enabled` lands as
    True via the server_default. Matches the operator-creates-it-on
    expectation in the dashboard."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        trigger_id = str(uuid.uuid4())
        now = _utcnow()
        s.execute(text(
            "INSERT INTO triggers "
            "(id, workspace_id, agent_name, kind, schedule, name, "
            " created_at, updated_at) "
            "VALUES (:id, :ws, :an, 'cron', '0 9 * * *', 'morning', "
            " :now, :now)"
        ), {"id": trigger_id, "ws": ws_id, "an": "morning-digest", "now": now})

    with session_scope() as s:
        row = s.get(Trigger, trigger_id)
        assert row.enabled is True


# ---------- FK behavior ---------- #


def test_trigger_fk_cascades_on_workspace_delete():
    """Deleting a workspace removes its triggers — workspace_id FK
    has ondelete CASCADE so we don't orphan rows."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        trigger_id = _make_cron_trigger(s, ws_id)

    with session_scope() as s:
        s.delete(s.get(Workspace, ws_id))

    with session_scope() as s:
        assert s.get(Trigger, trigger_id) is None


def test_last_run_id_set_null_on_run_delete():
    """Deleting a run nulls the trigger's last_run_id but keeps the
    trigger row. Run cleanup (future retention sweep) must not take
    triggers with it."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        trigger_id = _make_cron_trigger(s, ws_id)
        run_id = _make_run(s, ws_id)
        t = s.get(Trigger, trigger_id)
        t.last_run_id = run_id
        t.last_run_status = "succeeded"

    with session_scope() as s:
        s.delete(s.get(Run, run_id))

    with session_scope() as s:
        row = s.get(Trigger, trigger_id)
        assert row is not None  # trigger survives
        assert row.last_run_id is None  # FK SET NULL
        # status snapshot is independent of the FK and stays.
        assert row.last_run_status == "succeeded"


# ---------- Partial-unique on webhook_token_hash ---------- #


def test_two_cron_triggers_coexist_with_null_token_hash():
    """Cron rows have NULL webhook_token_hash. The partial-unique
    index only constrains populated values, so two cron triggers on
    the same agent coexist."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        _make_cron_trigger(s, ws_id, name="morning", schedule="0 9 * * *")
        _make_cron_trigger(s, ws_id, name="afternoon", schedule="0 14 * * *")
        # Both committed cleanly.


def test_duplicate_webhook_token_hash_rejected():
    """Two webhook triggers can't share a token hash — the partial-
    unique index fires. Token collisions would route the public
    webhook endpoint to the wrong agent."""
    shared_hash = "feedcafe" * 8
    with session_scope() as s:
        ws_id = _make_workspace(s)
        _make_webhook_trigger(s, ws_id, name="first", token_hash=shared_hash)

    # Use an explicit session so the IntegrityError fires on commit
    # (not when the session_scope() __exit__ raises). Same shape as
    # test_widget_schema's collision test.
    from db import SessionLocal
    s2 = SessionLocal()
    try:
        with session_scope() as s:
            ws_id = s.execute(select(Workspace.id)).scalars().first()
        s2.add(Trigger(
            id=str(uuid.uuid4()),
            workspace_id=ws_id,
            agent_name="morning-digest",
            kind="webhook",
            webhook_token_hash=shared_hash,  # collision
            name="second",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        ))
        with pytest.raises(IntegrityError):
            s2.commit()
    finally:
        s2.rollback()
        s2.close()


# ---------- Tenant isolation ---------- #


def test_triggers_isolate_workspaces():
    """A workspace's triggers don't leak across tenants — workspace_id
    is the foundational scope on every list query."""
    with session_scope() as s:
        ws_a = _make_workspace(s)
        ws_b = _make_workspace(s)
        _make_cron_trigger(s, ws_a, name="a-1")
        _make_cron_trigger(s, ws_b, name="b-1")

    with session_scope() as s:
        a_rows = s.execute(
            select(Trigger).where(Trigger.workspace_id == ws_a)
        ).scalars().all()
        assert len(a_rows) == 1
        assert a_rows[0].name == "a-1"


# ---------- Index existence ---------- #


def test_scheduler_due_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'triggers' "
            "AND indexname = 'ix_triggers_due'"
        )).first()
        assert r is not None


def test_per_agent_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'triggers' "
            "AND indexname = 'ix_triggers_workspace_agent'"
        )).first()
        assert r is not None


def test_webhook_token_partial_unique_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'triggers' "
            "AND indexname = 'ix_triggers_webhook_token'"
        )).first()
        assert r is not None


# ---------- Validation helper ---------- #


def test_kind_validator_accepts_known_values():
    for k in ("cron", "webhook"):
        assert is_valid_trigger_kind(k)


def test_kind_validator_rejects_unknown():
    for bad in ("CRON", "", "schedule", "event", None, 42):
        assert not is_valid_trigger_kind(bad)


def test_constant_set_has_expected_membership():
    """If someone adds an event-based kind (Phase 22B) without
    updating the validator, this test breaks immediately."""
    assert _VALID_TRIGGER_KINDS == {"cron", "webhook"}
