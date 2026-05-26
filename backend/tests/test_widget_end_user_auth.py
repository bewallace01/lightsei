"""Phase 25.4: widget endpoints accept end-user identity.

Three surfaces:

1. Identified end users (linked to the vendor workspace) can post +
   poll their own conversations; new conversations get
   `widget_conversations.end_user_id` stamped. Their own threads are
   reachable; other identified users' threads + anonymous threads
   are 404.
2. Anonymous callers (no Authorization header) keep the existing
   v1 behavior: anon_user_id-only conversations are reachable;
   identified threads return 404 so a leaked id can't be polled
   anonymously.
3. Cross-vendor isolation: an end user linked to vendor A AND vendor
   B sees only vendor-A conversations when posting to vendor A's
   public_id, and only vendor-B conversations when posting to vendor
   B's public_id, even though the same session token is valid against
   both.

Plus: invalid bearer is 401 (no silent degrade to anonymous), and
an end user with no link to this vendor falls back to anonymous
behavior (per spec).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from db import session_scope
from keys import generate_session_token, hash_token
from models import (
    Agent,
    EndUser,
    EndUserSession,
    EndUserVendorLink,
    WidgetConversation,
    WidgetMessage,
    Workspace,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_widget_workspace(
    *,
    public_id: str | None = None,
    allowed_origins: list[str] | None = None,
    customer_facing_agent_name: str | None = "vega",
) -> str:
    """Mirrors the helper in test_widget_endpoints.py but defaults
    to a fresh public_id per call so multiple workspaces in one
    test don't collide on the unique constraint."""
    ws_id = str(uuid.uuid4())
    public_id = public_id or f"wid_{ws_id[:8]}"
    with session_scope() as s:
        s.add(Workspace(
            id=ws_id,
            name=f"vendor-{ws_id[:8]}",
            created_at=_now(),
            widget_public_id=public_id,
            allowed_widget_origins=allowed_origins or [
                "https://customer.example.com",
            ],
            customer_facing_agent_name=customer_facing_agent_name,
        ))
        s.flush()
        if customer_facing_agent_name:
            s.add(Agent(
                workspace_id=ws_id,
                name=customer_facing_agent_name,
                role="specialist",
                description="Customer-facing bot.",
                sensitivity_level="public",
                capabilities=["widget:respond", "widget:escalate"],
                command_handlers=[],
                created_at=_now(),
                updated_at=_now(),
            ))
    return ws_id, public_id


def _origin(host: str = "https://customer.example.com") -> dict[str, str]:
    return {"origin": host}


def _make_end_user_with_session(
    *,
    email: str | None = None,
    link_to_workspace_ids: list[str] | None = None,
) -> tuple[str, str]:
    """Returns (end_user_id, plaintext_token).

    `link_to_workspace_ids` inserts active vendor links so the
    widget endpoint accepts this end user as identified on those
    workspaces.
    """
    euid = str(uuid.uuid4())
    token = generate_session_token()
    with session_scope() as s:
        s.add(EndUser(
            id=euid,
            email=email or f"eu-{euid[:8]}@example.com",
        ))
        s.flush()
        s.add(EndUserSession(
            id=str(uuid.uuid4()),
            end_user_id=euid,
            token_hash=hash_token(token),
            created_at=_now(),
            expires_at=_now() + timedelta(days=30),
        ))
        for ws_id in link_to_workspace_ids or []:
            s.add(EndUserVendorLink(
                end_user_id=euid, workspace_id=ws_id,
            ))
    return euid, token


def _auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _post(client, public_id: str, *, headers: dict[str, str], **body):
    h = {**_origin(), **headers}
    return client.post(
        f"/widget/{public_id}/messages", headers=h, json={"text": "hi", **body},
    )


def _poll(client, public_id: str, conv_id: str, *, headers: dict[str, str]):
    h = {**_origin(), **headers}
    return client.get(
        f"/widget/{public_id}/conversations/{conv_id}", headers=h,
    )


# ---------- Identified happy path ---------- #


def test_identified_post_new_conversation_stamps_end_user_id(client):
    """When a linked end user posts, the new conversation row gets
    end_user_id set (and anon_user_id stays NULL even if the body
    sent one)."""
    ws_id, public_id = _make_widget_workspace()
    euid, token = _make_end_user_with_session(link_to_workspace_ids=[ws_id])

    r = _post(
        client, public_id, headers=_auth(token), anon_user_id="anon-junk",
    )
    assert r.status_code == 202, r.text
    conv_id = r.json()["conversation_id"]

    with session_scope() as s:
        conv = s.get(WidgetConversation, conv_id)
        assert conv.end_user_id == euid
        assert conv.anon_user_id is None  # identified path doesn't carry anon
        assert conv.workspace_id == ws_id


def test_identified_can_poll_own_conversation(client):
    ws_id, public_id = _make_widget_workspace()
    _, token = _make_end_user_with_session(link_to_workspace_ids=[ws_id])

    r1 = _post(client, public_id, headers=_auth(token))
    conv_id = r1.json()["conversation_id"]

    r2 = _poll(client, public_id, conv_id, headers=_auth(token))
    assert r2.status_code == 200
    msgs = r2.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"


def test_identified_can_continue_their_existing_conversation(client):
    """Second POST with the same conversation_id appends a message."""
    ws_id, public_id = _make_widget_workspace()
    _, token = _make_end_user_with_session(link_to_workspace_ids=[ws_id])

    r1 = _post(client, public_id, headers=_auth(token))
    conv_id = r1.json()["conversation_id"]

    r2 = _post(
        client, public_id, headers=_auth(token),
        conversation_id=conv_id, text="follow-up",
    )
    assert r2.status_code == 202
    assert r2.json()["conversation_id"] == conv_id

    with session_scope() as s:
        msgs = s.execute(
            select(WidgetMessage)
            .where(WidgetMessage.conversation_id == conv_id)
            .order_by(WidgetMessage.id)
        ).scalars().all()
    assert [m.text for m in msgs] == ["hi", "follow-up"]


# ---------- Identified isolation ---------- #


def test_identified_cannot_reach_other_users_conversation(client):
    """Alice and Bob both linked to the same vendor. Alice posts a
    conversation; Bob cannot poll it or post to it. Bob's bearer
    token + Alice's conv_id = 404."""
    ws_id, public_id = _make_widget_workspace()
    _, token_a = _make_end_user_with_session(
        email="alice@example.com", link_to_workspace_ids=[ws_id],
    )
    _, token_b = _make_end_user_with_session(
        email="bob@example.com", link_to_workspace_ids=[ws_id],
    )

    r1 = _post(client, public_id, headers=_auth(token_a))
    alice_conv_id = r1.json()["conversation_id"]

    # Bob can't poll Alice's conv.
    r_poll = _poll(client, public_id, alice_conv_id, headers=_auth(token_b))
    assert r_poll.status_code == 404

    # Bob can't post to Alice's conv either.
    r_post = _post(
        client, public_id, headers=_auth(token_b),
        conversation_id=alice_conv_id,
    )
    assert r_post.status_code == 404


def test_identified_cannot_reach_anonymous_conversation(client):
    """An anonymous user posts a conversation; an identified user
    can't poll or post to it. anonymous conv has end_user_id=NULL,
    identified caller's current_end_user_id != NULL, mismatch = 404."""
    ws_id, public_id = _make_widget_workspace()
    _, token = _make_end_user_with_session(link_to_workspace_ids=[ws_id])

    # Anonymous post.
    r_anon = client.post(
        f"/widget/{public_id}/messages",
        headers=_origin(),
        json={"text": "anon-msg", "anon_user_id": "anon-x"},
    )
    assert r_anon.status_code == 202
    anon_conv_id = r_anon.json()["conversation_id"]

    # Identified caller hits 404 on the anonymous conv id.
    r_poll = _poll(client, public_id, anon_conv_id, headers=_auth(token))
    assert r_poll.status_code == 404


# ---------- Anonymous behavior ---------- #


def test_anonymous_keeps_existing_behavior(client):
    """No Authorization header = existing v1 anonymous flow."""
    ws_id, public_id = _make_widget_workspace()

    r = client.post(
        f"/widget/{public_id}/messages",
        headers=_origin(),
        json={"text": "hello", "anon_user_id": "anon-1"},
    )
    assert r.status_code == 202
    conv_id = r.json()["conversation_id"]
    with session_scope() as s:
        conv = s.get(WidgetConversation, conv_id)
        assert conv.end_user_id is None
        assert conv.anon_user_id == "anon-1"


def test_anonymous_cannot_reach_identified_conversation(client):
    """Identified caller starts a thread; an anonymous request to
    the same conv_id gets 404 so a leaked id can't be polled
    without auth."""
    ws_id, public_id = _make_widget_workspace()
    _, token = _make_end_user_with_session(link_to_workspace_ids=[ws_id])
    r_eu = _post(client, public_id, headers=_auth(token))
    eu_conv_id = r_eu.json()["conversation_id"]

    # Anonymous poll of the identified conv.
    r_anon = client.get(
        f"/widget/{public_id}/conversations/{eu_conv_id}",
        headers=_origin(),
    )
    assert r_anon.status_code == 404


# ---------- Unlinked end-user fallback ---------- #


def test_unlinked_end_user_falls_back_to_anonymous(client):
    """An end user with a valid session but NO link to this workspace
    (or whose link is soft-revoked) is treated as anonymous per the
    25.4 spec: 'When absent or unlinked, falls back to existing
    anonymous behavior.'"""
    ws_id, public_id = _make_widget_workspace()
    _, token = _make_end_user_with_session(link_to_workspace_ids=[])

    r = _post(
        client, public_id, headers=_auth(token), anon_user_id="anon-fb",
    )
    assert r.status_code == 202
    conv_id = r.json()["conversation_id"]
    with session_scope() as s:
        conv = s.get(WidgetConversation, conv_id)
        assert conv.end_user_id is None  # fell back to anonymous
        assert conv.anon_user_id == "anon-fb"


# ---------- Invalid bearer is strict ---------- #


def test_invalid_bearer_returns_401(client):
    """Present-but-invalid token doesn't silently degrade to
    anonymous — that would dump session-private state into a
    public conversation."""
    ws_id, public_id = _make_widget_workspace()
    r = _post(client, public_id, headers=_auth("not-a-real-token"))
    assert r.status_code == 401


def test_operator_session_bearer_returns_401(client):
    """Cross-token-type guard from 25.3 fires at the widget surface
    too. Operator's session token used on the public widget endpoint
    = 401."""
    from models import (
        Session as SessionRow,
        User,
        Workspace as Ws,
        WorkspaceMember,
    )

    ws_id, public_id = _make_widget_workspace()

    # Mint an operator session row.
    op_token = generate_session_token()
    with session_scope() as s:
        op_ws = Ws(
            id=str(uuid.uuid4()),
            name="op-ws",
            created_at=_now(),
        )
        s.add(op_ws)
        s.flush()
        uid = str(uuid.uuid4())
        s.add(User(
            id=uid,
            email=f"op-{uid[:8]}@example.com",
            password_hash="x",
            workspace_id=op_ws.id,
            created_at=_now(),
        ))
        s.flush()
        s.add(WorkspaceMember(user_id=uid, workspace_id=op_ws.id))
        s.add(SessionRow(
            id=str(uuid.uuid4()),
            user_id=uid,
            token_hash=hash_token(op_token),
            created_at=_now(),
            expires_at=_now() + timedelta(days=30),
            active_workspace_id=op_ws.id,
        ))

    r = _post(client, public_id, headers=_auth(op_token))
    assert r.status_code == 401


# ---------- Cross-vendor isolation ---------- #


def test_cross_vendor_isolation_same_end_user_two_vendors(client):
    """End user Alice linked to BOTH vendor A and vendor B. She
    starts a conversation on vendor A's widget. Polling that
    conversation via vendor B's public_id = 404 (different
    workspace_id). Starting a fresh conversation on vendor B's
    widget creates a separate row scoped to vendor B; the two
    don't bleed."""
    ws_a, pub_a = _make_widget_workspace()
    ws_b, pub_b = _make_widget_workspace()
    _, token = _make_end_user_with_session(
        link_to_workspace_ids=[ws_a, ws_b],
    )

    # Alice posts on vendor A.
    r_a = _post(client, pub_a, headers=_auth(token))
    conv_a_id = r_a.json()["conversation_id"]

    # Polling vendor-A conv via vendor-B's public_id = 404 (workspace
    # mismatch).
    r_cross = _poll(client, pub_b, conv_a_id, headers=_auth(token))
    assert r_cross.status_code == 404

    # Posting fresh on vendor B = new conv row scoped to vendor B.
    r_b = _post(client, pub_b, headers=_auth(token))
    conv_b_id = r_b.json()["conversation_id"]
    assert conv_b_id != conv_a_id
    with session_scope() as s:
        conv_a = s.get(WidgetConversation, conv_a_id)
        conv_b = s.get(WidgetConversation, conv_b_id)
        assert conv_a.workspace_id == ws_a
        assert conv_b.workspace_id == ws_b
        # Same end_user_id on both — that's correct; the workspace
        # boundary is the isolation, not the identity.
        assert conv_a.end_user_id == conv_b.end_user_id


# ---------- Phase 27.6: soft-revoke read-only ---------- #


def test_soft_revoked_end_user_can_still_poll_past_conversation(client):
    """Phase 27 spec: 'past conversations stay accessible to the end
    user (read-only); no new messages can be sent.'

    Setup: end user linked + posts a conversation. Then the link
    gets soft-revoked (removed_at set). The end user can still GET
    the thread (read path), but POSTing a new message goes through
    the anonymous-fallback path and 404s on the identified conv id.
    """
    from models import EndUserVendorLink
    ws_id, public_id = _make_widget_workspace()
    _, token = _make_end_user_with_session(link_to_workspace_ids=[ws_id])

    # Send a message while still actively linked — creates conv.
    r1 = _post(client, public_id, headers=_auth(token))
    conv_id = r1.json()["conversation_id"]

    # Soft-revoke the link (simulates DELETE /me/end-user/vendors/{id}).
    with session_scope() as s:
        # The link's end_user_id is whatever _make_end_user_with_session
        # produced; look it up by workspace.
        from sqlalchemy import select as _sel
        link = s.execute(_sel(EndUserVendorLink).where(
            EndUserVendorLink.workspace_id == ws_id,
        )).scalar_one()
        link.removed_at = _now()

    # GET (read path) STILL works with the same bearer.
    r_get = _poll(client, public_id, conv_id, headers=_auth(token))
    assert r_get.status_code == 200, (
        "soft-revoked end user must keep read access to past conversations"
    )
    msgs = r_get.json()["messages"]
    assert len(msgs) >= 1

    # POST (write path) loses identified status — falls back to
    # anonymous, which 404s on the identified conv id.
    r_post = _post(
        client, public_id, headers=_auth(token),
        conversation_id=conv_id,
    )
    assert r_post.status_code == 404, (
        "soft-revoked end user must NOT be able to post new messages "
        "into the previously-linked vendor's conversations"
    )


def test_soft_revoked_end_user_cannot_start_new_conversation(client):
    """The unsubscribed end user posting WITHOUT conversation_id
    creates a fresh anonymous conversation (no end_user_id stamp)
    rather than a new identified one. Confirms the write path drops
    identity on soft-revoke."""
    from models import EndUserVendorLink
    ws_id, public_id = _make_widget_workspace()
    eu_id, token = _make_end_user_with_session(
        link_to_workspace_ids=[ws_id],
    )
    # Soft-revoke immediately.
    with session_scope() as s:
        from sqlalchemy import select as _sel
        link = s.execute(_sel(EndUserVendorLink).where(
            EndUserVendorLink.workspace_id == ws_id,
            EndUserVendorLink.end_user_id == eu_id,
        )).scalar_one()
        link.removed_at = _now()

    r = _post(
        client, public_id, headers=_auth(token), anon_user_id="anon-fb",
    )
    assert r.status_code == 202
    conv_id = r.json()["conversation_id"]
    with session_scope() as s:
        conv = s.get(WidgetConversation, conv_id)
        # End-user identity dropped on the write path; conv lands
        # anonymous even though bearer is technically valid.
        assert conv.end_user_id is None
        assert conv.anon_user_id == "anon-fb"
