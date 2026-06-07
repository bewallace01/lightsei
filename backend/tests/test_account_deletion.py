"""Phase 31.5.g: in-app account deletion (Apple guideline 5.1.1(v)).

Two endpoints, one per identity:

  DELETE /auth/account    -> operator (session user). Owned workspaces
                             cascade away with the user.
  DELETE /me/end-user     -> end user. Sessions / vendor links / push /
                             apns cascade; widget conversations keep
                             their transcript with end_user_id nulled.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from db import session_scope
from keys import generate_session_token, hash_token
from models import (
    Agent,
    EndUser,
    EndUserSession,
    EndUserVendorLink,
    User,
    WidgetConversation,
    Workspace,
    WorkspaceMember,
)
from tests.conftest import auth_headers, signup


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- DELETE /auth/account (operator) ---------- #


def test_delete_operator_account_removes_user_and_owned_workspace(client):
    alice = signup(client, email="del-op@example.com", workspace_name="del-co")
    ws_id = alice["workspace"]["id"]
    user_id = alice["user"]["id"]

    # Drop a workspace-scoped row in to prove the cascade reaches it.
    with session_scope() as s:
        s.add(Agent(
            workspace_id=ws_id,
            name="argus",
            role="specialist",
            description="bot",
            sensitivity_level="public",
            capabilities=[],
            command_handlers=[],
            created_at=_now(),
            updated_at=_now(),
        ))

    r = client.delete("/auth/account", headers=auth_headers(alice["session_token"]))
    assert r.status_code == 200
    assert r.json() == {"deleted": True}

    with session_scope() as s:
        assert s.get(User, user_id) is None
        assert s.get(Workspace, ws_id) is None
        assert s.execute(
            WorkspaceMember.__table__.select().where(
                WorkspaceMember.user_id == user_id
            )
        ).first() is None
        assert s.execute(
            Agent.__table__.select().where(Agent.workspace_id == ws_id)
        ).first() is None


def test_delete_operator_account_invalidates_session(client):
    alice = signup(client, email="del-op2@example.com", workspace_name="del-co2")
    token = alice["session_token"]

    assert client.delete("/auth/account", headers=auth_headers(token)).status_code == 200

    # Session row cascaded away with the user, so the token is dead.
    r = client.get("/me/workspaces", headers=auth_headers(token))
    assert r.status_code == 401


def test_delete_operator_account_rejects_api_key(client):
    alice = signup(client, email="del-op3@example.com", workspace_name="del-co3")
    r = client.delete(
        "/auth/account",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 401


def test_delete_operator_account_requires_auth(client):
    assert client.delete("/auth/account").status_code in (401, 403)


# ---------- DELETE /me/end-user (end user) ---------- #


def _make_end_user(*, email: str | None = None) -> tuple[str, str]:
    """Returns (end_user_id, plaintext_session_token)."""
    euid = str(uuid.uuid4())
    token = generate_session_token()
    with session_scope() as s:
        s.add(EndUser(
            id=euid,
            email=email or f"eu-{euid[:8]}@example.com",
            display_name="Alice",
        ))
        s.flush()
        s.add(EndUserSession(
            id=str(uuid.uuid4()),
            end_user_id=euid,
            token_hash=hash_token(token),
            created_at=_now(),
            expires_at=_now() + timedelta(days=30),
        ))
    return euid, token


def _make_vendor() -> str:
    ws_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(
            id=ws_id,
            name=f"vendor-{ws_id[:8]}",
            created_at=_now(),
            vendor_slug=f"slug-{ws_id[:8]}",
            widget_public_id=f"wid_{ws_id[:8]}",
            customer_facing_agent_name="vega",
        ))
    return ws_id


def _eu_auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def test_delete_end_user_account_cascades_but_keeps_conversation(client):
    euid, token = _make_end_user(email="del-eu@example.com")
    ws_id = _make_vendor()
    conv_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(EndUserVendorLink(end_user_id=euid, workspace_id=ws_id))
        s.add(WidgetConversation(
            id=conv_id,
            workspace_id=ws_id,
            customer_facing_agent_name="vega",
            status="open",
            end_user_id=euid,
            started_at=_now(),
            last_message_at=_now(),
        ))

    r = client.delete("/me/end-user", headers=_eu_auth(token))
    assert r.status_code == 200
    assert r.json() == {"deleted": True}

    with session_scope() as s:
        assert s.get(EndUser, euid) is None
        assert s.execute(
            EndUserSession.__table__.select().where(
                EndUserSession.end_user_id == euid
            )
        ).first() is None
        assert s.execute(
            EndUserVendorLink.__table__.select().where(
                EndUserVendorLink.end_user_id == euid
            )
        ).first() is None
        # Conversation transcript survives, unlinked from the person.
        conv = s.get(WidgetConversation, conv_id)
        assert conv is not None
        assert conv.end_user_id is None


def test_delete_end_user_account_invalidates_session(client):
    _, token = _make_end_user(email="del-eu2@example.com")
    assert client.delete("/me/end-user", headers=_eu_auth(token)).status_code == 200
    assert client.get("/me/end-user", headers=_eu_auth(token)).status_code == 401


def test_delete_end_user_account_requires_auth(client):
    assert client.delete("/me/end-user").status_code in (401, 403)
