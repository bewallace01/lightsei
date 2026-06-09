"""Tests for the feeder — what makes the AI Business Team proactive.

Two layers:
  - build_digest_payload(): pure. Driven with plain dicts, no DB.
  - enqueue_due_digests / tick / enqueue_digest_for_workspace: integration
    against the real DB via session_scope, driven with an explicit `now`
    (same style as test_scheduler.py — no freezegun, no TestClient so the
    real scheduler loop doesn't race us).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import feeder
from db import session_scope
from models import Agent, Event, Workspace


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_workspace(s) -> str:
    ws_id = str(uuid.uuid4())
    s.add(Workspace(id=ws_id, name=f"feeder-{ws_id[:8]}", created_at=_now()))
    s.flush()
    return ws_id


def _deploy_bi(s, workspace_id: str, name: str = feeder.DIGEST_AGENT) -> None:
    now = _now()
    s.add(Agent(
        workspace_id=workspace_id,
        name=name,
        role="executor",
        created_at=now,
        updated_at=now,
    ))
    s.flush()


def _add_event(s, workspace_id: str, *, kind: str, agent_name: str,
               timestamp: datetime) -> None:
    s.add(Event(
        workspace_id=workspace_id,
        run_id=str(uuid.uuid4()),
        agent_name=agent_name,
        kind=kind,
        payload={},
        timestamp=timestamp,
    ))


def _count_feeder_cmds(s, workspace_id: str) -> int:
    return s.execute(
        text(
            "SELECT count(*) FROM commands "
            "WHERE workspace_id = :ws AND kind = :kind "
            "AND payload ->> 'source' = :source"
        ),
        {"ws": workspace_id, "kind": feeder.DIGEST_KIND,
         "source": feeder.DIGEST_SOURCE},
    ).scalar_one()


# ---------- build_digest_payload(): pure ---------- #


def test_digest_payload_counts_by_assistant_and_kind():
    events = [
        {"kind": "lead.scored", "agent_name": "lead", "payload": {}},
        {"kind": "lead.scored", "agent_name": "lead", "payload": {}},
        {"kind": "reputation.analyzed", "agent_name": "reputation", "payload": {}},
        {"kind": "website.check_complete", "agent_name": "website", "payload": {}},
    ]
    out = feeder.build_digest_payload(events, now_iso="2026-06-08T00:00:00+00:00")

    assert out["source"] == "feeder"
    assert out["total_events"] == 4
    assert out["events_by_assistant"] == {"lead": 2, "reputation": 1, "website": 1}
    assert out["events_by_kind"]["lead.scored"] == 2
    # Curated highlights promote real persona kinds to headline counters.
    assert out["highlights"]["leads_scored"] == 2
    assert out["highlights"]["reviews_analyzed"] == 1
    assert out["highlights"]["website_checks"] == 1


def test_digest_payload_empty_week_is_valid():
    out = feeder.build_digest_payload([], now_iso=None)
    assert out["total_events"] == 0
    assert out["events_by_assistant"] == {}
    assert out["highlights"] == {}  # no zero-count headline noise


def test_digest_payload_counts_crashes_too():
    events = [{"kind": "bi.crash", "agent_name": "bi", "payload": {}}]
    out = feeder.build_digest_payload(events)
    assert out["events_by_kind"]["bi.crash"] == 1
    # A crash isn't a curated highlight, but it's not dropped.
    assert out["highlights"] == {}


# ---------- enqueue: integration ---------- #


def test_tick_enqueues_one_digest_per_bi_workspace():
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_bi(s, ws)
        _add_event(s, ws, kind="lead.scored", agent_name="lead",
                   timestamp=_now() - timedelta(days=1))

    with session_scope() as s:
        n = feeder.tick(s, _now())

    assert n >= 1
    with session_scope() as s:
        assert _count_feeder_cmds(s, ws) == 1
        # The enqueued command carries the rolled-up digest data.
        row = s.execute(
            text("SELECT payload FROM commands WHERE workspace_id = :ws "
                 "AND kind = :k"),
            {"ws": ws, "k": feeder.DIGEST_KIND},
        ).mappings().first()
        assert row["payload"]["data"]["total_events"] == 1
        assert row["payload"]["data"]["highlights"]["leads_scored"] == 1


def test_tick_is_idempotent_within_dedup_window():
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_bi(s, ws)

    now = _now()
    with session_scope() as s:
        feeder.tick(s, now)
    # Second tick a few hours later: still inside DEDUP_WINDOW -> no new cmd.
    with session_scope() as s:
        feeder.tick(s, now + timedelta(hours=3))

    with session_scope() as s:
        assert _count_feeder_cmds(s, ws) == 1


def test_tick_fires_again_after_dedup_window():
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_bi(s, ws)

    now = _now()
    with session_scope() as s:
        feeder.tick(s, now)
    with session_scope() as s:
        feeder.tick(s, now + feeder.DEDUP_WINDOW + timedelta(hours=1))

    with session_scope() as s:
        assert _count_feeder_cmds(s, ws) == 2


def test_tick_skips_workspace_without_bi():
    with session_scope() as s:
        ws = _make_workspace(s)
        # No BI assistant deployed; deploy a different persona instead.
        _deploy_bi(s, ws, name="website")

    with session_scope() as s:
        feeder.tick(s, _now())

    with session_scope() as s:
        assert _count_feeder_cmds(s, ws) == 0


def test_force_bypasses_dedup_window():
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_bi(s, ws)

    now = _now()
    with session_scope() as s:
        feeder.enqueue_digest_for_workspace(s, ws, now)
    # Same window, but force=True (the "generate now" endpoint path).
    with session_scope() as s:
        cmd_id = feeder.enqueue_digest_for_workspace(s, ws, now, force=True)
        assert cmd_id is not None

    with session_scope() as s:
        assert _count_feeder_cmds(s, ws) == 2


def test_digest_excludes_events_older_than_period():
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_bi(s, ws)
        _add_event(s, ws, kind="lead.scored", agent_name="lead",
                   timestamp=_now() - timedelta(days=2))   # in window
        _add_event(s, ws, kind="lead.scored", agent_name="lead",
                   timestamp=_now() - timedelta(days=30))  # too old

    with session_scope() as s:
        feeder.enqueue_digest_for_workspace(s, ws, _now())

    with session_scope() as s:
        row = s.execute(
            text("SELECT payload FROM commands WHERE workspace_id = :ws"),
            {"ws": ws},
        ).mappings().first()
        assert row["payload"]["data"]["total_events"] == 1
