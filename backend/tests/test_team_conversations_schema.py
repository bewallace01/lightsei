"""Phase 30.3.a: tests for team_conversations + team_messages.

Surfaces:

1. Roundtrip + server defaults (created_at, updated_at, status="completed",
   content=""). Confirms the alembic 0047 server_default()s land in
   Python without needing every insert to set them explicitly.
2. The three role kinds round-trip: "user", "router", "assistant".
   Router rows carry a JSONB routed_agents payload; assistant rows
   carry an agent_name.
3. FK CASCADE: deleting a workspace removes its team_conversations
   (and via the conversation→message FK, the messages too).
4. The pending-assistant partial index lands with the expected WHERE
   clause so the per-agent claim loop can use it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text

from db import session_scope
from models import (
    TeamConversation,
    TeamMessage,
    Workspace,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_workspace(s) -> str:
    ws_id = str(uuid.uuid4())
    s.add(Workspace(
        id=ws_id,
        name=f"team-conv-{ws_id[:8]}",
        created_at=_utcnow(),
    ))
    s.flush()
    return ws_id


def _make_conversation(s, ws_id: str, *, title: str = "general") -> str:
    cid = str(uuid.uuid4())
    s.add(TeamConversation(
        id=cid,
        workspace_id=ws_id,
        title=title,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    ))
    s.flush()
    return cid


# ---------- Roundtrip ---------- #


def test_team_conversation_roundtrip():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        cid = _make_conversation(s, ws_id, title="release-planning")

    with session_scope() as s:
        row = s.get(TeamConversation, cid)
        assert row is not None
        assert row.workspace_id == ws_id
        assert row.title == "release-planning"
        assert row.created_at is not None
        assert row.updated_at is not None


def test_user_message_uses_server_defaults():
    """status defaults to 'completed' + content defaults to '' so
    the dispatch path can insert a minimal row without restating
    them."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        cid = _make_conversation(s, ws_id)
        mid = str(uuid.uuid4())
        s.add(TeamMessage(
            id=mid,
            conversation_id=cid,
            role="user",
            content="ship it",
            created_at=_utcnow(),
            completed_at=_utcnow(),
        ))

    with session_scope() as s:
        m = s.get(TeamMessage, mid)
        assert m.role == "user"
        assert m.content == "ship it"
        assert m.status == "completed"
        assert m.agent_name is None
        assert m.routed_agents is None
        assert m.error is None


# ---------- All three role kinds ---------- #


def test_router_row_carries_routed_agents_jsonb():
    decision = {
        "agents": [
            {"name": "argus", "reason": "security scan request"},
            {"name": "hermes", "reason": "loops in argus's findings"},
        ],
    }
    with session_scope() as s:
        ws_id = _make_workspace(s)
        cid = _make_conversation(s, ws_id)
        mid = str(uuid.uuid4())
        s.add(TeamMessage(
            id=mid,
            conversation_id=cid,
            role="router",
            content="Routing to argus + hermes.",
            routed_agents=decision,
            created_at=_utcnow(),
            completed_at=_utcnow(),
        ))

    with session_scope() as s:
        m = s.get(TeamMessage, mid)
        assert m.role == "router"
        assert m.routed_agents == decision
        assert m.agent_name is None


def test_assistant_row_carries_agent_name_and_is_pending_by_default_path():
    """The dispatch path inserts assistant rows with explicit
    status='pending'. Confirm the column accepts that + that
    agent_name is required-by-convention (not nullable enforced)
    for attribution to work."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        cid = _make_conversation(s, ws_id)
        mid = str(uuid.uuid4())
        s.add(TeamMessage(
            id=mid,
            conversation_id=cid,
            role="assistant",
            status="pending",
            agent_name="argus",
            created_at=_utcnow(),
        ))

    with session_scope() as s:
        m = s.get(TeamMessage, mid)
        assert m.role == "assistant"
        assert m.agent_name == "argus"
        assert m.status == "pending"
        assert m.content == ""
        assert m.completed_at is None


# ---------- FK cascade ---------- #


def test_workspace_delete_cascades_to_conversations_and_messages():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        cid = _make_conversation(s, ws_id)
        for role, agent in (
            ("user", None),
            ("router", None),
            ("assistant", "argus"),
            ("assistant", "hermes"),
        ):
            s.add(TeamMessage(
                id=str(uuid.uuid4()),
                conversation_id=cid,
                role=role,
                agent_name=agent,
                status="pending" if role == "assistant" else "completed",
                created_at=_utcnow(),
            ))

    with session_scope() as s:
        s.delete(s.get(Workspace, ws_id))

    with session_scope() as s:
        convs = s.execute(
            select(TeamConversation)
            .where(TeamConversation.workspace_id == ws_id)
        ).scalars().all()
        assert convs == []
        msgs = s.execute(
            select(TeamMessage).where(TeamMessage.conversation_id == cid)
        ).scalars().all()
        assert msgs == []


# ---------- Index landings ---------- #


def test_conv_created_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'team_messages' "
            "AND indexname = 'ix_team_messages_conv_created'"
        )).first()
        assert r is not None


def test_pending_assistant_partial_index_landed():
    """The per-agent claim loop scans for the oldest pending assistant
    row for a given agent_name. The partial index filters status +
    role so the scan is small even on a busy workspace."""
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'team_messages' "
            "AND indexname = 'ix_team_messages_pending_assistant'"
        )).first()
        assert r is not None
        idx = r[0].lower()
        # Postgres serializes the partial WHERE with explicit ::text
        # casts; assert on the literals only so the test isn't
        # coupled to the exact cast spelling.
        assert "'pending'" in idx
        assert "'assistant'" in idx
