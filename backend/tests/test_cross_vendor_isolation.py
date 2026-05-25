"""Phase 25.6: cross-vendor isolation security suite.

The load-bearing security test for Phase 25. End user Alice is linked
to vendor A AND vendor B with conversations on both. The guarantees:

1. Widget endpoint scoping. Alice's session token resolves only her
   vendor-A conversations against vendor A's public_id, only her
   vendor-B conversations against vendor B's public_id. Same bearer,
   different surface, completely separate threads.

2. Operator inbox scoping. Vendor A's operator can ONLY see Alice's
   vendor-A conversations from `/workspaces/me/inbox`. Vendor A's
   operator hitting Alice's vendor-B conversation_id directly gets
   404, even though it's the same Alice. The workspace_id boundary
   on `widget_conversations` is the gate.

3. Orchestrator payload scoping. The vendor-A bot's `widget.chat`
   command only carries vendor-A conversation history; vendor-B
   data never crosses into vendor-A's command queue. (The orchestrator
   queries by `conv.workspace_id`, so this is implicit; we assert it
   explicitly here as defense-in-depth against a future regression.)

4. Operator take-over scoping. Vendor A's operator cannot take over
   vendor B's conversation even with the conversation_id in hand.

These tests reuse fixtures from 25.4 (`test_widget_end_user_auth.py`)
but the SCENARIO is distinct: a single end user shared across two
vendors, exercising every surface the spec calls out. If anything
in 25.6 starts failing, the multi-vendor isolation guarantee Phase 25
makes is broken, and 26.x consumer chat / 27.x cross-vendor subs
need to wait until it's fixed.
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
    Command,
    EndUser,
    EndUserSession,
    EndUserVendorLink,
    WidgetConversation,
    WidgetMessage,
    Workspace,
)
from tests.conftest import auth_headers, signup
from widget_orchestrator import run_widget_chat_job


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- Scenario setup ---------- #


def _make_vendor_with_operator(
    client,
    *,
    public_id: str,
    operator_email: str,
    workspace_name: str,
    bot_name: str = "vega",
    bot_zone: str = "pii",
) -> tuple[str, str, str]:
    """Spin up a real vendor: operator signs up (gets workspace + session
    token + WorkspaceMember row), then we wire the workspace's widget
    public_id + customer-facing bot in a direct DB write.

    Returns (workspace_id, session_token, bot_name) — caller uses the
    session token to hit operator-authed endpoints.
    """
    sess = signup(
        client, email=operator_email,
        workspace_name=workspace_name,
    )
    ws_id = sess["workspace"]["id"]
    op_token = sess["session_token"]
    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        ws.widget_public_id = public_id
        ws.allowed_widget_origins = ["https://customer.example.com"]
        ws.customer_facing_agent_name = bot_name
        s.add(Agent(
            workspace_id=ws_id,
            name=bot_name,
            role="specialist",
            description=f"customer-facing bot for {workspace_name}",
            sensitivity_level=bot_zone,
            capabilities=["widget:respond", "widget:escalate"],
            command_handlers=[],
            created_at=_now(),
            updated_at=_now(),
        ))
    return ws_id, op_token, bot_name


def _make_alice_linked_to(workspace_ids: list[str]) -> tuple[str, str]:
    """End-user Alice with active vendor links to each workspace.
    Returns (end_user_id, plaintext_session_token)."""
    euid = str(uuid.uuid4())
    token = generate_session_token()
    with session_scope() as s:
        s.add(EndUser(
            id=euid,
            email="alice@example.com",
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
        for ws_id in workspace_ids:
            s.add(EndUserVendorLink(
                end_user_id=euid, workspace_id=ws_id,
            ))
    return euid, token


def _origin() -> dict[str, str]:
    return {"origin": "https://customer.example.com"}


def _eu_auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture
def scenario(client):
    """Set up the standard 25.6 scenario:

      vendor A: workspace_a, operator A (op_token_a), bot 'vega' (pii)
      vendor B: workspace_b, operator B (op_token_b), bot 'atlas' (public)
      Alice: linked to BOTH, holds end-user session token alice_token
      conv_a: Alice's conversation on vendor A (one user msg)
      conv_b: Alice's conversation on vendor B (one user msg)

    Returns a dict with all the handles tests need.
    """
    ws_a, op_a, _ = _make_vendor_with_operator(
        client,
        public_id="wid_vendor_a",
        operator_email="op-a@example.com",
        workspace_name="vendor-a",
        bot_name="vega",
        bot_zone="pii",
    )
    ws_b, op_b, _ = _make_vendor_with_operator(
        client,
        public_id="wid_vendor_b",
        operator_email="op-b@example.com",
        workspace_name="vendor-b",
        bot_name="atlas",
        bot_zone="public",
    )
    euid, alice_token = _make_alice_linked_to([ws_a, ws_b])

    # Alice posts a message on each vendor's widget. The bearer is
    # the same end-user session token; the URL public_id is what
    # scopes the conversation.
    r_a = client.post(
        "/widget/wid_vendor_a/messages",
        headers={**_origin(), **_eu_auth(alice_token)},
        json={"text": "Hi vendor A"},
    )
    assert r_a.status_code == 202, r_a.text
    conv_a_id = r_a.json()["conversation_id"]

    r_b = client.post(
        "/widget/wid_vendor_b/messages",
        headers={**_origin(), **_eu_auth(alice_token)},
        json={"text": "Hi vendor B"},
    )
    assert r_b.status_code == 202, r_b.text
    conv_b_id = r_b.json()["conversation_id"]

    return {
        "ws_a": ws_a,
        "ws_b": ws_b,
        "op_token_a": op_a,
        "op_token_b": op_b,
        "alice_token": alice_token,
        "alice_id": euid,
        "conv_a_id": conv_a_id,
        "conv_b_id": conv_b_id,
    }


# ---------- 1. Widget endpoint scoping ---------- #


def test_alice_polls_vendor_a_conv_via_vendor_a_public_id(client, scenario):
    """Alice's GET on vendor A's public_id with vendor A's conv_id
    returns the conversation. Sanity check; the isolation tests
    below would be vacuous if this didn't work."""
    r = client.get(
        f"/widget/wid_vendor_a/conversations/{scenario['conv_a_id']}",
        headers={**_origin(), **_eu_auth(scenario["alice_token"])},
    )
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert any(m["text"] == "Hi vendor A" for m in msgs)


def test_alice_404s_on_vendor_a_public_id_with_vendor_b_conv_id(client, scenario):
    """Same bearer, vendor A's public_id, vendor B's conv_id = 404.
    Workspace mismatch on conv.workspace_id != workspace.id."""
    r = client.get(
        f"/widget/wid_vendor_a/conversations/{scenario['conv_b_id']}",
        headers={**_origin(), **_eu_auth(scenario["alice_token"])},
    )
    assert r.status_code == 404


def test_alice_404s_on_vendor_b_public_id_with_vendor_a_conv_id(client, scenario):
    """Mirror of the above. Both directions must be blocked."""
    r = client.get(
        f"/widget/wid_vendor_b/conversations/{scenario['conv_a_id']}",
        headers={**_origin(), **_eu_auth(scenario["alice_token"])},
    )
    assert r.status_code == 404


def test_alice_cannot_post_to_vendor_b_conv_via_vendor_a_public_id(client, scenario):
    """Same isolation but for POST. A leaked vendor-B conv_id can't
    be reached by posting against vendor A's public_id."""
    r = client.post(
        "/widget/wid_vendor_a/messages",
        headers={**_origin(), **_eu_auth(scenario["alice_token"])},
        json={
            "text": "cross-vendor attempt",
            "conversation_id": scenario["conv_b_id"],
        },
    )
    assert r.status_code == 404


def test_alice_conversations_are_separate_rows(client, scenario):
    """Conv A and Conv B are two distinct WidgetConversation rows.
    Same end_user_id (Alice), different workspace_id, different
    conversation_id."""
    with session_scope() as s:
        a = s.get(WidgetConversation, scenario["conv_a_id"])
        b = s.get(WidgetConversation, scenario["conv_b_id"])
    assert a.id != b.id
    assert a.workspace_id == scenario["ws_a"]
    assert b.workspace_id == scenario["ws_b"]
    assert a.end_user_id == b.end_user_id == scenario["alice_id"]


# ---------- 2. Operator inbox scoping ---------- #


def test_operator_a_inbox_lists_only_vendor_a_conversations(client, scenario):
    """Operator A's session points at workspace A. The /inbox
    listing must include Alice's conv_a but NOT her conv_b."""
    r = client.get(
        "/workspaces/me/inbox?status=all",
        headers=auth_headers(scenario["op_token_a"]),
    )
    assert r.status_code == 200, r.text
    conv_ids = [c["id"] for c in r.json()["conversations"]]
    assert scenario["conv_a_id"] in conv_ids
    assert scenario["conv_b_id"] not in conv_ids


def test_operator_b_inbox_lists_only_vendor_b_conversations(client, scenario):
    """Mirror: Operator B sees conv_b only."""
    r = client.get(
        "/workspaces/me/inbox?status=all",
        headers=auth_headers(scenario["op_token_b"]),
    )
    assert r.status_code == 200, r.text
    conv_ids = [c["id"] for c in r.json()["conversations"]]
    assert scenario["conv_b_id"] in conv_ids
    assert scenario["conv_a_id"] not in conv_ids


def test_operator_a_404s_on_vendor_b_conv_thread_view(client, scenario):
    """Operator A poking Alice's vendor-B conversation_id directly
    via the thread-view endpoint returns 404. The leak path would
    be: operator finds conv_b_id somewhere (DB, logs, support
    ticket), pastes it into the URL, expects to read the thread.
    Must fail closed."""
    r = client.get(
        f"/workspaces/me/inbox/{scenario['conv_b_id']}",
        headers=auth_headers(scenario["op_token_a"]),
    )
    assert r.status_code == 404


def test_operator_a_cannot_take_over_vendor_b_conv(client, scenario):
    """Same 404 on the take-over endpoint. If take-over leaked, the
    operator could pause vendor B's bot and impersonate it in a
    customer's view."""
    r = client.post(
        f"/workspaces/me/inbox/{scenario['conv_b_id']}/take-over",
        headers=auth_headers(scenario["op_token_a"]),
    )
    assert r.status_code == 404


def test_operator_a_cannot_resolve_vendor_b_conv(client, scenario):
    r = client.post(
        f"/workspaces/me/inbox/{scenario['conv_b_id']}/resolve",
        headers=auth_headers(scenario["op_token_a"]),
    )
    assert r.status_code == 404


# ---------- 3. Orchestrator payload scoping ---------- #


def test_vendor_a_orchestrator_payload_contains_only_vendor_a_history(client, scenario):
    """Drive the vendor-A orchestrator directly and assert the
    widget.chat command payload carries vendor-A's conversation
    history only. Vendor B's data doesn't cross the workspace
    boundary even though the same end_user is on both sides."""
    # Add a bot reply on each side so history is non-trivially scoped.
    with session_scope() as s:
        s.add(WidgetMessage(
            conversation_id=scenario["conv_a_id"],
            role="bot",
            text="vendor-A bot reply",
            sent_at=_now(),
        ))
        s.add(WidgetMessage(
            conversation_id=scenario["conv_b_id"],
            role="bot",
            text="vendor-B bot reply",
            sent_at=_now(),
        ))

    # Capture the message id for vendor A's new turn so the
    # orchestrator can split it from history.
    with session_scope() as s:
        s.add(WidgetMessage(
            conversation_id=scenario["conv_a_id"],
            role="user",
            text="vendor-A second turn",
            sent_at=_now(),
        ))
        s.flush()
        latest_a_msg_id = s.execute(
            select(WidgetMessage.id)
            .where(WidgetMessage.conversation_id == scenario["conv_a_id"])
            .order_by(WidgetMessage.id.desc())
            .limit(1)
        ).scalar_one()

    with session_scope() as s:
        result = run_widget_chat_job(s, scenario["ws_a"], {
            "conversation_id": scenario["conv_a_id"],
            "user_message_id": latest_a_msg_id,
        })

    assert result["status"] == "dispatched"
    with session_scope() as s:
        cmd = s.get(Command, result["command_id"])
        history_texts = [m["text"] for m in cmd.payload["conversation_history"]]
        # Vendor A's history is here.
        assert "Hi vendor A" in history_texts
        assert "vendor-A bot reply" in history_texts
        # Vendor B's history is NOT here, no matter what.
        assert "Hi vendor B" not in history_texts
        assert "vendor-B bot reply" not in history_texts
        # The command itself is bound to vendor A's workspace + bot.
        assert cmd.workspace_id == scenario["ws_a"]
        assert cmd.agent_name == "vega"
        # End-user identity flows through (Alice is on both vendors;
        # the payload is hers but workspace-scoped). The PII zone of
        # vendor A's bot means the email is included.
        assert cmd.payload["end_user"]["id"] == scenario["alice_id"]
        assert cmd.payload["end_user"]["email"] == "alice@example.com"
        assert cmd.payload["end_user"]["sensitivity_hint"] == "pii"


def test_vendor_b_orchestrator_payload_redacts_email_for_public_bot(client, scenario):
    """Mirror: vendor B's atlas bot is in the public zone. The
    orchestrator passes sensitivity_hint='public', so when atlas
    reads `lightsei.end_user.email` in its handler it gets None
    (verified by SDK tests). Here we confirm the BACKEND side
    sets sensitivity_hint='public' for atlas, which is the wire
    contract the SDK accessor enforces."""
    # Latest message id for vendor B's existing turn.
    with session_scope() as s:
        latest_b_msg_id = s.execute(
            select(WidgetMessage.id)
            .where(WidgetMessage.conversation_id == scenario["conv_b_id"])
            .order_by(WidgetMessage.id.desc())
            .limit(1)
        ).scalar_one()

    with session_scope() as s:
        result = run_widget_chat_job(s, scenario["ws_b"], {
            "conversation_id": scenario["conv_b_id"],
            "user_message_id": latest_b_msg_id,
        })

    assert result["status"] == "dispatched"
    with session_scope() as s:
        cmd = s.get(Command, result["command_id"])
        assert cmd.workspace_id == scenario["ws_b"]
        assert cmd.agent_name == "atlas"
        # End user id + display_name still flow (those aren't
        # zone-gated), but the sensitivity_hint tells the SDK to
        # redact email on read.
        assert cmd.payload["end_user"]["id"] == scenario["alice_id"]
        assert cmd.payload["end_user"]["display_name"] == "Alice"
        assert cmd.payload["end_user"]["sensitivity_hint"] == "public"


# ---------- 4. Cross-workspace conversation pollution ---------- #


def test_alice_second_message_on_vendor_a_lands_only_in_vendor_a(client, scenario):
    """Sanity: a follow-up message on vendor A doesn't accidentally
    bleed into vendor B's message table. Catches any future bug
    where the conversation_id => workspace mapping gets crossed."""
    r = client.post(
        "/widget/wid_vendor_a/messages",
        headers={**_origin(), **_eu_auth(scenario["alice_token"])},
        json={
            "text": "follow-up A",
            "conversation_id": scenario["conv_a_id"],
        },
    )
    assert r.status_code == 202

    with session_scope() as s:
        # Vendor A messages now: original "Hi vendor A" + "follow-up A"
        a_msgs = s.execute(
            select(WidgetMessage)
            .where(WidgetMessage.conversation_id == scenario["conv_a_id"])
            .order_by(WidgetMessage.id)
        ).scalars().all()
        a_texts = [m.text for m in a_msgs]
        assert "follow-up A" in a_texts

        # Vendor B messages: just the original "Hi vendor B".
        # follow-up A must NOT be here.
        b_msgs = s.execute(
            select(WidgetMessage)
            .where(WidgetMessage.conversation_id == scenario["conv_b_id"])
        ).scalars().all()
        b_texts = [m.text for m in b_msgs]
        assert "follow-up A" not in b_texts
        assert b_texts == ["Hi vendor B"]
