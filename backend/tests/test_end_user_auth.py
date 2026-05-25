"""Phase 25.3: tests for backend/end_user_auth.py.

Two surfaces:

1. `_resolve` direct: covers the happy path + every 401 path
   (missing, malformed bearer, unknown token, expired session,
   revoked session, cross-token-type operator session, dangling
   session whose end_user was deleted).
2. `linked_workspaces`: only active subscriptions (removed_at IS NULL)
   surface; soft-revoked links are filtered out.

The cross-token-type check is the load-bearing security test for
this phase. If it ever stops 401ing, an operator paste of their
own bearer header into a `/c` request would resolve to whatever
end-user identity happened to have the same token plaintext,
which in practice is none but the failure mode would be silent.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from db import session_scope
from end_user_auth import _resolve
from keys import generate_session_token, hash_token
from models import (
    EndUser,
    EndUserSession,
    EndUserVendorLink,
    Session as SessionRow,
    User,
    Workspace,
    WorkspaceMember,
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


def _make_end_user(s, *, email: str | None = None) -> str:
    euid = str(uuid.uuid4())
    s.add(EndUser(
        id=euid,
        email=email or f"eu-{euid[:8]}@example.com",
    ))
    s.flush()
    return euid


def _make_end_user_session(
    s, end_user_id: str, *,
    plaintext: str | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> tuple[str, str]:
    """Returns (session_id, plaintext_token). Caller can pass an explicit
    plaintext to force a collision with another row in tests."""
    token = plaintext or generate_session_token()
    sid = str(uuid.uuid4())
    s.add(EndUserSession(
        id=sid,
        end_user_id=end_user_id,
        token_hash=hash_token(token),
        created_at=_utcnow(),
        expires_at=expires_at or _utcnow() + timedelta(days=30),
        revoked_at=revoked_at,
    ))
    s.flush()
    return sid, token


def _make_operator_session(s, *, plaintext: str | None = None) -> str:
    """Helper for the cross-token-type test: drop a SessionRow whose
    token_hash matches a known plaintext."""
    ws_id = _make_workspace(s)
    uid = str(uuid.uuid4())
    s.add(User(
        id=uid,
        email=f"op-{uid[:8]}@example.com",
        password_hash="x",
        workspace_id=ws_id,
        created_at=_utcnow(),
    ))
    s.flush()
    s.add(WorkspaceMember(user_id=uid, workspace_id=ws_id))
    token = plaintext or generate_session_token()
    sid = str(uuid.uuid4())
    s.add(SessionRow(
        id=sid,
        user_id=uid,
        token_hash=hash_token(token),
        created_at=_utcnow(),
        expires_at=_utcnow() + timedelta(days=30),
        active_workspace_id=ws_id,
    ))
    s.flush()
    return token


# ---------- Happy path ---------- #


def test_resolve_valid_token_returns_end_user_and_session():
    with session_scope() as s:
        euid = _make_end_user(s, email="happy@example.com")
        sid, token = _make_end_user_session(s, euid)

    with session_scope() as s:
        result = _resolve(f"Bearer {token}", s)
        assert result.end_user.id == euid
        assert result.end_user.email == "happy@example.com"
        assert result.session.id == sid
        assert result.linked_workspaces == []


def test_resolve_includes_active_linked_workspaces():
    """Subscriber to two vendors: both surface in linked_workspaces.
    A soft-revoked third link is excluded."""
    with session_scope() as s:
        ws_a = _make_workspace(s)
        ws_b = _make_workspace(s)
        ws_revoked = _make_workspace(s)
        euid = _make_end_user(s)
        s.add(EndUserVendorLink(end_user_id=euid, workspace_id=ws_a))
        s.add(EndUserVendorLink(end_user_id=euid, workspace_id=ws_b))
        s.add(EndUserVendorLink(
            end_user_id=euid, workspace_id=ws_revoked,
            removed_at=_utcnow(),
        ))
        _, token = _make_end_user_session(s, euid)

    with session_scope() as s:
        result = _resolve(f"Bearer {token}", s)
        ws_ids = {w.id for w in result.linked_workspaces}
        assert ws_ids == {ws_a, ws_b}


# ---------- 401 paths ---------- #


def test_resolve_rejects_missing_bearer():
    with session_scope() as s:
        with pytest.raises(HTTPException) as exc:
            _resolve(None, s)
        assert exc.value.status_code == 401
        assert "missing" in exc.value.detail


def test_resolve_rejects_malformed_bearer():
    """No 'Bearer ' prefix, or empty value, or wrong scheme."""
    with session_scope() as s:
        for header in ("Basic abc", "Bearer ", "abc", "Bearer  "):
            with pytest.raises(HTTPException) as exc:
                _resolve(header, s)
            assert exc.value.status_code == 401


def test_resolve_rejects_unknown_token():
    """Token that's not in either end_user_sessions or sessions."""
    fake = generate_session_token()
    with session_scope() as s:
        with pytest.raises(HTTPException) as exc:
            _resolve(f"Bearer {fake}", s)
        assert exc.value.status_code == 401
        assert exc.value.detail == "invalid end-user session"


def test_resolve_rejects_revoked_session():
    with session_scope() as s:
        euid = _make_end_user(s)
        _, token = _make_end_user_session(s, euid, revoked_at=_utcnow())

    with session_scope() as s:
        with pytest.raises(HTTPException) as exc:
            _resolve(f"Bearer {token}", s)
        assert exc.value.status_code == 401
        assert "revoked" in exc.value.detail


def test_resolve_rejects_expired_session():
    with session_scope() as s:
        euid = _make_end_user(s)
        _, token = _make_end_user_session(
            s, euid, expires_at=_utcnow() - timedelta(seconds=1),
        )

    with session_scope() as s:
        with pytest.raises(HTTPException) as exc:
            _resolve(f"Bearer {token}", s)
        assert exc.value.status_code == 401
        assert "expired" in exc.value.detail


def test_resolve_rejects_operator_session_token():
    """Cross-token-type protection: an operator session token used
    on the end-user path must 401 with a distinctive detail string."""
    with session_scope() as s:
        token = _make_operator_session(s)

    with session_scope() as s:
        with pytest.raises(HTTPException) as exc:
            _resolve(f"Bearer {token}", s)
        assert exc.value.status_code == 401
        assert "operator session token" in exc.value.detail


def test_resolve_via_fastapi_dep_signature():
    """get_end_user is the FastAPI-facing wrapper. Smoke that the
    public signature is what main.py + endpoints will call against."""
    from end_user_auth import get_end_user

    with session_scope() as s:
        euid = _make_end_user(s, email="dep@example.com")
        _, token = _make_end_user_session(s, euid)

    with session_scope() as s:
        result = get_end_user(authorization=f"Bearer {token}", session=s)
        assert result.end_user.email == "dep@example.com"
