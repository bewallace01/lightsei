"""Phase 21.8: tests for the operator inbox endpoints.

Five endpoints:

  GET  /workspaces/me/inbox                          — list + filter
  GET  /workspaces/me/inbox/{conversation_id}        — thread view
  POST /workspaces/me/inbox/{conversation_id}/take-over
  POST /workspaces/me/inbox/{conversation_id}/messages
  POST /workspaces/me/inbox/{conversation_id}/resolve
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from db import session_scope
from models import (
    Agent,
    Workspace,
    WidgetConversation,
    WidgetEscalation,
    WidgetMessage,
)
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add_agent(
    workspace_id: str,
    name: str,
    *,
    capabilities: list[str] | None = None,
    sensitivity_level: str = "public",
) -> None:
    with session_scope() as s:
        s.add(Agent(
            workspace_id=workspace_id,
            name=name,
            role="specialist",
            sensitivity_level=sensitivity_level,
            capabilities=capabilities or [],
            command_handlers=[],
            created_at=_now(),
            updated_at=_now(),
        ))


def _start_conversation(
    workspace_id: str,
    *,
    agent_name: str = "vega",
    status: str = "open",
) -> str:
    conv_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(WidgetConversation(
            id=conv_id,
            workspace_id=workspace_id,
            customer_facing_agent_name=agent_name,
            status=status,
            anon_user_id="anon-test",
            started_at=_now(),
            last_message_at=_now(),
        ))
    return conv_id


def _add_message(conv_id: str, role: str, text: str) -> int:
    with session_scope() as s:
        s.add(WidgetMessage(
            conversation_id=conv_id,
            role=role,
            text=text,
            sent_at=_now(),
        ))
        s.flush()
        msg_id = s.execute(
            select(WidgetMessage.id)
            .where(WidgetMessage.conversation_id == conv_id)
            .order_by(WidgetMessage.id.desc())
            .limit(1)
        ).scalar_one()
    return msg_id


def _add_open_escalation(conv_id: str, reason: str = "bot_escalate_call") -> str:
    esc_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(WidgetEscalation(
            id=esc_id,
            conversation_id=conv_id,
            reason=reason,
            payload={"hint": "test"},
            escalated_at=_now(),
        ))
    return esc_id


# ---------- GET /workspaces/me/inbox ---------- #


def test_list_inbox_default_returns_active_conversations(client, alice):
    """Default filter ('active') returns open + escalated +
    operator_owned. Resolved is excluded."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    open_id = _start_conversation(ws_id, status="open")
    escalated_id = _start_conversation(ws_id, status="escalated")
    operator_id = _start_conversation(ws_id, status="operator_owned")
    resolved_id = _start_conversation(ws_id, status="resolved")

    r = client.get(
        "/workspaces/me/inbox",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [c["id"] for c in body["conversations"]]
    assert open_id in ids
    assert escalated_id in ids
    assert operator_id in ids
    assert resolved_id not in ids
    assert body["filter"] == "active"
    assert body["as_of"]  # server timestamp


def test_list_inbox_escalated_first_then_recent(client, alice):
    """Escalated conversations bump to the top regardless of when
    they last had activity; within each status group, most-recent
    first."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")

    # Open conversation with very recent activity.
    open_id = _start_conversation(ws_id, status="open")
    _add_message(open_id, "user", "open-recent")
    with session_scope() as s:
        s.get(WidgetConversation, open_id).last_message_at = _now()

    # Escalated conversation with OLDER activity. Should still bump
    # to the top of the list.
    escalated_id = _start_conversation(ws_id, status="escalated")
    with session_scope() as s:
        # Force older timestamp.
        s.get(WidgetConversation, escalated_id).last_message_at = datetime(
            2020, 1, 1, tzinfo=timezone.utc,
        )

    r = client.get(
        "/workspaces/me/inbox",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    ids = [c["id"] for c in r.json()["conversations"]]
    assert ids[0] == escalated_id  # escalated bumped above the more-recent open one


def test_list_inbox_status_filter(client, alice):
    """Each explicit status filter returns only that status."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    open_id = _start_conversation(ws_id, status="open")
    escalated_id = _start_conversation(ws_id, status="escalated")

    r = client.get(
        "/workspaces/me/inbox?status=escalated",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    ids = [c["id"] for c in r.json()["conversations"]]
    assert ids == [escalated_id]
    assert open_id not in ids


def test_list_inbox_invalid_status_422(client, alice):
    r = client.get(
        "/workspaces/me/inbox?status=foo",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 422


def test_list_inbox_surfaces_preview_and_zone(client, alice):
    """Each row carries last_message_preview + sensitivity_level
    snapshotted from the agent."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", sensitivity_level="public")
    conv_id = _start_conversation(ws_id)
    _add_message(conv_id, "user", "How do I cancel?")

    r = client.get(
        "/workspaces/me/inbox",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    row = next(c for c in r.json()["conversations"] if c["id"] == conv_id)
    assert row["last_message_preview"] == "How do I cancel?"
    assert row["last_message_role"] == "user"
    assert row["sensitivity_level"] == "public"
    assert row["customer_facing_agent_name"] == "vega"


def test_list_inbox_truncates_long_preview(client, alice):
    """Preview is capped at INBOX_PREVIEW_CHARS with an ellipsis."""
    from main import INBOX_PREVIEW_CHARS
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    conv_id = _start_conversation(ws_id)
    long_text = "x" * (INBOX_PREVIEW_CHARS + 50)
    _add_message(conv_id, "user", long_text)

    r = client.get(
        "/workspaces/me/inbox",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    row = next(c for c in r.json()["conversations"] if c["id"] == conv_id)
    assert row["last_message_preview"].endswith("…")
    assert len(row["last_message_preview"]) == INBOX_PREVIEW_CHARS + 1  # "…" added


def test_list_inbox_open_escalation_count(client, alice):
    """open_escalation_count counts unresolved escalation rows."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    conv_id = _start_conversation(ws_id, status="escalated")
    _add_open_escalation(conv_id, reason="reason1")
    _add_open_escalation(conv_id, reason="reason2")

    r = client.get(
        "/workspaces/me/inbox",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    row = next(c for c in r.json()["conversations"] if c["id"] == conv_id)
    assert row["open_escalation_count"] == 2


def test_list_inbox_since_cursor(client, alice):
    """?since=<iso> filters to conversations with last_message_at >
    cursor — drives polling refresh."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    old_id = _start_conversation(ws_id)
    with session_scope() as s:
        s.get(WidgetConversation, old_id).last_message_at = datetime(
            2020, 1, 1, tzinfo=timezone.utc,
        )

    new_id = _start_conversation(ws_id)
    with session_scope() as s:
        s.get(WidgetConversation, new_id).last_message_at = datetime(
            2026, 6, 1, tzinfo=timezone.utc,
        )

    r = client.get(
        "/workspaces/me/inbox?since=2026-01-01T00:00:00Z",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    ids = [c["id"] for c in r.json()["conversations"]]
    assert new_id in ids
    assert old_id not in ids


def test_list_inbox_tenant_isolation(client, alice):
    """Other workspaces' conversations don't leak in."""
    other_ws = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(id=other_ws, name="other-co", created_at=_now()))
    _start_conversation(other_ws, agent_name="other-bot")

    r = client.get(
        "/workspaces/me/inbox",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.json()["conversations"] == []


def test_list_inbox_handles_deleted_agent(client, alice):
    """If the customer-facing bot was deleted, sensitivity_level
    falls back to null rather than crashing."""
    ws_id = alice["workspace"]["id"]
    conv_id = _start_conversation(ws_id, agent_name="ghost")
    # No agent row added.

    r = client.get(
        "/workspaces/me/inbox",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    row = next(c for c in r.json()["conversations"] if c["id"] == conv_id)
    assert row["sensitivity_level"] is None
    assert row["customer_facing_agent_name"] == "ghost"


# ---------- GET /workspaces/me/inbox/{id} ---------- #


def test_get_inbox_conversation_returns_thread(client, alice):
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", sensitivity_level="public")
    conv_id = _start_conversation(ws_id)
    _add_message(conv_id, "user", "first")
    _add_message(conv_id, "bot", "second")
    _add_message(conv_id, "system", "third")

    r = client.get(
        f"/workspaces/me/inbox/{conv_id}",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == conv_id
    assert body["customer_facing_agent_name"] == "vega"
    assert body["sensitivity_level"] == "public"
    assert [m["text"] for m in body["messages"]] == ["first", "second", "third"]
    assert [m["role"] for m in body["messages"]] == ["user", "bot", "system"]
    assert body["escalations"] == []


def test_get_inbox_conversation_surfaces_escalations(client, alice):
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    conv_id = _start_conversation(ws_id, status="escalated")
    _add_open_escalation(conv_id, reason="refund_request")

    r = client.get(
        f"/workspaces/me/inbox/{conv_id}",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    body = r.json()
    assert len(body["escalations"]) == 1
    assert body["escalations"][0]["reason"] == "refund_request"
    assert body["escalations"][0]["resolved_at"] is None


def test_get_inbox_conversation_404_unknown(client, alice):
    r = client.get(
        f"/workspaces/me/inbox/{uuid.uuid4()}",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 404


def test_get_inbox_conversation_tenant_isolation(client, alice):
    other_ws = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(id=other_ws, name="other-co", created_at=_now()))
    other_conv = _start_conversation(other_ws, agent_name="other-bot")

    r = client.get(
        f"/workspaces/me/inbox/{other_conv}",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 404


# ---------- POST .../take-over ---------- #


def test_take_over_flips_status_and_persists_system_message(client, alice):
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    conv_id = _start_conversation(ws_id, status="open")

    r = client.post(
        f"/workspaces/me/inbox/{conv_id}/take-over",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "operator_owned"

    with session_scope() as s:
        assert s.get(WidgetConversation, conv_id).status == "operator_owned"
        msgs = s.execute(
            select(WidgetMessage).where(
                WidgetMessage.conversation_id == conv_id
            )
        ).scalars().all()
        assert any(
            m.role == "system" and "operator has joined" in m.text
            for m in msgs
        )


def test_take_over_idempotent(client, alice):
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    conv_id = _start_conversation(ws_id, status="operator_owned")

    r = client.post(
        f"/workspaces/me/inbox/{conv_id}/take-over",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["noop"] is True

    # No duplicate system message.
    with session_scope() as s:
        msgs = s.execute(
            select(WidgetMessage).where(
                WidgetMessage.conversation_id == conv_id
            )
        ).scalars().all()
        assert len([m for m in msgs if "operator has joined" in m.text]) == 0


def test_take_over_409_on_resolved(client, alice):
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    conv_id = _start_conversation(ws_id, status="resolved")

    r = client.post(
        f"/workspaces/me/inbox/{conv_id}/take-over",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 409


# ---------- POST .../messages ---------- #


def test_operator_reply_persists_operator_message(client, alice):
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    conv_id = _start_conversation(ws_id, status="operator_owned")

    r = client.post(
        f"/workspaces/me/inbox/{conv_id}/messages",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"text": "Hi, I'm taking it from here."},
    )
    assert r.status_code == 200, r.text

    with session_scope() as s:
        msgs = s.execute(
            select(WidgetMessage).where(
                WidgetMessage.conversation_id == conv_id
            )
        ).scalars().all()
        assert any(m.role == "operator" for m in msgs)


def test_operator_reply_409_on_resolved(client, alice):
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    conv_id = _start_conversation(ws_id, status="resolved")

    r = client.post(
        f"/workspaces/me/inbox/{conv_id}/messages",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"text": "late reply"},
    )
    assert r.status_code == 409


def test_operator_reply_allowed_without_take_over(client, alice):
    """Operator can reply even on open / escalated conversations
    without taking over — surfaces as an operator-role message
    without pausing the bot."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    open_conv = _start_conversation(ws_id, status="open")
    escalated_conv = _start_conversation(ws_id, status="escalated")

    for conv_id in (open_conv, escalated_conv):
        r = client.post(
            f"/workspaces/me/inbox/{conv_id}/messages",
            headers=auth_headers(alice["api_key"]["plaintext"]),
            json={"text": "chiming in"},
        )
        assert r.status_code == 200, conv_id


# ---------- POST .../resolve ---------- #


def test_resolve_marks_conversation_and_escalations(client, alice):
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    conv_id = _start_conversation(ws_id, status="escalated")
    esc_id = _add_open_escalation(conv_id)

    r = client.post(
        f"/workspaces/me/inbox/{conv_id}/resolve",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "resolved"
    assert body["resolved_escalation_count"] == 1

    with session_scope() as s:
        conv = s.get(WidgetConversation, conv_id)
        assert conv.status == "resolved"
        assert conv.resolved_at is not None

        esc = s.get(WidgetEscalation, esc_id)
        assert esc.resolved_at is not None

        # System message dropped into thread.
        msgs = s.execute(
            select(WidgetMessage).where(
                WidgetMessage.conversation_id == conv_id
            )
        ).scalars().all()
        assert any(
            m.role == "system" and "resolved" in m.text.lower()
            for m in msgs
        )


def test_resolve_idempotent(client, alice):
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega")
    conv_id = _start_conversation(ws_id, status="resolved")

    r = client.post(
        f"/workspaces/me/inbox/{conv_id}/resolve",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200
    assert r.json()["noop"] is True


def test_resolve_tenant_isolation(client, alice):
    other_ws = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(id=other_ws, name="other-co", created_at=_now()))
    other_conv = _start_conversation(other_ws, agent_name="other-bot")

    r = client.post(
        f"/workspaces/me/inbox/{other_conv}/resolve",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 404
