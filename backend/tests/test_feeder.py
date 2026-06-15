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
from decimal import Decimal

from sqlalchemy import text

import feeder
from db import session_scope
from models import Agent, Event, Run, Workspace


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


def _count_feeder_cmds(s, workspace_id: str,
                       source: str = feeder.DIGEST_SOURCE) -> int:
    return s.execute(
        text(
            "SELECT count(*) FROM commands "
            "WHERE workspace_id = :ws AND kind = :kind "
            "AND payload ->> 'source' = :source"
        ),
        {"ws": workspace_id, "kind": feeder.DIGEST_KIND, "source": source},
    ).scalar_one()


def _make_run(s, workspace_id: str, *, agent_name: str, cost_usd: str,
              started_at: datetime) -> None:
    s.add(Run(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        agent_name=agent_name,
        started_at=started_at,
        ended_at=started_at,
        cost_usd=Decimal(cost_usd),
    ))
    s.flush()


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


# ---------- cost-spike detector: pure ---------- #


def test_detect_cost_spike_flags_a_real_jump():
    alert = feeder.detect_cost_spike(30.0, 10.0)
    assert alert is not None
    assert alert["pct_increase"] == 200
    assert "up 200%" in alert["headline"]


def test_detect_cost_spike_ignores_normal_variation():
    # 20% up is noise, not a spike (threshold is 50%).
    assert feeder.detect_cost_spike(12.0, 10.0) is None


def test_detect_cost_spike_guards_tiny_baseline():
    # 2c -> 50c is a 25x ratio but the baseline is below the floor.
    assert feeder.detect_cost_spike(0.50, 0.02) is None


def test_detect_cost_spike_handles_zero_prior():
    assert feeder.detect_cost_spike(50.0, 0.0) is None


# ---------- cost-spike feeder: integration ---------- #


def test_cost_alert_enqueued_on_spike():
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_bi(s, ws)
        # Prior week: $10 baseline. This week: $40 (4x) -> spike.
        _make_run(s, ws, agent_name="marketing", cost_usd="10.00",
                  started_at=_now() - timedelta(days=10))
        _make_run(s, ws, agent_name="marketing", cost_usd="35.00",
                  started_at=_now() - timedelta(days=2))
        _make_run(s, ws, agent_name="bi", cost_usd="5.00",
                  started_at=_now() - timedelta(days=1))

    with session_scope() as s:
        cmd_id = feeder.enqueue_cost_alert_for_workspace(s, ws, _now())
        assert cmd_id is not None

    with session_scope() as s:
        assert _count_feeder_cmds(s, ws, source=feeder.COST_ALERT_SOURCE) == 1
        row = s.execute(
            text("SELECT payload FROM commands WHERE workspace_id = :ws "
                 "AND payload ->> 'source' = :src"),
            {"ws": ws, "src": feeder.COST_ALERT_SOURCE},
        ).mappings().first()
        data = row["payload"]["data"]
        assert data["this_week_usd"] == 40.0
        assert data["prior_week_usd"] == 10.0
        assert data["this_week_by_assistant"]["marketing"] == 35.0
        assert "question" in row["payload"]


def test_no_cost_alert_when_spend_is_steady():
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_bi(s, ws)
        _make_run(s, ws, agent_name="bi", cost_usd="10.00",
                  started_at=_now() - timedelta(days=10))
        _make_run(s, ws, agent_name="bi", cost_usd="11.00",
                  started_at=_now() - timedelta(days=2))

    with session_scope() as s:
        assert feeder.enqueue_cost_alert_for_workspace(s, ws, _now()) is None

    with session_scope() as s:
        assert _count_feeder_cmds(s, ws, source=feeder.COST_ALERT_SOURCE) == 0


def test_cost_alert_dedups_within_window():
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_bi(s, ws)
        _make_run(s, ws, agent_name="bi", cost_usd="10.00",
                  started_at=_now() - timedelta(days=10))
        _make_run(s, ws, agent_name="bi", cost_usd="40.00",
                  started_at=_now() - timedelta(days=2))

    now = _now()
    with session_scope() as s:
        assert feeder.enqueue_cost_alert_for_workspace(s, ws, now) is not None
    with session_scope() as s:
        # Still spiking, but within the dedup window -> no second nag.
        assert feeder.enqueue_cost_alert_for_workspace(
            s, ws, now + timedelta(hours=3)) is None

    with session_scope() as s:
        assert _count_feeder_cmds(s, ws, source=feeder.COST_ALERT_SOURCE) == 1


def test_tick_runs_both_feeders():
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_bi(s, ws)
        _add_event(s, ws, kind="lead.scored", agent_name="lead",
                   timestamp=_now() - timedelta(days=1))
        _make_run(s, ws, agent_name="marketing", cost_usd="10.00",
                  started_at=_now() - timedelta(days=10))
        _make_run(s, ws, agent_name="marketing", cost_usd="40.00",
                  started_at=_now() - timedelta(days=2))

    with session_scope() as s:
        feeder.tick(s, _now())

    with session_scope() as s:
        assert _count_feeder_cmds(s, ws, source=feeder.DIGEST_SOURCE) == 1
        assert _count_feeder_cmds(s, ws, source=feeder.COST_ALERT_SOURCE) == 1


# ---------- feeder settings (opt-out) ---------- #


def test_feeder_enabled_by_default():
    with session_scope() as s:
        ws = _make_workspace(s)
        assert feeder.is_feeder_enabled(s, ws, feeder.FEEDER_WEEKLY_DIGEST)
        assert feeder.is_feeder_enabled(s, ws, feeder.FEEDER_COST_SPIKE)


def test_set_feeder_disabled_then_enabled():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        feeder.set_feeder_enabled(s, ws, feeder.FEEDER_WEEKLY_DIGEST, False, now)
    with session_scope() as s:
        assert not feeder.is_feeder_enabled(s, ws, feeder.FEEDER_WEEKLY_DIGEST)
        # The other feeder is untouched (independent rows).
        assert feeder.is_feeder_enabled(s, ws, feeder.FEEDER_COST_SPIKE)
    with session_scope() as s:
        feeder.set_feeder_enabled(s, ws, feeder.FEEDER_WEEKLY_DIGEST, True, now)
    with session_scope() as s:
        assert feeder.is_feeder_enabled(s, ws, feeder.FEEDER_WEEKLY_DIGEST)


def test_get_feeder_settings_annotates_catalog():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        feeder.set_feeder_enabled(s, ws, feeder.FEEDER_COST_SPIKE, False, now)
    with session_scope() as s:
        settings = feeder.get_feeder_settings(s, ws)
    by_kind = {f["kind"]: f for f in settings}
    assert by_kind[feeder.FEEDER_WEEKLY_DIGEST]["enabled"] is True
    assert by_kind[feeder.FEEDER_COST_SPIKE]["enabled"] is False
    # Catalog metadata is carried through for the UI.
    assert by_kind[feeder.FEEDER_WEEKLY_DIGEST]["name"]


def test_tick_skips_disabled_digest():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_bi(s, ws)
        feeder.set_feeder_enabled(s, ws, feeder.FEEDER_WEEKLY_DIGEST, False, now)
        # A spike so the cost feeder WOULD fire — proving only the digest
        # is gated off, not the whole tick.
        _make_run(s, ws, agent_name="bi", cost_usd="10.00",
                  started_at=now - timedelta(days=10))
        _make_run(s, ws, agent_name="bi", cost_usd="40.00",
                  started_at=now - timedelta(days=2))

    with session_scope() as s:
        feeder.tick(s, now)

    with session_scope() as s:
        assert _count_feeder_cmds(s, ws, source=feeder.DIGEST_SOURCE) == 0
        assert _count_feeder_cmds(s, ws, source=feeder.COST_ALERT_SOURCE) == 1


# ---------- Inbox Gmail feeder ---------- #


def _deploy_inbox(s, workspace_id: str) -> None:
    now = _now()
    s.add(Agent(
        workspace_id=workspace_id, name=feeder.INBOX_AGENT,
        role="executor", created_at=now, updated_at=now,
    ))
    s.flush()


def _gmail_messages(*ids):
    return {
        "messages": [
            {"id": mid, "thread_id": f"t-{mid}", "snippet": f"snippet {mid}",
             "from": "a@b.com", "subject": f"subject {mid}",
             "date": "Mon, 9 Jun 2026 00:00:00 +0000", "label_ids": ["UNREAD"]}
            for mid in ids
        ]
    }


def _count_inbox_cmds(s, workspace_id: str) -> int:
    return s.execute(
        text(
            "SELECT count(*) FROM commands WHERE workspace_id = :ws "
            "AND kind = :kind AND payload ->> 'source' = :source"
        ),
        {"ws": workspace_id, "kind": feeder.INBOX_KIND,
         "source": feeder.INBOX_SOURCE},
    ).scalar_one()


def test_inbox_feeder_enqueues_per_message(monkeypatch):
    import main
    monkeypatch.setattr(
        main, "invoke_connector_tool",
        lambda *a, **k: _gmail_messages("m1", "m2"),
    )
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_inbox(s, ws)

    with session_scope() as s:
        n = feeder.enqueue_inbox_items_for_workspace(s, ws, _now())
        assert n == 2

    with session_scope() as s:
        assert _count_inbox_cmds(s, ws) == 2
        row = s.execute(
            text("SELECT payload FROM commands WHERE workspace_id = :ws "
                 "AND payload ->> 'gmail_message_id' = 'm1'"),
            {"ws": ws},
        ).mappings().first()
        # The inbox assistant reads payload.email; body falls back to snippet.
        assert row["payload"]["email"]["subject"] == "subject m1"
        assert row["payload"]["email"]["body"] == "snippet m1"


def test_inbox_feeder_dedups_on_message_id(monkeypatch):
    import main
    monkeypatch.setattr(
        main, "invoke_connector_tool",
        lambda *a, **k: _gmail_messages("m1", "m2"),
    )
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_inbox(s, ws)

    with session_scope() as s:
        feeder.enqueue_inbox_items_for_workspace(s, ws, _now())
    # Same two messages still unread next poll, plus a new one.
    monkeypatch.setattr(
        main, "invoke_connector_tool",
        lambda *a, **k: _gmail_messages("m1", "m2", "m3"),
    )
    with session_scope() as s:
        n = feeder.enqueue_inbox_items_for_workspace(s, ws, _now())
        assert n == 1  # only m3 is new

    with session_scope() as s:
        assert _count_inbox_cmds(s, ws) == 3


def test_inbox_feeder_skips_on_connector_error(monkeypatch):
    import main

    def _boom(*a, **k):
        raise RuntimeError("gmail not installed")

    monkeypatch.setattr(main, "invoke_connector_tool", _boom)
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_inbox(s, ws)

    with session_scope() as s:
        assert feeder.enqueue_inbox_items_for_workspace(s, ws, _now()) == 0
    with session_scope() as s:
        assert _count_inbox_cmds(s, ws) == 0


def test_inbox_feeder_off_by_default_in_tick(monkeypatch):
    import main
    monkeypatch.setattr(
        main, "invoke_connector_tool",
        lambda *a, **k: _gmail_messages("m1"),
    )
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_inbox(s, ws)

    # Default off: tick must not poll Gmail or enqueue anything.
    with session_scope() as s:
        assert not feeder.is_feeder_enabled(s, ws, feeder.FEEDER_INBOX_GMAIL)
        feeder.tick(s, _now())
    with session_scope() as s:
        assert _count_inbox_cmds(s, ws) == 0


def test_inbox_feeder_runs_in_tick_when_enabled(monkeypatch):
    import main
    monkeypatch.setattr(
        main, "invoke_connector_tool",
        lambda *a, **k: _gmail_messages("m1", "m2"),
    )
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_inbox(s, ws)
        feeder.set_feeder_enabled(s, ws, feeder.FEEDER_INBOX_GMAIL, True, now)

    with session_scope() as s:
        feeder.tick(s, now)
    with session_scope() as s:
        assert _count_inbox_cmds(s, ws) == 2


# ---------- Reputation review feeder ---------- #


def _deploy_reputation(s, workspace_id: str) -> None:
    now = _now()
    s.add(Agent(
        workspace_id=workspace_id, name=feeder.REPUTATION_AGENT,
        role="executor", created_at=now, updated_at=now,
    ))
    s.flush()


def _fake_gb(reviews):
    """A stand-in for invoke_connector_tool that walks the auto-discover
    chain: one account, one location, then the supplied reviews."""
    def _invoke(session, *, workspace_id, connector_type, tool_name,
                payload, source_agent):
        assert connector_type == "google_business"
        assert source_agent == feeder.REPUTATION_AGENT
        if tool_name == "list_accounts":
            return {"accounts": [{"id": "acc1", "account_name": "Acme"}]}
        if tool_name == "list_locations":
            return {"locations": [{"id": "loc1", "title": "Acme Downtown"}]}
        if tool_name == "list_reviews":
            return {"reviews": reviews}
        raise AssertionError(f"unexpected tool {tool_name}")
    return _invoke


def _review(rid, rating, comment="ok", reviewer="Dana"):
    return {"id": rid, "rating": rating, "star_rating": "ONE",
            "comment": comment, "reviewer": reviewer,
            "create_time": "2026-06-09T00:00:00Z", "has_reply": False}


def _count_reputation_cmds(s, workspace_id: str) -> int:
    return s.execute(
        text(
            "SELECT count(*) FROM commands WHERE workspace_id = :ws "
            "AND kind = :kind AND payload ->> 'source' = :source"
        ),
        {"ws": workspace_id, "kind": feeder.REPUTATION_KIND,
         "source": feeder.REPUTATION_SOURCE},
    ).scalar_one()


def test_reputation_feeder_enqueues_per_review(monkeypatch):
    import main
    monkeypatch.setattr(
        main, "invoke_connector_tool",
        _fake_gb([_review("rv1", 1, "Slow"), _review("rv2", 5, "Great")]),
    )
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_reputation(s, ws)

    with session_scope() as s:
        n = feeder.enqueue_reputation_reviews_for_workspace(s, ws, _now())
        assert n == 2

    with session_scope() as s:
        assert _count_reputation_cmds(s, ws) == 2
        row = s.execute(
            text("SELECT payload FROM commands WHERE workspace_id = :ws "
                 "AND payload ->> 'review_id' = 'rv1'"),
            {"ws": ws},
        ).mappings().first()
        # The reputation assistant reads payload.review.{text,rating,author}.
        assert row["payload"]["review"]["text"] == "Slow"
        assert row["payload"]["review"]["rating"] == 1
        assert row["payload"]["review"]["source"] == "Acme Downtown"


def test_reputation_feeder_dedups_on_review_id(monkeypatch):
    import main
    monkeypatch.setattr(
        main, "invoke_connector_tool", _fake_gb([_review("rv1", 1)]),
    )
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_reputation(s, ws)

    with session_scope() as s:
        feeder.enqueue_reputation_reviews_for_workspace(s, ws, _now())
    # Same review still present next poll, plus a new one.
    monkeypatch.setattr(
        main, "invoke_connector_tool",
        _fake_gb([_review("rv1", 1), _review("rv2", 2)]),
    )
    with session_scope() as s:
        n = feeder.enqueue_reputation_reviews_for_workspace(s, ws, _now())
        assert n == 1  # only rv2 is new

    with session_scope() as s:
        assert _count_reputation_cmds(s, ws) == 2


def test_reputation_feeder_skips_when_no_account(monkeypatch):
    import main

    def _no_accounts(session, *, tool_name, **k):
        return {"accounts": []} if tool_name == "list_accounts" else {}

    monkeypatch.setattr(main, "invoke_connector_tool", _no_accounts)
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_reputation(s, ws)

    with session_scope() as s:
        assert feeder.enqueue_reputation_reviews_for_workspace(s, ws, _now()) == 0
    with session_scope() as s:
        assert _count_reputation_cmds(s, ws) == 0


def test_reputation_feeder_skips_on_connector_error(monkeypatch):
    import main

    def _boom(*a, **k):
        raise RuntimeError("not installed")

    monkeypatch.setattr(main, "invoke_connector_tool", _boom)
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_reputation(s, ws)

    with session_scope() as s:
        assert feeder.enqueue_reputation_reviews_for_workspace(s, ws, _now()) == 0


def test_reputation_feeder_off_by_default_in_tick(monkeypatch):
    import main
    monkeypatch.setattr(
        main, "invoke_connector_tool", _fake_gb([_review("rv1", 1)]),
    )
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_reputation(s, ws)

    with session_scope() as s:
        assert not feeder.is_feeder_enabled(
            s, ws, feeder.FEEDER_REPUTATION_REVIEWS)
        feeder.tick(s, _now())
    with session_scope() as s:
        assert _count_reputation_cmds(s, ws) == 0


def test_reputation_feeder_runs_in_tick_when_enabled(monkeypatch):
    import main
    monkeypatch.setattr(
        main, "invoke_connector_tool",
        _fake_gb([_review("rv1", 1), _review("rv2", 5)]),
    )
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_reputation(s, ws)
        feeder.set_feeder_enabled(
            s, ws, feeder.FEEDER_REPUTATION_REVIEWS, True, now)

    with session_scope() as s:
        feeder.tick(s, now)
    with session_scope() as s:
        assert _count_reputation_cmds(s, ws) == 2


# ---------- per-workspace feeder targeting (config) ---------- #


def test_feeder_config_round_trips():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        feeder.set_feeder_config(
            s, ws, feeder.FEEDER_REPUTATION_REVIEWS,
            {"account_id": "acc1", "location_id": "loc1",
             "location_title": "Acme Downtown"}, now,
        )
    with session_scope() as s:
        cfg = feeder.get_feeder_config(s, ws, feeder.FEEDER_REPUTATION_REVIEWS)
        assert cfg["account_id"] == "acc1"
        assert cfg["location_id"] == "loc1"


def test_setting_config_does_not_enable_an_off_feeder():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        feeder.set_feeder_config(
            s, ws, feeder.FEEDER_REPUTATION_REVIEWS,
            {"account_id": "a", "location_id": "l"}, now,
        )
    with session_scope() as s:
        # Reputation defaults OFF; picking a target must not flip it on.
        assert not feeder.is_feeder_enabled(
            s, ws, feeder.FEEDER_REPUTATION_REVIEWS)


def test_get_feeder_settings_carries_config_and_targetable():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        feeder.set_feeder_config(
            s, ws, feeder.FEEDER_REPUTATION_REVIEWS,
            {"location_id": "loc1"}, now,
        )
    with session_scope() as s:
        by_kind = {f["kind"]: f for f in feeder.get_feeder_settings(s, ws)}
    rep = by_kind[feeder.FEEDER_REPUTATION_REVIEWS]
    assert rep["targetable"] is True
    assert rep["config"]["location_id"] == "loc1"
    # Internal feeders take no target.
    assert by_kind[feeder.FEEDER_WEEKLY_DIGEST]["targetable"] is False


def test_reputation_feeder_uses_configured_target_without_discovery(monkeypatch):
    import main

    def _invoke(session, *, workspace_id, connector_type, tool_name,
                payload, source_agent):
        if tool_name == "list_reviews":
            # Confirm the configured target is what's queried.
            assert payload["account_id"] == "acc9"
            assert payload["location_id"] == "loc9"
            return {"reviews": [_review("rv1", 1, "Bad")]}
        raise AssertionError(
            f"discovery call {tool_name} should be skipped when configured")

    monkeypatch.setattr(main, "invoke_connector_tool", _invoke)
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_reputation(s, ws)
        feeder.set_feeder_config(
            s, ws, feeder.FEEDER_REPUTATION_REVIEWS,
            {"account_id": "acc9", "location_id": "loc9",
             "location_title": "Acme Uptown"}, now,
        )

    with session_scope() as s:
        n = feeder.enqueue_reputation_reviews_for_workspace(s, ws, now)
        assert n == 1

    with session_scope() as s:
        row = s.execute(
            text("SELECT payload FROM commands WHERE workspace_id = :ws"),
            {"ws": ws},
        ).mappings().first()
        # The configured location title rides the command payload.
        assert row["payload"]["review"]["source"] == "Acme Uptown"


# ---------- Website health feeder ---------- #


def _deploy_website(s, workspace_id: str) -> None:
    now = _now()
    s.add(Agent(
        workspace_id=workspace_id,
        name=feeder.WEBSITE_AGENT,
        role="executor",
        created_at=now,
        updated_at=now,
    ))
    s.flush()


def _count_website_cmds(s, workspace_id: str) -> int:
    return s.execute(
        text(
            "SELECT count(*) FROM commands "
            "WHERE workspace_id = :ws AND kind = :kind "
            "AND payload ->> 'source' = :source"
        ),
        {"ws": workspace_id, "kind": feeder.WEBSITE_KIND,
         "source": feeder.WEBSITE_SOURCE},
    ).scalar_one()


def test_normalize_website_url_adds_scheme_and_validates():
    n = feeder.normalize_website_url
    assert n("example.com") == "https://example.com"
    assert n("  www.example.com/contact ") == "https://www.example.com/contact"
    assert n("http://shop.example.com") == "http://shop.example.com"
    # Not a URL: empty, bare word (no dot), or a non-web scheme.
    assert n("") is None
    assert n(None) is None
    assert n("homepage") is None
    assert n("ftp://example.com") is None
    # Private or local targets would turn the worker into an SSRF probe.
    assert n("http://169.254.169.254/latest/meta-data/") is None
    assert n("http://127.0.0.1:8000") is None
    assert n("http://service.railway.internal") is None
    assert n("http://user:pass@example.com") is None


def test_website_feeder_enqueues_check_for_configured_url():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_website(s, ws)
        feeder.set_feeder_config(
            s, ws, feeder.FEEDER_WEBSITE_HEALTH,
            {"url": "https://acme.example"}, now,
        )

    with session_scope() as s:
        assert feeder.enqueue_website_check_for_workspace(s, ws, now) == 1

    with session_scope() as s:
        assert _count_website_cmds(s, ws) == 1
        row = s.execute(
            text("SELECT payload FROM commands WHERE workspace_id = :ws"),
            {"ws": ws},
        ).mappings().first()
        # The website assistant reads payload.url.
        assert row["payload"]["url"] == "https://acme.example"
        assert row["payload"]["source"] == feeder.WEBSITE_SOURCE


def test_website_feeder_noop_without_configured_url():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_website(s, ws)

    # Enabled by default but no URL set yet -> clean no-op, no command.
    with session_scope() as s:
        assert feeder.is_feeder_enabled(s, ws, feeder.FEEDER_WEBSITE_HEALTH)
        assert feeder.enqueue_website_check_for_workspace(s, ws, now) == 0
    with session_scope() as s:
        assert _count_website_cmds(s, ws) == 0


def test_website_feeder_dedups_within_window():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_website(s, ws)
        feeder.set_feeder_config(
            s, ws, feeder.FEEDER_WEBSITE_HEALTH,
            {"url": "https://acme.example"}, now,
        )

    with session_scope() as s:
        assert feeder.enqueue_website_check_for_workspace(s, ws, now) == 1
    # A second tick a few hours later (inside the daily window) enqueues
    # nothing: one sweep per cadence.
    later = now + timedelta(hours=3)
    with session_scope() as s:
        assert feeder.enqueue_website_check_for_workspace(s, ws, later) == 0
    with session_scope() as s:
        assert _count_website_cmds(s, ws) == 1


def test_website_feeder_fires_again_after_window():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_website(s, ws)
        feeder.set_feeder_config(
            s, ws, feeder.FEEDER_WEBSITE_HEALTH,
            {"url": "https://acme.example"}, now,
        )

    with session_scope() as s:
        feeder.enqueue_website_check_for_workspace(s, ws, now)
    # Past the dedup window -> a fresh sweep is due.
    tomorrow = now + feeder.WEBSITE_DEDUP_WINDOW + timedelta(minutes=1)
    with session_scope() as s:
        assert feeder.enqueue_website_check_for_workspace(s, ws, tomorrow) == 1
    with session_scope() as s:
        assert _count_website_cmds(s, ws) == 2


def test_website_feeder_runs_in_tick_when_enabled():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_website(s, ws)
        feeder.set_feeder_config(
            s, ws, feeder.FEEDER_WEBSITE_HEALTH,
            {"url": "https://acme.example"}, now,
        )

    with session_scope() as s:
        feeder.tick(s, now)
    with session_scope() as s:
        assert _count_website_cmds(s, ws) == 1


def test_website_feeder_skips_when_disabled():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        _deploy_website(s, ws)
        feeder.set_feeder_config(
            s, ws, feeder.FEEDER_WEBSITE_HEALTH,
            {"url": "https://acme.example"}, now,
        )
        feeder.set_feeder_enabled(
            s, ws, feeder.FEEDER_WEBSITE_HEALTH, False, now)

    with session_scope() as s:
        feeder.tick(s, now)
    with session_scope() as s:
        assert _count_website_cmds(s, ws) == 0
