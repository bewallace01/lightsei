"""Phase 23.3: tests for the workspace CRUD surface.

Five endpoints scoped to the calling session user:

  GET /me/workspaces
  POST /me/workspaces
  POST /me/workspaces/{id}/switch
  PATCH /me/workspaces/{id}
  DELETE /me/workspaces/{id}

All five reject api-key auth (api keys are pinned to one workspace
and have no concept of "switch"). Owner-only gating on PATCH +
DELETE; refuse-last on DELETE; auto-switch when DELETE-ing the
active workspace.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from db import session_scope
from models import (
    Agent,
    Run,
    Workspace,
    WorkspaceMember,
)
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _S(alice) -> dict:
    return auth_headers(alice["session_token"])


def _K(alice) -> dict:
    return auth_headers(alice["api_key"]["plaintext"])


# ---------- GET /me/workspaces ---------- #


def test_list_returns_only_primary_for_fresh_signup(client, alice):
    r = client.get("/me/workspaces", headers=_S(alice))
    assert r.status_code == 200
    rows = r.json()["workspaces"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == alice["workspace"]["id"]
    assert row["role"] == "owner"
    assert row["is_active"] is True
    assert row["plan_tier"] == "free"


def test_list_ordered_by_joined_at_with_primary_first(client, alice):
    """Create two more workspaces; the original (signup-time) one
    stays at the top because joined_at is earliest."""
    r1 = client.post("/me/workspaces", headers=_S(alice), json={"name": "second"})
    assert r1.status_code == 200
    r2 = client.post("/me/workspaces", headers=_S(alice), json={"name": "third"})
    assert r2.status_code == 200

    r = client.get("/me/workspaces", headers=_S(alice))
    rows = r.json()["workspaces"]
    assert len(rows) == 3
    assert rows[0]["id"] == alice["workspace"]["id"]
    assert rows[1]["name"] == "second"
    assert rows[2]["name"] == "third"


def test_list_isolates_workspaces_across_users(client, alice, bob):
    """Alice's list doesn't include bob's workspaces."""
    r = client.get("/me/workspaces", headers=_S(alice))
    ids = {w["id"] for w in r.json()["workspaces"]}
    assert bob["workspace"]["id"] not in ids


def test_list_rejects_api_key(client, alice):
    r = client.get("/me/workspaces", headers=_K(alice))
    assert r.status_code == 401
    assert "session" in r.json()["detail"]


# ---------- POST /me/workspaces ---------- #


def test_create_returns_new_workspace_marked_active(client, alice):
    r = client.post(
        "/me/workspaces", headers=_S(alice), json={"name": "jyni"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "jyni"
    assert body["role"] == "owner"
    assert body["is_active"] is True
    assert body["plan_tier"] == "free"


def test_create_inserts_workspace_member(client, alice):
    r = client.post(
        "/me/workspaces", headers=_S(alice), json={"name": "jyni"},
    )
    new_id = r.json()["id"]
    with session_scope() as s:
        row = s.get(WorkspaceMember, (alice["user"]["id"], new_id))
        assert row is not None
        assert row.role == "owner"


def test_create_flips_session_active_workspace(client, alice):
    """After create, the calling session's active_workspace_id is
    the new workspace — the very next request lands in it."""
    r = client.post(
        "/me/workspaces", headers=_S(alice), json={"name": "jyni"},
    )
    new_id = r.json()["id"]
    # Hit a workspace-scoped endpoint via session-auth; expect to
    # see the NEW workspace's state (empty), not the old one.
    r2 = client.get("/workspaces/me", headers=_S(alice))
    assert r2.status_code == 200
    assert r2.json()["id"] == new_id


def test_create_rejects_blank_name(client, alice):
    r = client.post(
        "/me/workspaces", headers=_S(alice), json={"name": "   "},
    )
    assert r.status_code == 422


def test_create_rejects_oversized_name(client, alice):
    r = client.post(
        "/me/workspaces", headers=_S(alice), json={"name": "x" * 100},
    )
    assert r.status_code == 422


def test_create_rejects_api_key(client, alice):
    r = client.post(
        "/me/workspaces", headers=_K(alice), json={"name": "jyni"},
    )
    assert r.status_code == 401


# ---------- POST /me/workspaces/{id}/switch ---------- #


def test_switch_flips_active_to_target(client, alice):
    r = client.post(
        "/me/workspaces", headers=_S(alice), json={"name": "jyni"},
    )
    new_id = r.json()["id"]
    # Now create a third so we have somewhere to switch back to.
    primary_id = alice["workspace"]["id"]

    rs = client.post(
        f"/me/workspaces/{primary_id}/switch", headers=_S(alice),
    )
    assert rs.status_code == 200
    assert rs.json()["id"] == primary_id
    assert rs.json()["is_active"] is True

    # Confirm via workspace-scoped endpoint.
    r2 = client.get("/workspaces/me", headers=_S(alice))
    assert r2.json()["id"] == primary_id


def test_switch_404_on_non_membership(client, alice, bob):
    """Alice tries to switch to bob's workspace — 404, not 403
    (don't leak existence)."""
    r = client.post(
        f"/me/workspaces/{bob['workspace']['id']}/switch",
        headers=_S(alice),
    )
    assert r.status_code == 404


def test_switch_404_on_garbage_id(client, alice):
    r = client.post(
        f"/me/workspaces/{uuid.uuid4()}/switch", headers=_S(alice),
    )
    assert r.status_code == 404


def test_switch_rejects_api_key(client, alice):
    primary_id = alice["workspace"]["id"]
    r = client.post(
        f"/me/workspaces/{primary_id}/switch", headers=_K(alice),
    )
    assert r.status_code == 401


# ---------- PATCH /me/workspaces/{id} ---------- #


def test_patch_renames_workspace(client, alice):
    primary_id = alice["workspace"]["id"]
    r = client.patch(
        f"/me/workspaces/{primary_id}",
        headers=_S(alice),
        json={"name": "alice's main workspace"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "alice's main workspace"

    # Read-back via list.
    listed = client.get("/me/workspaces", headers=_S(alice)).json()["workspaces"]
    assert listed[0]["name"] == "alice's main workspace"


def test_patch_404_on_non_membership(client, alice, bob):
    r = client.patch(
        f"/me/workspaces/{bob['workspace']['id']}",
        headers=_S(alice),
        json={"name": "stolen"},
    )
    assert r.status_code == 404
    # Bob's workspace name unchanged.
    bob_listed = client.get("/me/workspaces", headers=_S(bob)).json()["workspaces"]
    assert bob_listed[0]["name"] != "stolen"


def test_patch_403_when_member_not_owner(client, alice):
    """Inject a 'member' role row directly (the invite flow that
    will produce these lands in 23B) + confirm PATCH 403s. Verifies
    the owner-only gate ships now so 23B doesn't re-litigate."""
    # Create a new workspace via alice; flip her role to 'member'.
    r = client.post(
        "/me/workspaces", headers=_S(alice), json={"name": "owned-then-demoted"},
    )
    new_id = r.json()["id"]
    with session_scope() as s:
        s.execute(text(
            "UPDATE workspace_members SET role = 'member' "
            "WHERE user_id = :u AND workspace_id = :w"
        ), {"u": alice["user"]["id"], "w": new_id})

    r2 = client.patch(
        f"/me/workspaces/{new_id}",
        headers=_S(alice),
        json={"name": "renamed"},
    )
    assert r2.status_code == 403


def test_patch_rejects_blank_name(client, alice):
    primary_id = alice["workspace"]["id"]
    r = client.patch(
        f"/me/workspaces/{primary_id}",
        headers=_S(alice),
        json={"name": "   "},
    )
    assert r.status_code == 422


def test_patch_rejects_api_key(client, alice):
    primary_id = alice["workspace"]["id"]
    r = client.patch(
        f"/me/workspaces/{primary_id}",
        headers=_K(alice),
        json={"name": "x"},
    )
    assert r.status_code == 401


# ---------- DELETE /me/workspaces/{id} ---------- #


def test_delete_404_on_non_membership(client, alice, bob):
    r = client.delete(
        f"/me/workspaces/{bob['workspace']['id']}", headers=_S(alice),
    )
    assert r.status_code == 404


def test_delete_refuses_last_workspace(client, alice):
    """Alice only has her primary workspace; can't delete it."""
    primary_id = alice["workspace"]["id"]
    r = client.delete(
        f"/me/workspaces/{primary_id}", headers=_S(alice),
    )
    assert r.status_code == 422
    assert "last workspace" in r.json()["detail"]


def test_delete_non_active_workspace(client, alice):
    """Delete a side workspace while the session is active in
    the primary. switched_to is null because no switch was needed."""
    r = client.post(
        "/me/workspaces", headers=_S(alice), json={"name": "side"},
    )
    side_id = r.json()["id"]
    # Switch back to primary so side is non-active.
    primary_id = alice["workspace"]["id"]
    client.post(f"/me/workspaces/{primary_id}/switch", headers=_S(alice))

    r2 = client.delete(f"/me/workspaces/{side_id}", headers=_S(alice))
    assert r2.status_code == 200
    out = r2.json()
    assert out["deleted"] is True
    assert out["switched_to"] is None

    # Side is gone from the list.
    listed = client.get("/me/workspaces", headers=_S(alice)).json()["workspaces"]
    assert not any(w["id"] == side_id for w in listed)


def test_delete_active_workspace_auto_switches(client, alice):
    """When deleting the active workspace, the session auto-flips
    to another workspace the user belongs to — no bounce through
    the picker page (23.6)."""
    r = client.post(
        "/me/workspaces", headers=_S(alice), json={"name": "side"},
    )
    side_id = r.json()["id"]
    # side is now active (create auto-switches).

    r2 = client.delete(f"/me/workspaces/{side_id}", headers=_S(alice))
    assert r2.status_code == 200
    out = r2.json()
    assert out["deleted"] is True
    assert out["switched_to"] == alice["workspace"]["id"]

    # Next session-authed request lands in the primary workspace.
    r3 = client.get("/workspaces/me", headers=_S(alice))
    assert r3.status_code == 200
    assert r3.json()["id"] == alice["workspace"]["id"]


def test_delete_cascades_agents_and_runs(client, alice):
    """Delete a workspace with seeded agents + runs; both are
    gone after via the existing CASCADE FKs."""
    r = client.post(
        "/me/workspaces", headers=_S(alice), json={"name": "doomed"},
    )
    doomed_id = r.json()["id"]
    # Switch back to primary so we can delete doomed via /me/workspaces.
    client.post(
        f"/me/workspaces/{alice['workspace']['id']}/switch",
        headers=_S(alice),
    )

    # Seed an agent + a run into the doomed workspace via the DB
    # directly (endpoint surface would need extra dance).
    now = _now()
    from decimal import Decimal as _Decimal
    with session_scope() as s:
        s.add(Agent(
            workspace_id=doomed_id, name="doomed-bot", role="executor",
            capabilities=[], command_handlers=[],
            created_at=now, updated_at=now,
        ))
        s.add(Run(
            id=str(uuid.uuid4()), workspace_id=doomed_id,
            agent_name="doomed-bot", started_at=now, ended_at=now,
            cost_usd=_Decimal("0"),
        ))

    client.delete(f"/me/workspaces/{doomed_id}", headers=_S(alice))

    with session_scope() as s:
        ws = s.get(Workspace, doomed_id)
        assert ws is None
        agents = s.execute(
            select(Agent).where(Agent.workspace_id == doomed_id)
        ).scalars().all()
        runs = s.execute(
            select(Run).where(Run.workspace_id == doomed_id)
        ).scalars().all()
        assert agents == []
        assert runs == []


def test_delete_403_when_member_not_owner(client, alice):
    """Same gating as PATCH — only owners can delete."""
    r = client.post(
        "/me/workspaces", headers=_S(alice), json={"name": "demote-me"},
    )
    new_id = r.json()["id"]
    with session_scope() as s:
        s.execute(text(
            "UPDATE workspace_members SET role = 'member' "
            "WHERE user_id = :u AND workspace_id = :w"
        ), {"u": alice["user"]["id"], "w": new_id})

    r2 = client.delete(f"/me/workspaces/{new_id}", headers=_S(alice))
    assert r2.status_code == 403


def test_delete_rejects_api_key(client, alice):
    primary_id = alice["workspace"]["id"]
    r = client.delete(f"/me/workspaces/{primary_id}", headers=_K(alice))
    assert r.status_code == 401
