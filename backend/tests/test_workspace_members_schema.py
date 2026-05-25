"""Phase 23.1: tests for the workspace_members join + active session pointer.

Three surfaces:

1. `workspace_members` row roundtrip + composite PK + FK cascades on
   both sides (user delete + workspace delete).
2. `sessions.active_workspace_id` column: nullable FK with SET NULL
   on workspace delete so a session whose workspace was deleted from
   another tab doesn't 500 on the next request.
3. Validation helper `is_valid_workspace_member_role` + the frozenset
   it backs.

The migration-time backfill of existing data (every user → one member
row; every session → active_workspace_id set) is verified manually in
23.8 against a staging DB clone — pytest's per-test schema reset means
there are no pre-migration rows at test time.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from db import session_scope
from models import (
    Session as SessionRow,
    User,
    Workspace,
    WorkspaceMember,
    _VALID_WORKSPACE_MEMBER_ROLES,
    is_valid_workspace_member_role,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_workspace(s) -> str:
    ws_id = str(uuid.uuid4())
    s.add(Workspace(
        id=ws_id, name=f"ws-{ws_id[:8]}", created_at=_utcnow(),
    ))
    s.flush()
    return ws_id


def _make_user(s, workspace_id: str, *, email: str | None = None) -> str:
    uid = str(uuid.uuid4())
    s.add(User(
        id=uid,
        email=email or f"u-{uid[:8]}@example.com",
        password_hash="x",
        workspace_id=workspace_id,
        created_at=_utcnow(),
    ))
    s.flush()
    return uid


def _make_session(s, user_id: str, *, active_workspace_id: str | None = None) -> str:
    sid = str(uuid.uuid4())
    s.add(SessionRow(
        id=sid,
        user_id=user_id,
        token_hash=f"h-{sid[:8]}",
        created_at=_utcnow(),
        expires_at=_utcnow() + timedelta(days=30),
        active_workspace_id=active_workspace_id,
    ))
    s.flush()
    return sid


# ---------- workspace_members roundtrip ---------- #


def test_membership_roundtrip():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        uid = _make_user(s, ws_id)
        s.add(WorkspaceMember(
            user_id=uid, workspace_id=ws_id,
        ))

    with session_scope() as s:
        row = s.get(WorkspaceMember, (uid, ws_id))
        assert row is not None
        assert row.role == "owner"  # server_default
        assert row.joined_at is not None


def test_role_server_default_is_owner():
    """A membership row created without an explicit role lands as
    'owner' — matches the v1 'one user creates one workspace' shape."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        uid = _make_user(s, ws_id)
        # Bypass the SQLAlchemy default by raw SQL — the column's
        # server_default kicks in.
        s.execute(text(
            "INSERT INTO workspace_members (user_id, workspace_id) "
            "VALUES (:u, :w)"
        ), {"u": uid, "w": ws_id})

    with session_scope() as s:
        row = s.get(WorkspaceMember, (uid, ws_id))
        assert row.role == "owner"


def test_composite_pk_rejects_duplicate_membership():
    """A user can't be a member of the same workspace twice — the
    PK on (user_id, workspace_id) enforces it. Defensive against
    Phase 23B accept-flow double-clicks."""
    from db import SessionLocal

    with session_scope() as s:
        ws_id = _make_workspace(s)
        uid = _make_user(s, ws_id)
        s.add(WorkspaceMember(user_id=uid, workspace_id=ws_id))

    s2 = SessionLocal()
    try:
        s2.add(WorkspaceMember(user_id=uid, workspace_id=ws_id))
        with pytest.raises(IntegrityError):
            s2.commit()
    finally:
        s2.rollback()
        s2.close()


def test_same_user_can_belong_to_multiple_workspaces():
    """The whole point of Phase 23: one user, many workspaces."""
    with session_scope() as s:
        ws_a = _make_workspace(s)
        ws_b = _make_workspace(s)
        uid = _make_user(s, ws_a)
        s.add(WorkspaceMember(user_id=uid, workspace_id=ws_a))
        s.add(WorkspaceMember(user_id=uid, workspace_id=ws_b))

    with session_scope() as s:
        rows = s.execute(
            select(WorkspaceMember).where(WorkspaceMember.user_id == uid)
        ).scalars().all()
        assert {r.workspace_id for r in rows} == {ws_a, ws_b}


def test_same_workspace_can_have_multiple_members():
    """Schema is many-to-many already; Phase 23B's invite flow will
    insert the second row. Confirm the schema allows it today even
    though the v1 endpoints never do."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        u_a = _make_user(s, ws_id, email="a@example.com")
        u_b = _make_user(s, ws_id, email="b@example.com")
        s.add(WorkspaceMember(user_id=u_a, workspace_id=ws_id))
        s.add(WorkspaceMember(user_id=u_b, workspace_id=ws_id))

    with session_scope() as s:
        rows = s.execute(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == ws_id)
        ).scalars().all()
        assert {r.user_id for r in rows} == {u_a, u_b}


# ---------- FK cascade ---------- #


def test_member_cascade_on_user_delete():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        uid = _make_user(s, ws_id)
        s.add(WorkspaceMember(user_id=uid, workspace_id=ws_id))

    with session_scope() as s:
        s.delete(s.get(User, uid))

    with session_scope() as s:
        assert s.get(WorkspaceMember, (uid, ws_id)) is None


def test_member_cascade_on_workspace_delete():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        # Two members so we know the cascade hits the whole group.
        u_a = _make_user(s, ws_id, email="a@example.com")
        u_b = _make_user(s, ws_id, email="b@example.com")
        s.add(WorkspaceMember(user_id=u_a, workspace_id=ws_id))
        s.add(WorkspaceMember(user_id=u_b, workspace_id=ws_id))

    with session_scope() as s:
        s.delete(s.get(Workspace, ws_id))

    with session_scope() as s:
        rows = s.execute(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == ws_id)
        ).scalars().all()
        assert rows == []


# ---------- sessions.active_workspace_id ---------- #


def test_session_active_workspace_roundtrip():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        uid = _make_user(s, ws_id)
        sid = _make_session(s, uid, active_workspace_id=ws_id)

    with session_scope() as s:
        row = s.get(SessionRow, sid)
        assert row.active_workspace_id == ws_id


def test_session_active_workspace_default_is_null():
    """A session created without an explicit active workspace lands
    as NULL — the 23.6 picker page is the recovery surface for that
    state. Lets the migration backfill be non-blocking."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        uid = _make_user(s, ws_id)
        sid = _make_session(s, uid)  # no active_workspace_id

    with session_scope() as s:
        row = s.get(SessionRow, sid)
        assert row.active_workspace_id is None


def test_session_active_workspace_set_null_on_workspace_delete():
    """SET NULL (not CASCADE): deleting a workspace nulls the
    pointer on any session whose active_workspace_id was that
    workspace, while keeping the session row + user alive. The
    23.2 resolver then 401s + the dashboard sends the user through
    the workspace picker.

    Scenario: user lives in workspace A (their legacy/primary)
    but their session is currently viewing workspace B. Deleting B
    should null the session's active pointer without touching the
    user or the session row itself.
    """
    with session_scope() as s:
        ws_primary = _make_workspace(s)
        ws_secondary = _make_workspace(s)
        uid = _make_user(s, ws_primary)
        sid = _make_session(s, uid, active_workspace_id=ws_secondary)

    with session_scope() as s:
        s.delete(s.get(Workspace, ws_secondary))

    with session_scope() as s:
        row = s.get(SessionRow, sid)
        assert row is not None  # session survives
        assert row.active_workspace_id is None  # FK SET NULL fired
        # User wasn't touched either — they still own ws_primary.
        u = s.get(User, uid)
        assert u is not None
        assert u.workspace_id == ws_primary


# ---------- Index existence ---------- #


def test_workspace_members_per_workspace_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'workspace_members' "
            "AND indexname = 'ix_workspace_members_workspace'"
        )).first()
        assert r is not None


def test_sessions_active_workspace_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'sessions' "
            "AND indexname = 'ix_sessions_active_workspace'"
        )).first()
        assert r is not None


# ---------- Validation helper ---------- #


def test_role_validator_accepts_known_values():
    for r in ("owner", "member"):
        assert is_valid_workspace_member_role(r)


def test_role_validator_rejects_unknown():
    for bad in ("OWNER", "", "admin", "viewer", None, 1):
        assert not is_valid_workspace_member_role(bad)


def test_constant_set_has_expected_membership():
    assert _VALID_WORKSPACE_MEMBER_ROLES == {"owner", "member"}
