"""Phase 23.2: tests for active-workspace session auth + signup membership.

Three surfaces:

1. Session-token auth resolves the workspace via
   `sess.active_workspace_id` + a `workspace_members` membership
   check (NOT via the legacy `user.workspace_id`).
2. The four 401 conditions: invalid session, no active workspace,
   workspace deleted (FK SET NULL), user not a member of the
   active workspace.
3. API-key auth is unchanged (still reads `api_key.workspace_id`).
4. New signups insert a `workspace_members` row so the freshly
   minted session passes the membership check on its very first
   request.

Switching a session's active_workspace_id is what Phase 23.3 will
ship as the dashboard's "switch workspace" endpoint. Here we
simulate it by writing the column directly so the resolver paths
can be tested without depending on 23.3 endpoints.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from db import session_scope
from models import (
    ApiKey,
    Session as SessionRow,
    User,
    Workspace,
    WorkspaceMember,
)
from tests.conftest import auth_headers, signup


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- New signups insert the WorkspaceMember row ---------- #


def test_signup_inserts_workspace_member_row(client):
    """The new auth resolver requires a workspace_members row to
    accept a session. New signups must insert that row in the same
    transaction or the freshly-minted session would 401 on its
    very first request."""
    out = signup(client, email="member-signup@example.com")
    user_id = out["user"]["id"]
    workspace_id = out["workspace"]["id"]

    with session_scope() as s:
        row = s.get(WorkspaceMember, (user_id, workspace_id))
        assert row is not None
        assert row.role == "owner"


def test_signup_session_works_immediately(client):
    """End-to-end: the signup response's session_token should hit
    a session-authed endpoint and return 200 on the very next
    request — proves the resolver's new membership check accepts
    fresh signups."""
    out = signup(client, email="immediate@example.com")
    r = client.get(
        "/auth/me",
        headers=auth_headers(out["session_token"]),
    )
    assert r.status_code == 200, r.text


# ---------- Session resolves via active_workspace_id ---------- #


def test_session_returns_active_workspace_not_legacy(client, alice):
    """Switch the session's active_workspace_id to a different
    workspace the user is also a member of. The auth resolver
    must return the new workspace, not the legacy
    user.workspace_id."""
    user_id = alice["user"]["id"]
    legacy_ws = alice["workspace"]["id"]

    # Create a second workspace + add alice as a member (simulating
    # what 23.3's POST /me/workspaces will do at the end of Phase 23).
    with session_scope() as s:
        ws_b = str(uuid.uuid4())
        s.add(Workspace(id=ws_b, name="alice-side-project", created_at=_now()))
        s.flush()
        s.add(WorkspaceMember(user_id=user_id, workspace_id=ws_b))
        # Flip the session's active pointer manually.
        s.execute(text(
            "UPDATE sessions SET active_workspace_id = :w "
            "WHERE user_id = :u"
        ), {"w": ws_b, "u": user_id})

    # /auth/me reports the active workspace via the resolver.
    r = client.get(
        "/auth/me",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 200
    # The /auth/me response shape may name the workspace differently;
    # what matters is that the underlying resolver returns ws_b.
    # Verify with a workspace-scoped endpoint that exposes the id.
    rk = client.get(
        "/workspaces/me/api-keys",
        headers=auth_headers(alice["session_token"]),
    )
    assert rk.status_code == 200
    # The keys list will be empty since ws_b has no keys yet — the
    # important thing is the request didn't 401 + didn't return
    # legacy_ws data. (Legacy workspace had a "default" api key from
    # signup.)
    keys = rk.json().get("api_keys", [])
    assert all(k.get("workspace_id", ws_b) == ws_b for k in keys) if keys else True
    # And the legacy workspace's keys aren't visible.
    assert not any(k.get("name") == "default" for k in keys)


# ---------- 401 paths ---------- #


def test_session_with_null_active_workspace_returns_401(client, alice):
    """If a session somehow ends up with NULL active_workspace_id
    (workspace was deleted via FK SET NULL, or legacy session that
    pre-dated the migration backfill), the resolver 401s with a
    distinctive detail so the dashboard middleware can route to the
    workspace picker (23.6)."""
    with session_scope() as s:
        s.execute(text(
            "UPDATE sessions SET active_workspace_id = NULL "
            "WHERE user_id = :u"
        ), {"u": alice["user"]["id"]})

    r = client.get(
        "/auth/me",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 401
    assert "no active workspace" in r.json()["detail"]


def test_session_returns_401_when_user_not_member_of_active(client, alice):
    """Defensive: a stale active_workspace_id pointing at a
    workspace the user never joined (or was removed from in
    23B's invite-revoke flow) yields a clean 401, not a silent
    data leak."""
    with session_scope() as s:
        # Create a workspace alice doesn't belong to.
        orphan_ws = str(uuid.uuid4())
        s.add(Workspace(id=orphan_ws, name="not-yours", created_at=_now()))
        s.flush()
        # Point alice's session at it without inserting membership.
        s.execute(text(
            "UPDATE sessions SET active_workspace_id = :w "
            "WHERE user_id = :u"
        ), {"w": orphan_ws, "u": alice["user"]["id"]})

    r = client.get(
        "/auth/me",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 401
    assert "not a member" in r.json()["detail"]


def test_session_returns_401_after_active_workspace_deleted(client, alice, bob):
    """End-to-end SET NULL path: alice is viewing a second
    workspace, then that workspace gets deleted (we use bob's
    workspace as the proxy, but with alice added as a member +
    her session pointing at it). The FK SET NULL fires on delete,
    leaving alice's active_workspace_id NULL — which surfaces as
    the same 'no active workspace' 401."""
    alice_user_id = alice["user"]["id"]
    bob_ws = bob["workspace"]["id"]

    with session_scope() as s:
        # Make alice a member of bob's workspace + point her
        # session there.
        s.add(WorkspaceMember(user_id=alice_user_id, workspace_id=bob_ws))
        s.execute(text(
            "UPDATE sessions SET active_workspace_id = :w "
            "WHERE user_id = :u"
        ), {"w": bob_ws, "u": alice_user_id})

    # Delete bob's workspace (FK SET NULL fires).
    with session_scope() as s:
        s.delete(s.get(Workspace, bob_ws))

    r = client.get(
        "/auth/me",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 401
    assert "no active workspace" in r.json()["detail"]


def test_session_token_invalid_still_401s(client):
    """Sanity: the existing invalid-session 401 still fires — the
    new active-workspace check doesn't accidentally shadow earlier
    rejection paths."""
    r = client.get(
        "/auth/me",
        headers=auth_headers("bks_totally_bogus"),
    )
    assert r.status_code == 401


# ---------- API-key path unchanged ---------- #


def test_api_key_path_ignores_active_workspace(client, alice):
    """API keys are pinned to their workspace at creation time;
    they don't care about the calling session's active pointer.
    Even if some other session has an active_workspace_id pointing
    elsewhere, the api key continues to resolve to its own
    workspace."""
    # Use alice's api key (workspace = alice's original ws).
    r = client.get(
        "/agents",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200

    # Now disrupt alice's SESSION's active_workspace_id. The api-key
    # request must still work + still resolve to alice's workspace.
    with session_scope() as s:
        s.execute(text(
            "UPDATE sessions SET active_workspace_id = NULL "
            "WHERE user_id = :u"
        ), {"u": alice["user"]["id"]})

    r2 = client.get(
        "/agents",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r2.status_code == 200
    # Session-auth on the same user is broken now, confirming the
    # change isolated to the right path.
    rs = client.get(
        "/auth/me",
        headers=auth_headers(alice["session_token"]),
    )
    assert rs.status_code == 401


def test_api_key_path_works_with_no_workspace_member_row(client, alice):
    """API-key auth doesn't consult workspace_members at all — the
    key's workspace_id is the source of truth. Even if a
    misconfigured workspace had no member rows (impossible via the
    happy paths, but defensively tested), the api key still works."""
    # Wipe the user's membership row entirely; api-key auth should
    # not care.
    with session_scope() as s:
        s.execute(text(
            "DELETE FROM workspace_members WHERE user_id = :u"
        ), {"u": alice["user"]["id"]})

    r = client.get(
        "/agents",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200
