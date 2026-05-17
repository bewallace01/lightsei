"""Phase 16.4: backend tests for cross-zone dispatch enforcement.

The load-bearing piece. Same-zone dispatches always work; different-
zone dispatches are refused unless the source agent has
dispatches_cross_zone=True. Auto-approval rules (Phase 11.2) still
apply on top — cross-zone-enabled does NOT mean auto-approved.

This file covers the backend's enqueue_command gate. The SDK's
LightseiCrossZoneError surfacing of the same gate lives in
sdk/tests/test_cross_zone_gate.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from db import session_scope
from models import Agent
from tests.conftest import auth_headers


# ---------- Helpers ---------- #


def _make_agent(
    session, workspace_id, *, name,
    sensitivity_level="internal",
    dispatches_cross_zone=False,
    **kwargs,
):
    now = datetime.now(timezone.utc)
    session.add(
        Agent(
            workspace_id=workspace_id,
            name=name,
            role="executor",
            sensitivity_level=sensitivity_level,
            dispatches_cross_zone=dispatches_cross_zone,
            created_at=now,
            updated_at=now,
            **kwargs,
        )
    )


def _enqueue(client, api_key, *, target, source, kind="x.do", payload=None):
    return client.post(
        f"/agents/{target}/commands",
        headers=auth_headers(api_key),
        json={
            "kind": kind,
            "payload": payload or {},
            "source_agent": source,
            "dispatch_chain_id": str(uuid.uuid4()),
        },
    )


# ---------- Schema default ---------- #


def test_agent_defaults_to_dispatches_cross_zone_false(client, alice):
    """Default-deny posture for new agents: cross-zone dispatch is off
    unless explicitly opted in."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="argus")
    with session_scope() as s:
        a = s.execute(
            select(Agent).where(
                Agent.workspace_id == workspace_id,
                Agent.name == "argus",
            )
        ).scalar_one()
    assert a.dispatches_cross_zone is False


# ---------- enqueue_command: same-zone always allowed ---------- #


def test_same_zone_dispatch_allowed(client, alice):
    """Two agents in the same zone — even sensitive — dispatch freely
    regardless of dispatches_cross_zone setting."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="src", sensitivity_level="sensitive")
        _make_agent(s, workspace_id, name="tgt", sensitivity_level="sensitive")

    r = _enqueue(client, api_key, target="tgt", source="src")
    assert r.status_code == 200, r.text


def test_same_zone_dispatch_allowed_even_when_pii(client, alice):
    """Two `'pii'` agents in the same workspace can dispatch to each
    other — staying in-zone is always fine."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="pii-1", sensitivity_level="pii")
        _make_agent(s, workspace_id, name="pii-2", sensitivity_level="pii")

    r = _enqueue(client, api_key, target="pii-2", source="pii-1")
    assert r.status_code == 200, r.text


# ---------- enqueue_command: cross-zone refusal ---------- #


def test_cross_zone_refused_without_opt_in(client, alice):
    """The canonical CRM-bot block: pii agent tries to dispatch to a
    public agent → 403 cross_zone_blocked. This is the wedge."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="crm", sensitivity_level="pii")
        _make_agent(s, workspace_id, name="research", sensitivity_level="public")

    r = _enqueue(client, api_key, target="research", source="crm")
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "cross_zone_blocked"
    assert detail["source_agent"] == "crm"
    assert detail["source_zone"] == "pii"
    assert detail["target_agent"] == "research"
    assert detail["target_zone"] == "public"
    assert "dispatches_cross_zone" in detail["message"]


def test_cross_zone_refused_in_either_direction(client, alice):
    """Symmetric: public → pii is also blocked, not just pii → public.
    Defense-in-depth — there's no 'going up' or 'going down' on the
    sensitivity ladder; any difference triggers the gate."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="public-bot", sensitivity_level="public")
        _make_agent(s, workspace_id, name="pii-bot", sensitivity_level="pii")

    # public → pii
    r = _enqueue(client, api_key, target="pii-bot", source="public-bot")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "cross_zone_blocked"


def test_cross_zone_refused_internal_vs_sensitive(client, alice):
    """Any mismatch counts, not just public/pii extremes. internal vs
    sensitive is also a cross-zone dispatch."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="src", sensitivity_level="internal")
        _make_agent(s, workspace_id, name="tgt", sensitivity_level="sensitive")

    r = _enqueue(client, api_key, target="tgt", source="src")
    assert r.status_code == 403


# ---------- enqueue_command: opt-in unblocks the gate ---------- #


def test_cross_zone_allowed_when_source_opted_in(client, alice):
    """dispatches_cross_zone=True on the source lets the cross-zone
    dispatch through. Approval rules still apply on top (covered
    in a separate Phase 11.2 test) — this just verifies the gate
    opens."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(
            s, workspace_id, name="src",
            sensitivity_level="pii",
            dispatches_cross_zone=True,
        )
        _make_agent(s, workspace_id, name="tgt", sensitivity_level="public")

    r = _enqueue(client, api_key, target="tgt", source="src")
    assert r.status_code == 200, r.text


def test_opt_in_is_property_of_source_not_target(client, alice):
    """Opting the TARGET into cross-zone doesn't unblock anything —
    the property must be on the SOURCE. This is by design: the
    decision 'this agent is trusted to cross zones' is about the
    dispatcher's trustworthiness, not the target's."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="src", sensitivity_level="pii")
        _make_agent(
            s, workspace_id, name="tgt",
            sensitivity_level="public",
            dispatches_cross_zone=True,  # target opted in, but source didn't
        )

    r = _enqueue(client, api_key, target="tgt", source="src")
    assert r.status_code == 403


# ---------- User-initiated (no source_agent) bypasses the gate ---------- #


def test_user_initiated_dispatch_bypasses_zone_gate(client, alice):
    """A dashboard-driven enqueue (no source_agent in the body) is
    treated as an explicit user decision and skips the zone check.
    The user picked this target explicitly; we're not the SDK trying
    to enforce a contract on autonomous code."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="pii-bot", sensitivity_level="pii")

    # No source_agent in the body.
    r = client.post(
        "/agents/pii-bot/commands",
        headers=auth_headers(api_key),
        json={"kind": "x.do", "payload": {}},
    )
    assert r.status_code == 200, r.text


# ---------- PATCH dispatches_cross_zone ---------- #


def test_patch_agent_dispatches_cross_zone(client, alice):
    """The flag is editable through the existing PATCH /agents/{name}
    endpoint so the dashboard's agent-detail editor can toggle it."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="argus", sensitivity_level="pii")

    r = client.patch(
        "/agents/argus",
        headers=auth_headers(api_key),
        json={"dispatches_cross_zone": True},
    )
    assert r.status_code == 200
    assert r.json()["dispatches_cross_zone"] is True


def test_patch_agent_sensitivity_level(client, alice):
    """sensitivity_level is also editable via PATCH /agents/{name}.
    Validates against the four-level ladder; 422 with the valid set
    in the message on invalid."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, workspace_id, name="argus", sensitivity_level="internal")

    # Happy path: bump to pii.
    r = client.patch(
        "/agents/argus",
        headers=auth_headers(api_key),
        json={"sensitivity_level": "pii"},
    )
    assert r.status_code == 200
    assert r.json()["sensitivity_level"] == "pii"

    # Bad value → 422 with the valid set in the message.
    r = client.patch(
        "/agents/argus",
        headers=auth_headers(api_key),
        json={"sensitivity_level": "HIGH"},
    )
    assert r.status_code == 422
    assert "public" in r.json()["detail"]


def test_get_agent_serializes_dispatches_cross_zone(client, alice):
    """GET /agents/{name} returns the new field so the dashboard can
    render the toggle state."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(
            s, workspace_id, name="argus",
            sensitivity_level="pii",
            dispatches_cross_zone=True,
        )

    r = client.get("/agents/argus", headers=auth_headers(api_key))
    assert r.status_code == 200
    body = r.json()
    assert body["dispatches_cross_zone"] is True
    assert body["sensitivity_level"] == "pii"


# ---------- Cross-workspace isolation ---------- #


def test_cross_zone_gate_does_not_leak_across_workspaces(client, alice, bob):
    """Bob's agent with the same name as Alice's doesn't affect
    Alice's cross-zone check. Each workspace's gate uses its own
    agents."""
    a_ws = alice["workspace"]["id"]
    b_ws = bob["workspace"]["id"]
    api_key_a = alice["api_key"]["plaintext"]
    with session_scope() as s:
        _make_agent(s, a_ws, name="src", sensitivity_level="pii")
        _make_agent(s, a_ws, name="tgt", sensitivity_level="public")
        # Bob has a same-zone pair that would pass; shouldn't affect
        # Alice.
        _make_agent(s, b_ws, name="src", sensitivity_level="public")
        _make_agent(s, b_ws, name="tgt", sensitivity_level="public")

    r = _enqueue(client, api_key_a, target="tgt", source="src")
    assert r.status_code == 403
