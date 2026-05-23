"""Phase 21.6: tests for the widget chat orchestrator job handler.

Drives `run_widget_chat_job` directly with a session + payload —
doesn't go through the job runner thread. Asserts:

- Happy path enqueues a widget.chat Command for the bot.
- Conversation history is correctly trimmed + chronological-ordered.
- The new user message is plucked out as `user_message` (not
  duplicated in `conversation_history`).
- Missing bot → system message + status=failed.
- Bot without widget:respond capability → bot message + skipped.
- Conversation in operator_owned / resolved state → skipped.
- Unknown conversation → skipped.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from db import session_scope
from models import (
    Agent,
    Command,
    Workspace,
    WidgetConversation,
    WidgetMessage,
)
from widget_orchestrator import run_widget_chat_job


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _setup(
    *,
    workspace_id: str | None = None,
    agent_name: str | None = "vega",
    bot_caps: list[str] | None = None,
    conv_status: str = "open",
) -> tuple[str, str, str, int]:
    """Create a workspace, optional bot, conversation + one user
    message. Returns (workspace_id, conversation_id, agent_name_or_empty,
    user_message_id)."""
    if workspace_id is None:
        workspace_id = str(uuid.uuid4())
    if bot_caps is None:
        bot_caps = ["widget:respond"]

    with session_scope() as s:
        s.add(Workspace(
            id=workspace_id,
            name=f"orchestrator-ws-{workspace_id[:8]}",
            created_at=_now(),
        ))
        s.flush()
        if agent_name:
            s.add(Agent(
                workspace_id=workspace_id,
                name=agent_name,
                role="specialist",
                sensitivity_level="public",
                capabilities=bot_caps,
                command_handlers=[],
                created_at=_now(),
                updated_at=_now(),
            ))

    conv_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(WidgetConversation(
            id=conv_id,
            workspace_id=workspace_id,
            customer_facing_agent_name=agent_name or "missing-bot",
            status=conv_status,
            started_at=_now(),
            last_message_at=_now(),
        ))
        s.flush()
        s.add(WidgetMessage(
            conversation_id=conv_id,
            role="user",
            text="How do I cancel?",
            sent_at=_now(),
        ))
        s.flush()
        user_msg_id = s.execute(
            select(WidgetMessage.id).where(
                WidgetMessage.conversation_id == conv_id
            )
        ).scalar_one()

    return workspace_id, conv_id, agent_name or "", user_msg_id


# ---------- Happy path ---------- #


def test_orchestrator_enqueues_widget_chat_command():
    ws_id, conv_id, agent_name, user_msg_id = _setup()
    with session_scope() as s:
        result = run_widget_chat_job(s, ws_id, {
            "conversation_id": conv_id,
            "user_message_id": user_msg_id,
        })

    assert result["status"] == "dispatched"
    assert result["target_agent"] == "vega"
    assert result["conversation_id"] == conv_id
    assert result["command_id"]

    with session_scope() as s:
        cmd = s.get(Command, result["command_id"])
        assert cmd is not None
        assert cmd.workspace_id == ws_id
        assert cmd.agent_name == "vega"
        assert cmd.kind == "widget.chat"
        assert cmd.status == "pending"
        assert cmd.approval_state == "approved"
        assert cmd.payload["conversation_id"] == conv_id
        assert cmd.payload["user_message"] == "How do I cancel?"
        assert cmd.payload["conversation_history"] == []


def test_orchestrator_separates_user_message_from_history():
    """The current turn's user message should NOT appear in
    conversation_history — that's the role of `user_message`."""
    ws_id, conv_id, _, user_msg_id = _setup()

    # Add an earlier bot reply + earlier user message so history is
    # non-empty.
    with session_scope() as s:
        s.add(WidgetMessage(
            conversation_id=conv_id,
            role="bot",
            text="Sure, what's up?",
            sent_at=_now(),
        ))
        s.add(WidgetMessage(
            conversation_id=conv_id,
            role="user",
            text="Earlier question",
            sent_at=_now(),
        ))

    # Insert a NEW current user message that we'll target.
    with session_scope() as s:
        s.add(WidgetMessage(
            conversation_id=conv_id,
            role="user",
            text="Latest message",
            sent_at=_now(),
        ))
        s.flush()
        new_msg_id = s.execute(
            select(WidgetMessage.id)
            .where(WidgetMessage.conversation_id == conv_id)
            .order_by(WidgetMessage.id.desc())
            .limit(1)
        ).scalar_one()

    with session_scope() as s:
        result = run_widget_chat_job(s, ws_id, {
            "conversation_id": conv_id,
            "user_message_id": new_msg_id,
        })

    with session_scope() as s:
        cmd = s.get(Command, result["command_id"])
        assert cmd.payload["user_message"] == "Latest message"

        history = cmd.payload["conversation_history"]
        history_texts = [m["text"] for m in history]
        assert "Latest message" not in history_texts
        # Earlier messages preserved in chronological order.
        assert history_texts == [
            "How do I cancel?",
            "Sure, what's up?",
            "Earlier question",
        ]


def test_orchestrator_clamps_history_to_limit():
    """Conversations longer than WIDGET_HISTORY_LIMIT get the most
    recent N messages — older ones drop."""
    from widget_orchestrator import WIDGET_HISTORY_LIMIT

    ws_id, conv_id, _, _ = _setup()

    # Insert way more than the limit.
    with session_scope() as s:
        for i in range(WIDGET_HISTORY_LIMIT + 10):
            s.add(WidgetMessage(
                conversation_id=conv_id,
                role="bot" if i % 2 else "user",
                text=f"msg-{i}",
                sent_at=_now(),
            ))

    # New user message at the end.
    with session_scope() as s:
        s.add(WidgetMessage(
            conversation_id=conv_id,
            role="user",
            text="final",
            sent_at=_now(),
        ))
        s.flush()
        new_id = s.execute(
            select(WidgetMessage.id)
            .where(WidgetMessage.conversation_id == conv_id)
            .order_by(WidgetMessage.id.desc())
            .limit(1)
        ).scalar_one()

    with session_scope() as s:
        result = run_widget_chat_job(s, ws_id, {
            "conversation_id": conv_id,
            "user_message_id": new_id,
        })

    with session_scope() as s:
        cmd = s.get(Command, result["command_id"])
        # `user_message` + history together should be at most
        # WIDGET_HISTORY_LIMIT rows.
        assert len(cmd.payload["conversation_history"]) <= WIDGET_HISTORY_LIMIT


# ---------- Sad paths ---------- #


def test_orchestrator_missing_conversation():
    """Conversation deleted between enqueue and run → skipped."""
    with session_scope() as s:
        result = run_widget_chat_job(
            s, "ws_x",
            {"conversation_id": str(uuid.uuid4()), "user_message_id": 1},
        )
    assert result["status"] == "skipped"
    assert result["reason"] == "conversation_not_found"


def test_orchestrator_cross_workspace_conversation():
    """Conversation exists but belongs to another workspace —
    same skip path as missing."""
    ws_id, conv_id, _, user_msg_id = _setup()
    with session_scope() as s:
        result = run_widget_chat_job(s, "other-workspace", {
            "conversation_id": conv_id,
            "user_message_id": user_msg_id,
        })
    assert result["status"] == "skipped"
    assert result["reason"] == "conversation_not_found"


def test_orchestrator_missing_bot_persists_system_message():
    """Operator picked a bot then deleted the agent. The
    orchestrator drops a system message so the end user sees
    something + reports failed."""
    ws_id, conv_id, _, user_msg_id = _setup(agent_name=None)

    with session_scope() as s:
        result = run_widget_chat_job(s, ws_id, {
            "conversation_id": conv_id,
            "user_message_id": user_msg_id,
        })

    assert result["status"] == "failed"
    assert result["reason"] == "bot_not_found"

    with session_scope() as s:
        msgs = s.execute(
            select(WidgetMessage)
            .where(WidgetMessage.conversation_id == conv_id)
            .order_by(WidgetMessage.id)
        ).scalars().all()
        # First was the user's question; second is the system message.
        assert msgs[-1].role == "system"
        assert "No customer-facing bot is configured" in msgs[-1].text


def test_orchestrator_missing_capability_persists_bot_message():
    """Bot exists but lacks widget:respond → bot-role message
    explaining (vs. silent failure)."""
    ws_id, conv_id, _, user_msg_id = _setup(bot_caps=["internet"])

    with session_scope() as s:
        result = run_widget_chat_job(s, ws_id, {
            "conversation_id": conv_id,
            "user_message_id": user_msg_id,
        })

    assert result["status"] == "skipped"
    assert result["reason"] == "missing_widget_respond_capability"

    with session_scope() as s:
        msgs = s.execute(
            select(WidgetMessage)
            .where(WidgetMessage.conversation_id == conv_id)
            .order_by(WidgetMessage.id)
        ).scalars().all()
        assert msgs[-1].role == "bot"
        assert "isn't set up" in msgs[-1].text


def test_orchestrator_skips_resolved_conversation():
    ws_id, conv_id, _, user_msg_id = _setup(conv_status="resolved")
    with session_scope() as s:
        result = run_widget_chat_job(s, ws_id, {
            "conversation_id": conv_id,
            "user_message_id": user_msg_id,
        })
    assert result["status"] == "skipped"
    assert result["reason"] == "conversation_not_open"
    assert result["conversation_status"] == "resolved"


def test_orchestrator_skips_operator_owned_conversation():
    ws_id, conv_id, _, user_msg_id = _setup(conv_status="operator_owned")
    with session_scope() as s:
        result = run_widget_chat_job(s, ws_id, {
            "conversation_id": conv_id,
            "user_message_id": user_msg_id,
        })
    assert result["status"] == "skipped"
    assert result["reason"] == "conversation_not_open"


def test_orchestrator_handler_registered_in_jobs():
    """jobs._HANDLERS should have the widget_chat handler after
    importing widget_orchestrator (which the module does as a
    side effect on import)."""
    import jobs
    assert "widget_chat" in jobs._HANDLERS
