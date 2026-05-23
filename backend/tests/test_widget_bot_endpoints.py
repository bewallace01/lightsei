"""Phase 21.5: tests for the bot-side widget endpoints.

POST /widget-bot/respond — bot posts a reply.
POST /widget-bot/escalate — bot flips conversation to escalated.

Both API-key authed; capability-gated (`widget:respond` /
`widget:escalate`); conversation-scoped to the calling workspace.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from capabilities import KNOWN_CAPABILITIES
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
    capabilities: list[str],
    sensitivity_level: str = "public",
) -> None:
    with session_scope() as s:
        s.add(Agent(
            workspace_id=workspace_id,
            name=name,
            role="specialist",
            sensitivity_level=sensitivity_level,
            capabilities=capabilities,
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


# ---------- Capability constant ---------- #


def test_widget_respond_is_a_known_capability():
    assert "widget:respond" in KNOWN_CAPABILITIES
    assert "widget:escalate" in KNOWN_CAPABILITIES


# ---------- /widget-bot/respond ---------- #


def test_respond_happy_path(client, alice):
    """Bot with widget:respond posts a reply; message lands in the
    conversation + last_message_at bumps."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["widget:respond"])
    conv_id = _start_conversation(ws_id)

    r = client.post(
        "/widget-bot/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "vega",
            "conversation_id": conv_id,
            "text": "I can help with that.",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["conversation_id"] == conv_id

    with session_scope() as s:
        msgs = s.execute(
            select(WidgetMessage).where(WidgetMessage.conversation_id == conv_id)
        ).scalars().all()
        assert len(msgs) == 1
        assert msgs[0].role == "bot"
        assert msgs[0].text == "I can help with that."


def test_respond_403_when_capability_missing(client, alice):
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "atlas", capabilities=["internet"])  # no widget:respond
    conv_id = _start_conversation(ws_id)

    r = client.post(
        "/widget-bot/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "atlas",
            "conversation_id": conv_id,
            "text": "x",
        },
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "capability_missing"
    assert detail["capability"] == "widget:respond"


def test_respond_404_when_agent_unknown(client, alice):
    """source_agent doesn't exist → 404 (distinct from 403 so SDK
    code can map 'bot not in workspace' vs 'missing capability')."""
    conv_id = _start_conversation(alice["workspace"]["id"])
    r = client.post(
        "/widget-bot/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "ghost-bot",
            "conversation_id": conv_id,
            "text": "x",
        },
    )
    assert r.status_code == 404


def test_respond_404_when_conversation_unknown(client, alice):
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["widget:respond"])
    r = client.post(
        "/widget-bot/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "vega",
            "conversation_id": str(uuid.uuid4()),
            "text": "x",
        },
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "conversation_not_found"


def test_respond_404_cross_workspace_conversation(client, alice):
    """A bot in workspace A can't post into workspace B's
    conversation — tenant isolation."""
    other_ws_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(
            id=other_ws_id, name="other-co",
            created_at=_now(),
        ))
    other_conv = _start_conversation(other_ws_id, agent_name="other-bot")
    _add_agent(
        alice["workspace"]["id"], "vega",
        capabilities=["widget:respond"],
    )

    r = client.post(
        "/widget-bot/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "vega",
            "conversation_id": other_conv,
            "text": "x",
        },
    )
    assert r.status_code == 404


def test_respond_409_when_conversation_resolved(client, alice):
    """If the operator marked the conversation resolved, the bot
    reply is refused — don't let a stale bot reply land after a
    human closed the thread."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["widget:respond"])
    conv_id = _start_conversation(ws_id, status="resolved")

    r = client.post(
        "/widget-bot/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "vega",
            "conversation_id": conv_id,
            "text": "late reply",
        },
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "conversation_resolved"


def test_respond_works_for_operator_owned(client, alice):
    """operator_owned only pauses the orchestrator job (in 21.2);
    a bot CAN still post if explicitly called. Edge case; the
    happy path is the orchestrator skips the call entirely."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["widget:respond"])
    conv_id = _start_conversation(ws_id, status="operator_owned")

    r = client.post(
        "/widget-bot/respond",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "vega",
            "conversation_id": conv_id,
            "text": "bot still works",
        },
    )
    assert r.status_code == 200


# ---------- /widget-bot/escalate ---------- #


def test_escalate_happy_path(client, alice):
    """Bot with widget:escalate flips conversation to escalated +
    creates escalation row + drops a system message."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["widget:escalate"])
    conv_id = _start_conversation(ws_id)

    r = client.post(
        "/widget-bot/escalate",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "vega",
            "conversation_id": conv_id,
            "reason": "refund_request",
            "payload": {"last_user_message": "can I get a refund?"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "escalated"
    assert body["escalation_id"]

    with session_scope() as s:
        conv = s.get(WidgetConversation, conv_id)
        assert conv.status == "escalated"

        escs = s.execute(
            select(WidgetEscalation).where(
                WidgetEscalation.conversation_id == conv_id
            )
        ).scalars().all()
        assert len(escs) == 1
        assert escs[0].reason == "refund_request"
        assert escs[0].payload == {"last_user_message": "can I get a refund?"}
        assert escs[0].resolved_at is None

        # System message in the thread surfaces the handoff to the
        # end user.
        msgs = s.execute(
            select(WidgetMessage).where(WidgetMessage.conversation_id == conv_id)
        ).scalars().all()
        assert any(m.role == "system" for m in msgs)


def test_escalate_403_when_capability_missing(client, alice):
    """widget:respond alone is not enough — escalate is a separate
    capability so an operator can opt-in to reply without granting
    the escalate path."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["widget:respond"])  # no widget:escalate
    conv_id = _start_conversation(ws_id)

    r = client.post(
        "/widget-bot/escalate",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "vega",
            "conversation_id": conv_id,
            "reason": "boom",
        },
    )
    assert r.status_code == 403
    assert r.json()["detail"]["capability"] == "widget:escalate"


def test_escalate_idempotent_on_already_escalated(client, alice):
    """Already-escalated conversation → noop without creating a
    duplicate escalation row."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["widget:escalate"])
    conv_id = _start_conversation(ws_id, status="escalated")

    r = client.post(
        "/widget-bot/escalate",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "vega",
            "conversation_id": conv_id,
            "reason": "second_call",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["noop"] is True

    with session_scope() as s:
        escs = s.execute(
            select(WidgetEscalation).where(
                WidgetEscalation.conversation_id == conv_id
            )
        ).scalars().all()
        # No new row created.
        assert len(escs) == 0


def test_escalate_idempotent_on_operator_owned(client, alice):
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["widget:escalate"])
    conv_id = _start_conversation(ws_id, status="operator_owned")

    r = client.post(
        "/widget-bot/escalate",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "vega",
            "conversation_id": conv_id,
            "reason": "boom",
        },
    )
    assert r.status_code == 200
    assert r.json()["noop"] is True


def test_escalate_idempotent_on_resolved(client, alice):
    """A resolved conversation can't be re-escalated — the operator
    wrapped it up. No-op."""
    ws_id = alice["workspace"]["id"]
    _add_agent(ws_id, "vega", capabilities=["widget:escalate"])
    conv_id = _start_conversation(ws_id, status="resolved")

    r = client.post(
        "/widget-bot/escalate",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "vega",
            "conversation_id": conv_id,
            "reason": "boom",
        },
    )
    assert r.status_code == 200
    assert r.json()["noop"] is True


def test_escalate_404_cross_workspace_conversation(client, alice):
    other_ws_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(
            id=other_ws_id, name="other-co",
            created_at=_now(),
        ))
    other_conv = _start_conversation(other_ws_id, agent_name="other-bot")
    _add_agent(
        alice["workspace"]["id"], "vega",
        capabilities=["widget:escalate"],
    )

    r = client.post(
        "/widget-bot/escalate",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={
            "source_agent": "vega",
            "conversation_id": other_conv,
            "reason": "x",
        },
    )
    assert r.status_code == 404
