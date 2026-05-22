"""Phase 21.1: tests for the widget chat schema.

Three surfaces:

1. `widget_conversations` / `widget_messages` / `widget_escalations`
   roundtrip + FK cascades + index existence.
2. The three new columns on `workspaces`: `customer_facing_agent_name`,
   `widget_public_id` (unique), `allowed_widget_origins` (jsonb).
3. The status + role validation helpers
   (`is_valid_widget_conversation_status`,
   `is_valid_widget_message_role`).

Same shape as `test_connector_schema.py`. Endpoint + orchestrator
tests live in their own files starting from 21.2.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from db import session_scope
from models import (
    Workspace,
    WidgetConversation,
    WidgetEscalation,
    WidgetMessage,
    _VALID_WIDGET_CONVERSATION_STATUSES,
    _VALID_WIDGET_MESSAGE_ROLES,
    is_valid_widget_conversation_status,
    is_valid_widget_message_role,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_workspace(s, *, widget_public_id: str | None = None) -> str:
    ws_id = str(uuid.uuid4())
    s.add(Workspace(
        id=ws_id,
        name=f"widget-schema-{ws_id[:8]}",
        created_at=_utcnow(),
        widget_public_id=widget_public_id,
    ))
    s.flush()
    return ws_id


def _start_conversation(
    s,
    workspace_id: str,
    *,
    agent_name: str = "support-bot",
    anon_user_id: str | None = "anon-1",
) -> str:
    conv_id = str(uuid.uuid4())
    now = _utcnow()
    s.add(WidgetConversation(
        id=conv_id,
        workspace_id=workspace_id,
        customer_facing_agent_name=agent_name,
        anon_user_id=anon_user_id,
        started_at=now,
        last_message_at=now,
    ))
    s.flush()
    return conv_id


# ---------- Workspace new columns ---------- #


def test_widget_columns_default_to_empty_state():
    """A workspace created without setting any widget fields has
    null customer-facing bot + null public id + empty allowlist."""
    with session_scope() as s:
        ws_id = _make_workspace(s)

    with session_scope() as s:
        w = s.get(Workspace, ws_id)
        assert w.customer_facing_agent_name is None
        assert w.widget_public_id is None
        assert w.allowed_widget_origins == []


def test_widget_public_id_is_unique_across_workspaces():
    """Two workspaces can't share a widget_public_id — the public
    id is the only thing the customer-side snippet identifies us
    by, so a collision would route messages to the wrong inbox."""
    public_id = "wid_unique_42"
    with session_scope() as s:
        _make_workspace(s, widget_public_id=public_id)

    # Inline insert without session_scope() so the IntegrityError
    # fires on commit (not on the context manager exit). Same shape
    # as test_connector_schema.py's partial-unique tests.
    from db import SessionLocal
    s2 = SessionLocal()
    try:
        s2.add(Workspace(
            id=str(uuid.uuid4()),
            name="collision-co",
            created_at=_utcnow(),
            widget_public_id=public_id,  # same id, different workspace
        ))
        with pytest.raises(IntegrityError):
            s2.commit()
    finally:
        s2.rollback()
        s2.close()


def test_widget_public_id_null_does_not_collide():
    """Two workspaces with null widget_public_id coexist — only
    populated ids must be unique."""
    with session_scope() as s:
        _make_workspace(s)
        _make_workspace(s)  # second, also null public id


def test_allowed_widget_origins_roundtrips_jsonb_list():
    origins = ["https://app.halo.dev", "https://www.halo.dev"]
    with session_scope() as s:
        ws_id = _make_workspace(s)
        w = s.get(Workspace, ws_id)
        w.allowed_widget_origins = origins

    with session_scope() as s:
        w = s.get(Workspace, ws_id)
        assert w.allowed_widget_origins == origins


# ---------- widget_conversations roundtrip ---------- #


def test_conversation_roundtrip():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        conv_id = _start_conversation(s, ws_id, agent_name="vega")

    with session_scope() as s:
        row = s.get(WidgetConversation, conv_id)
        assert row is not None
        assert row.workspace_id == ws_id
        assert row.customer_facing_agent_name == "vega"
        assert row.status == "open"  # server_default
        assert row.anon_user_id == "anon-1"
        assert row.resolved_at is None


def test_conversation_fk_cascades_on_workspace_delete():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        conv_id = _start_conversation(s, ws_id)

    with session_scope() as s:
        s.delete(s.get(Workspace, ws_id))

    with session_scope() as s:
        assert s.get(WidgetConversation, conv_id) is None


# ---------- widget_messages roundtrip ---------- #


def test_message_roundtrip_and_thread_order():
    """Insert messages out of order, fetch in order, confirm
    sent_at-ordered ascending matches insertion order."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        conv_id = _start_conversation(s, ws_id)
        for i, (role, txt) in enumerate([
            ("user", "Hello?"),
            ("bot", "How can I help?"),
            ("user", "I have a billing question."),
            ("bot", "Let me check that for you."),
        ]):
            s.add(WidgetMessage(
                conversation_id=conv_id,
                role=role,
                text=txt,
                sent_at=datetime(
                    2026, 5, 22, 12, 0, i, tzinfo=timezone.utc,
                ),
            ))

    with session_scope() as s:
        rows = s.execute(
            select(WidgetMessage)
            .where(WidgetMessage.conversation_id == conv_id)
            .order_by(WidgetMessage.sent_at)
        ).scalars().all()
        assert len(rows) == 4
        assert [r.role for r in rows] == ["user", "bot", "user", "bot"]
        assert rows[0].text == "Hello?"


def test_message_fk_cascades_on_conversation_delete():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        conv_id = _start_conversation(s, ws_id)
        s.add(WidgetMessage(
            conversation_id=conv_id,
            role="user", text="x",
            sent_at=_utcnow(),
        ))
        s.flush()

    with session_scope() as s:
        s.delete(s.get(WidgetConversation, conv_id))

    with session_scope() as s:
        rows = s.execute(
            select(WidgetMessage).where(
                WidgetMessage.conversation_id == conv_id
            )
        ).scalars().all()
        assert rows == []


# ---------- widget_escalations roundtrip ---------- #


def test_escalation_roundtrip():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        conv_id = _start_conversation(s, ws_id)
        esc_id = str(uuid.uuid4())
        s.add(WidgetEscalation(
            id=esc_id,
            conversation_id=conv_id,
            reason="bot_escalate_call",
            payload={"last_user_message": "how do I cancel?"},
            escalated_at=_utcnow(),
        ))
        s.flush()

    with session_scope() as s:
        row = s.get(WidgetEscalation, esc_id)
        assert row is not None
        assert row.reason == "bot_escalate_call"
        assert row.payload == {"last_user_message": "how do I cancel?"}
        assert row.suggested_fix is None  # 21.9 fills this in later
        assert row.resolved_at is None


def test_escalation_fk_cascades_on_conversation_delete():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        conv_id = _start_conversation(s, ws_id)
        esc_id = str(uuid.uuid4())
        s.add(WidgetEscalation(
            id=esc_id,
            conversation_id=conv_id,
            reason="bot_crash",
            payload={},
            escalated_at=_utcnow(),
        ))
        s.flush()

    with session_scope() as s:
        s.delete(s.get(WidgetConversation, conv_id))

    with session_scope() as s:
        assert s.get(WidgetEscalation, esc_id) is None


def test_escalation_suggested_fix_jsonb_roundtrip():
    """The 21.9 incident-response extension writes a structured
    `suggested_fix` blob; confirm jsonb handles the shape."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        conv_id = _start_conversation(s, ws_id)
        esc_id = str(uuid.uuid4())
        s.add(WidgetEscalation(
            id=esc_id,
            conversation_id=conv_id,
            reason="bot_escalate_call",
            payload={},
            escalated_at=_utcnow(),
            suggested_fix={
                "kind": "system_prompt_addendum",
                "detail": "When users ask about refunds, link them to /refunds.",
            },
        ))

    with session_scope() as s:
        row = s.get(WidgetEscalation, esc_id)
        assert row.suggested_fix["kind"] == "system_prompt_addendum"
        assert "refunds" in row.suggested_fix["detail"]


# ---------- Tenant isolation ---------- #


def test_conversations_isolate_workspaces():
    """A workspace's conversations don't leak across tenants — the
    workspace_id FK + the inbox-list-index together scope every
    /inbox query."""
    with session_scope() as s:
        ws_a = _make_workspace(s)
        ws_b = _make_workspace(s)
        _start_conversation(s, ws_a, agent_name="vega")
        _start_conversation(s, ws_b, agent_name="orion")

    with session_scope() as s:
        a_rows = s.execute(
            select(WidgetConversation).where(
                WidgetConversation.workspace_id == ws_a
            )
        ).scalars().all()
        assert len(a_rows) == 1
        assert a_rows[0].customer_facing_agent_name == "vega"


# ---------- Index existence ---------- #


def test_inbox_list_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'widget_conversations' "
            "AND indexname = 'ix_widget_conversations_workspace_status_active'"
        )).first()
        assert r is not None


def test_anon_user_partial_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'widget_conversations' "
            "AND indexname = 'ix_widget_conversations_workspace_anon_user'"
        )).first()
        assert r is not None


def test_thread_render_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'widget_messages' "
            "AND indexname = 'ix_widget_messages_conversation_sent_at'"
        )).first()
        assert r is not None


def test_open_escalations_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'widget_escalations' "
            "AND indexname = 'ix_widget_escalations_open_recent'"
        )).first()
        assert r is not None


def test_workspace_widget_public_id_unique_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'workspaces' "
            "AND indexname = 'ix_workspaces_widget_public_id'"
        )).first()
        assert r is not None


# ---------- Validation helpers ---------- #


def test_status_validator_accepts_known_values():
    for s in ("open", "escalated", "operator_owned", "resolved"):
        assert is_valid_widget_conversation_status(s)


def test_status_validator_rejects_unknown():
    for bad in ("OPEN", "", "in_progress", None, 42):
        assert not is_valid_widget_conversation_status(bad)


def test_role_validator_accepts_known_values():
    for r in ("user", "bot", "operator", "system"):
        assert is_valid_widget_message_role(r)


def test_role_validator_rejects_unknown():
    for bad in ("USER", "", "agent", None, 0):
        assert not is_valid_widget_message_role(bad)


def test_constant_sets_have_expected_membership():
    """Belt + suspenders: if someone touches the frozenset, the test
    breaks immediately rather than silently letting a typo in."""
    assert _VALID_WIDGET_CONVERSATION_STATUSES == {
        "open", "escalated", "operator_owned", "resolved",
    }
    assert _VALID_WIDGET_MESSAGE_ROLES == {
        "user", "bot", "operator", "system",
    }
