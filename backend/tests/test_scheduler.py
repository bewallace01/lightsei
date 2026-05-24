"""Phase 22.3: tests for the cron-trigger scheduler.

The runner loop itself is a thin wrapper around asyncio.sleep +
asyncio.to_thread. The interesting logic — what fires, what gets
skipped, what gets fast-forwarded — lives in two pure-ish helpers:
`tick(session, now)` and `fast_forward_stale_triggers(session, now)`.

Tests drive those directly with an explicit `now` so we don't need
freezegun. Tests deliberately don't use the FastAPI TestClient: the
client's startup hook would launch the real scheduler loop + the
jobs runner, which would race the assertions on
generation_jobs.status (and our own enqueued rows would pick a
scheduled_run handler that doesn't exist yet — that lands in 22.4).
See [[feedback_jobs_runner_test_race]].
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

import scheduler
import triggers as trigmod
from db import session_scope
from models import Trigger, Workspace


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_workspace(s) -> str:
    ws_id = str(uuid.uuid4())
    s.add(Workspace(
        id=ws_id,
        name=f"scheduler-{ws_id[:8]}",
        created_at=_now(),
    ))
    s.flush()
    return ws_id


def _make_cron_trigger(
    s,
    workspace_id: str,
    *,
    agent_name: str = "morning-digest",
    name: str = "9am",
    schedule: str = "0 9 * * *",
    next_run_at: datetime,
    enabled: bool = True,
) -> str:
    trigger_id = str(uuid.uuid4())
    now = _now()
    s.add(Trigger(
        id=trigger_id,
        workspace_id=workspace_id,
        agent_name=agent_name,
        kind="cron",
        schedule=schedule,
        name=name,
        enabled=enabled,
        next_run_at=next_run_at,
        created_at=now,
        updated_at=now,
    ))
    s.flush()
    return trigger_id


def _make_webhook_trigger(
    s, workspace_id: str, *, agent_name: str = "morning-digest",
) -> str:
    trigger_id = str(uuid.uuid4())
    now = _now()
    s.add(Trigger(
        id=trigger_id,
        workspace_id=workspace_id,
        agent_name=agent_name,
        kind="webhook",
        webhook_token_hash=uuid.uuid4().hex,
        name="webhook",
        created_at=now,
        updated_at=now,
    ))
    s.flush()
    return trigger_id


def _count_scheduled_run_jobs(s, *, trigger_id: str) -> int:
    """Filter by trigger_id payload so cross-test contamination
    (other tests sharing the same DB) doesn't inflate the count."""
    rows = s.execute(text(
        "SELECT request_payload FROM generation_jobs "
        "WHERE kind = 'scheduled_run'"
    )).mappings().all()
    return sum(
        1 for r in rows
        if (r["request_payload"] or {}).get("trigger_id") == trigger_id
    )


# ---------- tick(): happy path ---------- #


def test_tick_enqueues_scheduled_run_for_due_cron_trigger():
    """A cron trigger with next_run_at <= now fires: a
    generation_jobs row lands with kind='scheduled_run' + payload
    {trigger_id}."""
    now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc)
    past = now - timedelta(minutes=1)
    with session_scope() as s:
        ws_id = _make_workspace(s)
        tid = _make_cron_trigger(s, ws_id, next_run_at=past)

    with session_scope() as s:
        fired = scheduler.tick(s, now)
    assert fired == 1

    with session_scope() as s:
        assert _count_scheduled_run_jobs(s, trigger_id=tid) == 1


def test_tick_advances_next_run_at_and_stamps_last_run_at():
    """After tick(), next_run_at is strictly future relative to the
    tick's `now`, and last_run_at == now."""
    now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc)
    past = now - timedelta(minutes=1)
    with session_scope() as s:
        ws_id = _make_workspace(s)
        tid = _make_cron_trigger(
            s, ws_id, schedule="0 9 * * *", next_run_at=past,
        )

    with session_scope() as s:
        scheduler.tick(s, now)

    with session_scope() as s:
        row = s.get(Trigger, tid)
        assert row.next_run_at is not None
        assert row.next_run_at > now
        assert row.last_run_at == now


def test_tick_skips_disabled_trigger():
    now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc)
    past = now - timedelta(minutes=1)
    with session_scope() as s:
        ws_id = _make_workspace(s)
        tid = _make_cron_trigger(s, ws_id, next_run_at=past, enabled=False)

    with session_scope() as s:
        fired = scheduler.tick(s, now)
    assert fired == 0

    with session_scope() as s:
        assert _count_scheduled_run_jobs(s, trigger_id=tid) == 0
        row = s.get(Trigger, tid)
        # next_run_at not touched.
        assert row.next_run_at == past


def test_tick_skips_webhook_trigger():
    """Webhooks have NULL next_run_at and aren't part of the cron
    scan. tick() leaves them alone."""
    now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc)
    with session_scope() as s:
        ws_id = _make_workspace(s)
        tid = _make_webhook_trigger(s, ws_id)

    with session_scope() as s:
        fired = scheduler.tick(s, now)
    assert fired == 0

    with session_scope() as s:
        assert _count_scheduled_run_jobs(s, trigger_id=tid) == 0


def test_tick_skips_future_trigger():
    """A cron trigger whose next_run_at is in the future is left
    alone — scheduler.tick only fires due triggers."""
    now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc)
    future = now + timedelta(minutes=15)
    with session_scope() as s:
        ws_id = _make_workspace(s)
        tid = _make_cron_trigger(s, ws_id, next_run_at=future)

    with session_scope() as s:
        fired = scheduler.tick(s, now)
    assert fired == 0

    with session_scope() as s:
        row = s.get(Trigger, tid)
        assert row.next_run_at == future
        assert row.last_run_at is None


def test_tick_skips_trigger_past_grace_window():
    """A trigger whose next_run_at is older than the grace window
    isn't fired by tick. fast_forward_stale_triggers is the cleanup
    path for those rows."""
    now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc)
    way_past = now - scheduler.GRACE_WINDOW - timedelta(hours=1)
    with session_scope() as s:
        ws_id = _make_workspace(s)
        tid = _make_cron_trigger(s, ws_id, next_run_at=way_past)

    with session_scope() as s:
        fired = scheduler.tick(s, now)
    assert fired == 0

    with session_scope() as s:
        assert _count_scheduled_run_jobs(s, trigger_id=tid) == 0
        row = s.get(Trigger, tid)
        assert row.next_run_at == way_past  # untouched


def test_tick_idempotent_within_same_now():
    """Calling tick() twice with the same `now` only fires the
    trigger once: after the first call, next_run_at is in the
    future relative to `now`."""
    now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc)
    past = now - timedelta(minutes=1)
    with session_scope() as s:
        ws_id = _make_workspace(s)
        tid = _make_cron_trigger(s, ws_id, next_run_at=past)

    with session_scope() as s:
        first = scheduler.tick(s, now)
    with session_scope() as s:
        second = scheduler.tick(s, now)
    assert first == 1
    assert second == 0

    with session_scope() as s:
        assert _count_scheduled_run_jobs(s, trigger_id=tid) == 1


def test_tick_fires_multiple_due_triggers_in_one_pass():
    now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc)
    past = now - timedelta(minutes=1)
    with session_scope() as s:
        ws_id = _make_workspace(s)
        t1 = _make_cron_trigger(s, ws_id, name="a", next_run_at=past)
        t2 = _make_cron_trigger(s, ws_id, name="b", next_run_at=past)

    with session_scope() as s:
        fired = scheduler.tick(s, now)
    assert fired == 2

    with session_scope() as s:
        assert _count_scheduled_run_jobs(s, trigger_id=t1) == 1
        assert _count_scheduled_run_jobs(s, trigger_id=t2) == 1


def test_tick_disables_trigger_with_malformed_schedule():
    """If a trigger somehow ends up with a bad schedule (shouldn't
    happen via the API, but defense-in-depth), tick disables it
    instead of crash-looping the scheduler."""
    now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc)
    past = now - timedelta(minutes=1)
    with session_scope() as s:
        ws_id = _make_workspace(s)
        tid = _make_cron_trigger(
            s, ws_id, schedule="0 9 * * *", next_run_at=past,
        )
        # Bypass validate_cron + write garbage directly. Simulates a
        # corrupted row or a migration bug.
        s.execute(text(
            "UPDATE triggers SET schedule = 'garbage' WHERE id = :id"
        ), {"id": tid})

    with session_scope() as s:
        scheduler.tick(s, now)

    with session_scope() as s:
        row = s.get(Trigger, tid)
        # Was disabled, not crashed past.
        assert row.enabled is False


def test_tick_requires_tz_aware_now():
    with session_scope() as s:
        with pytest.raises(ValueError):
            scheduler.tick(s, datetime(2026, 5, 24, 9, 0, 0))


# ---------- fast_forward_stale_triggers ---------- #


def test_fast_forward_advances_past_grace_window_trigger():
    """A trigger whose next_run_at is way in the past gets pushed
    forward to the next future fire — no job enqueued."""
    now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc)
    way_past = now - scheduler.GRACE_WINDOW - timedelta(hours=2)
    with session_scope() as s:
        ws_id = _make_workspace(s)
        tid = _make_cron_trigger(
            s, ws_id, schedule="0 9 * * *", next_run_at=way_past,
        )

    with session_scope() as s:
        advanced = scheduler.fast_forward_stale_triggers(s, now)
    assert advanced == 1

    with session_scope() as s:
        row = s.get(Trigger, tid)
        assert row.next_run_at is not None
        assert row.next_run_at > now
        # No run was enqueued.
        assert _count_scheduled_run_jobs(s, trigger_id=tid) == 0


def test_fast_forward_ignores_in_window_trigger():
    """A trigger whose next_run_at is inside the grace window (will
    be fired by the next tick) shouldn't be touched."""
    now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc)
    # 1 hour past, well within the 24h grace window.
    recent_past = now - timedelta(hours=1)
    with session_scope() as s:
        ws_id = _make_workspace(s)
        tid = _make_cron_trigger(s, ws_id, next_run_at=recent_past)

    with session_scope() as s:
        advanced = scheduler.fast_forward_stale_triggers(s, now)
    assert advanced == 0

    with session_scope() as s:
        row = s.get(Trigger, tid)
        assert row.next_run_at == recent_past


def test_fast_forward_skips_disabled():
    now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc)
    way_past = now - scheduler.GRACE_WINDOW - timedelta(hours=2)
    with session_scope() as s:
        ws_id = _make_workspace(s)
        tid = _make_cron_trigger(
            s, ws_id, next_run_at=way_past, enabled=False,
        )

    with session_scope() as s:
        advanced = scheduler.fast_forward_stale_triggers(s, now)
    assert advanced == 0

    with session_scope() as s:
        row = s.get(Trigger, tid)
        assert row.next_run_at == way_past
