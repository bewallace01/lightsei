"""Phase 33.1: business-onboarding tests.

Pure plan-builder tests + integration (apply provisions assistants +
feeders + profile) + endpoint round-trip.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text

import feeder
import onboarding
from db import session_scope
from models import Workspace
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_workspace(s) -> str:
    ws_id = str(uuid.uuid4())
    s.add(Workspace(id=ws_id, name=f"onb-{ws_id[:8]}", created_at=_now()))
    s.flush()
    return ws_id


def _agent_names(s, ws: str) -> set[str]:
    rows = s.execute(
        text("SELECT name FROM agents WHERE workspace_id = :ws"), {"ws": ws}
    ).scalars().all()
    return set(rows)


def _agent_capabilities(s, ws: str, name: str) -> list[str]:
    row = s.execute(
        text(
            "SELECT capabilities FROM agents "
            "WHERE workspace_id = :ws AND name = :name"
        ),
        {"ws": ws, "name": name},
    ).first()
    assert row is not None
    return list(row[0] or [])


# ---------- pure plan builder ---------- #


def test_plan_maps_goals_to_assistants_feeders_connectors():
    plan = onboarding.build_provisioning_plan(
        "restaurant", ["email", "reviews", "summary"]
    )
    assert plan["assistants"] == ["inbox", "reputation", "bi"]
    # summary turns on two feeders; email/reviews their external ones.
    assert set(plan["feeders"]) == {
        "inbox_gmail", "reputation_reviews", "weekly_digest", "cost_spike",
    }
    conn = {c["type"] for c in plan["connectors_needed"]}
    assert conn == {"gmail", "google_business"}


def test_plan_dedups_and_drops_unknown_goals():
    plan = onboarding.build_provisioning_plan(
        "other", ["summary", "summary", "not_a_goal"]
    )
    assert plan["assistants"] == ["bi"]
    assert plan["goals"] == ["summary"]  # unknown dropped, dup collapsed


def test_plan_marketing_needs_no_connector():
    plan = onboarding.build_provisioning_plan("retail", ["marketing"])
    assert plan["assistants"] == ["marketing"]
    assert plan["feeders"] == []
    assert plan["connectors_needed"] == []


def test_recommended_goals_per_industry():
    assert "reviews" in onboarding.recommended_goals("restaurant")
    assert onboarding.recommended_goals("unknown_industry") == []


def test_catalog_has_industries_and_goals():
    cat = onboarding.catalog()
    assert {i["key"] for i in cat["industries"]} >= {"restaurant", "other"}
    assert {g["key"] for g in cat["goals"]} >= {"email", "reviews", "summary"}
    assert cat["recommendations"]["restaurant"]


# ---------- apply (integration) ---------- #


def test_apply_provisions_assistants_feeders_and_profile():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        plan = onboarding.build_provisioning_plan(
            "restaurant", ["reviews", "summary"]
        )
        onboarding.apply_provisioning_plan(s, ws, plan, now)

    with session_scope() as s:
        assert {"reputation", "bi"} <= _agent_names(s, ws)
        assert "connector:google_business" in _agent_capabilities(
            s, ws, "reputation"
        )
        # BI has no external connector, so onboarding leaves its powers alone.
        assert _agent_capabilities(s, ws, "bi") == []
        # weekly_digest defaults on anyway; reputation_reviews defaults OFF
        # and must be turned ON by onboarding.
        assert feeder.is_feeder_enabled(s, ws, feeder.FEEDER_REPUTATION_REVIEWS)
        profile = onboarding.get_profile(s, ws)
        assert profile["industry"] == "restaurant"
        assert set(profile["goals"]) == {"reviews", "summary"}
        assert profile["completed_at"]


def test_apply_is_idempotent():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        plan = onboarding.build_provisioning_plan("other", ["summary"])
        onboarding.apply_provisioning_plan(s, ws, plan, now)
    with session_scope() as s:
        onboarding.apply_provisioning_plan(s, ws, plan, now)
    with session_scope() as s:
        # One bi row, not two (ensure_agent is upsert-safe).
        n = s.execute(
            text("SELECT count(*) FROM agents WHERE workspace_id = :ws "
                 "AND name = 'bi'"),
            {"ws": ws},
        ).scalar_one()
        assert n == 1


def test_apply_merges_connector_capability_with_existing_capabilities():
    now = _now()
    with session_scope() as s:
        ws = _make_workspace(s)
        s.execute(
            text(
                """
                INSERT INTO agents (
                    workspace_id, name, daily_cost_cap_usd, capabilities,
                    created_at, updated_at
                )
                VALUES (
                    :ws, 'inbox', NULL, CAST(:capabilities AS JSONB),
                    :now, :now
                )
                """
            ),
            {"ws": ws, "capabilities": '["send_command"]', "now": now},
        )
        plan = onboarding.build_provisioning_plan("restaurant", ["email"])
        onboarding.apply_provisioning_plan(s, ws, plan, now)

    with session_scope() as s:
        assert _agent_capabilities(s, ws, "inbox") == [
            "send_command",
            "connector:gmail",
        ]


def test_get_profile_none_before_onboarding():
    with session_scope() as s:
        ws = _make_workspace(s)
        assert onboarding.get_profile(s, ws) is None


# ---------- endpoints ---------- #


def test_onboarding_get_returns_catalog_and_null_profile(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/onboarding", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile"] is None
    assert body["catalog"]["industries"]
    assert body["catalog"]["goals"]


def test_onboarding_submit_provisions_and_persists(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.post(
        "/workspaces/me/onboarding",
        headers=h,
        json={"industry": "professional", "goals": ["email", "summary"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "provisioned"
    assert set(body["plan"]["assistants"]) == {"inbox", "bi"}
    assert body["profile"]["industry"] == "professional"

    with session_scope() as s:
        assert "connector:gmail" in _agent_capabilities(
            s, alice["workspace"]["id"], "inbox"
        )

    # Now the profile is persisted: GET reflects it.
    r2 = client.get("/workspaces/me/onboarding", headers=h)
    assert r2.json()["profile"]["industry"] == "professional"
