"""Phase 19.1: tests for the Slack chat-surface schema backbone.

Three tables under test:

- `slack_workspaces`: FK to workspaces, encrypted bot token round-trips,
  the partial-unique index enforces one Lightsei workspace per Slack
  workspace among non-revoked installs.
- `slack_channels`: composite PK, defaults (sensitivity_level=internal,
  opted_in=false), FK cascade from slack_workspaces.
- `slack_events`: idempotency duplicate inserts violate the PK + don't
  drop earlier rows.

Actual Slack flows (OAuth, webhook, orchestrator) live in 19.2-19.4
and get their own test files.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from db import session_scope
from models import (
    SlackChannel,
    SlackEvent,
    SlackWorkspace,
    Workspace,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_workspace(s) -> str:
    """Insert a Lightsei workspace + return its id. Slack rows FK to
    workspaces.id so every test needs at least one."""
    ws_id = str(uuid.uuid4())
    s.add(
        Workspace(
            id=ws_id,
            name=f"slack-schema-test-{ws_id[:8]}",
            created_at=_utcnow(),
        )
    )
    s.flush()
    return ws_id


# ---------- slack_workspaces ---------- #


def test_slack_workspace_roundtrip():
    """Insert + read back the encrypted bot token (no decryption here —
    we're just testing that LargeBinary stores arbitrary bytes)."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        team_id = f"T{uuid.uuid4().hex[:10].upper()}"
        token_bytes = b"\x00\x01\x02fake-encrypted-token\xff\xfe"
        s.add(
            SlackWorkspace(
                slack_team_id=team_id,
                lightsei_workspace_id=ws_id,
                team_name="Coral",
                bot_token_encrypted=token_bytes,
                bot_user_id="U123BOT",
                installed_by_user_id=None,
                installed_at=_utcnow(),
            )
        )

    with session_scope() as s:
        row = s.get(SlackWorkspace, team_id)
        assert row is not None
        assert row.team_name == "Coral"
        assert row.bot_token_encrypted == token_bytes
        assert row.revoked_at is None


def test_slack_workspace_fk_cascades_on_workspace_delete():
    """When a Lightsei workspace is deleted, the slack_workspaces row
    goes with it (ondelete=CASCADE)."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        team_id = f"T{uuid.uuid4().hex[:10].upper()}"
        s.add(
            SlackWorkspace(
                slack_team_id=team_id,
                lightsei_workspace_id=ws_id,
                team_name="ToDelete",
                bot_token_encrypted=b"x",
                bot_user_id="U1",
                installed_at=_utcnow(),
            )
        )

    with session_scope() as s:
        ws = s.get(Workspace, ws_id)
        s.delete(ws)

    with session_scope() as s:
        assert s.get(SlackWorkspace, team_id) is None


def test_partial_unique_index_one_active_install_per_workspace():
    """A Lightsei workspace can only have one non-revoked Slack
    install at a time. A second active install with the same
    lightsei_workspace_id violates the partial-unique index."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        s.add(
            SlackWorkspace(
                slack_team_id="T_FIRST",
                lightsei_workspace_id=ws_id,
                team_name="First",
                bot_token_encrypted=b"x",
                bot_user_id="U1",
                installed_at=_utcnow(),
            )
        )

    s2 = None
    try:
        from db import SessionLocal
        s2 = SessionLocal()
        s2.add(
            SlackWorkspace(
                slack_team_id="T_SECOND",
                lightsei_workspace_id=ws_id,  # same Lightsei workspace
                team_name="Second",
                bot_token_encrypted=b"x",
                bot_user_id="U2",
                installed_at=_utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            s2.commit()
    finally:
        if s2 is not None:
            s2.rollback()
            s2.close()


def test_partial_unique_index_allows_revoked_reinstall():
    """Once an install is revoked, a fresh install of the same
    Lightsei workspace works. The partial-unique index excludes
    revoked rows so the second insert doesn't violate it."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        s.add(
            SlackWorkspace(
                slack_team_id="T_OLD",
                lightsei_workspace_id=ws_id,
                team_name="Old",
                bot_token_encrypted=b"x",
                bot_user_id="U1",
                installed_at=_utcnow(),
                revoked_at=_utcnow(),  # already revoked
            )
        )

    # New install with same Lightsei workspace should succeed.
    with session_scope() as s:
        s.add(
            SlackWorkspace(
                slack_team_id="T_NEW",
                lightsei_workspace_id=ws_id,
                team_name="New",
                bot_token_encrypted=b"x",
                bot_user_id="U2",
                installed_at=_utcnow(),
            )
        )

    with session_scope() as s:
        rows = s.execute(
            select(SlackWorkspace).where(
                SlackWorkspace.lightsei_workspace_id == ws_id
            )
        ).scalars().all()
        assert len(rows) == 2  # both visible; the partial index only restricted writes


# ---------- slack_channels ---------- #


def test_slack_channel_defaults_internal_zone_and_silent():
    """Fresh channel rows default to sensitivity_level='internal' +
    opted_in=false — the silent-by-default wedge."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        team_id = "T_CH_DEFAULT"
        s.add(
            SlackWorkspace(
                slack_team_id=team_id,
                lightsei_workspace_id=ws_id,
                team_name="X",
                bot_token_encrypted=b"x",
                bot_user_id="U1",
                installed_at=_utcnow(),
            )
        )
        s.flush()
        s.add(
            SlackChannel(
                slack_team_id=team_id,
                channel_id="C123",
                lightsei_workspace_id=ws_id,
                channel_name="data",
                # NOT setting sensitivity_level or opted_in — defaults
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )

    with session_scope() as s:
        row = s.get(SlackChannel, (team_id, "C123"))
        assert row is not None
        assert row.sensitivity_level == "internal"
        assert row.opted_in is False


def test_slack_channel_composite_pk_unique():
    """(slack_team_id, channel_id) is the PK — same pair twice fails."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        team_id = "T_DUPE"
        s.add(
            SlackWorkspace(
                slack_team_id=team_id,
                lightsei_workspace_id=ws_id,
                team_name="X",
                bot_token_encrypted=b"x",
                bot_user_id="U1",
                installed_at=_utcnow(),
            )
        )
        s.flush()
        s.add(
            SlackChannel(
                slack_team_id=team_id,
                channel_id="C_DUPE",
                lightsei_workspace_id=ws_id,
                channel_name="first",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )

    from db import SessionLocal
    s2 = SessionLocal()
    try:
        s2.add(
            SlackChannel(
                slack_team_id=team_id,
                channel_id="C_DUPE",  # collision
                lightsei_workspace_id=ws_id,
                channel_name="second",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            s2.commit()
    finally:
        s2.rollback()
        s2.close()


def test_slack_channel_cascade_on_slack_workspace_delete():
    """When a slack_workspaces row goes (e.g. workspace teardown
    cascade), its channels go with it."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        team_id = "T_CASC"
        s.add(
            SlackWorkspace(
                slack_team_id=team_id,
                lightsei_workspace_id=ws_id,
                team_name="X",
                bot_token_encrypted=b"x",
                bot_user_id="U1",
                installed_at=_utcnow(),
            )
        )
        s.flush()
        s.add(
            SlackChannel(
                slack_team_id=team_id,
                channel_id="C_CASC",
                lightsei_workspace_id=ws_id,
                channel_name="x",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )

    with session_scope() as s:
        sw = s.get(SlackWorkspace, team_id)
        s.delete(sw)

    with session_scope() as s:
        assert s.get(SlackChannel, (team_id, "C_CASC")) is None


# ---------- slack_events ---------- #


def test_slack_event_idempotency_duplicate_event_id_rejected():
    """The (slack_team_id, event_id) PK enforces idempotency. Inserting
    the same event_id twice for the same team fails — the webhook
    handler relies on this to short-circuit duplicate deliveries."""
    with session_scope() as s:
        s.add(
            SlackEvent(
                slack_team_id="T_EVT",
                event_id="Ev0001",
                kind="app_mention",
                received_at=_utcnow(),
            )
        )

    from db import SessionLocal
    s2 = SessionLocal()
    try:
        s2.add(
            SlackEvent(
                slack_team_id="T_EVT",
                event_id="Ev0001",  # duplicate
                kind="app_mention",
                received_at=_utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            s2.commit()
    finally:
        s2.rollback()
        s2.close()


def test_slack_event_same_event_id_different_team_allowed():
    """Slack event_ids are unique within a team but the (team, event_id)
    pair gives us cross-tenant safety. Two different Slack workspaces
    can carry the same Slack-side event_id without collision."""
    with session_scope() as s:
        s.add(
            SlackEvent(
                slack_team_id="T_AAA",
                event_id="Ev_SHARED",
                kind="app_mention",
                received_at=_utcnow(),
            )
        )
        s.add(
            SlackEvent(
                slack_team_id="T_BBB",
                event_id="Ev_SHARED",  # same event_id, different team
                kind="app_mention",
                received_at=_utcnow(),
            )
        )

    with session_scope() as s:
        rows = s.execute(
            select(SlackEvent).where(SlackEvent.event_id == "Ev_SHARED")
        ).scalars().all()
        assert len(rows) == 2


def test_received_at_index_exists():
    """ix_slack_events_received_at is what the cleanup cron's
    "delete rows older than 7d" query will use. Check it landed."""
    with session_scope() as s:
        r = s.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'slack_events' "
                "AND indexname = 'ix_slack_events_received_at'"
            )
        ).first()
        assert r is not None


def test_workspace_sensitivity_index_exists_on_slack_channels():
    """ix_slack_channels_workspace_sensitivity is what the chat
    orchestrator's "which channels are opted in for zone X?" query
    will use. Check it landed."""
    with session_scope() as s:
        r = s.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'slack_channels' "
                "AND indexname = 'ix_slack_channels_workspace_sensitivity'"
            )
        ).first()
        assert r is not None
