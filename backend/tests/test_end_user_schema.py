"""Phase 25.1: tests for the end-user identity tables.

Four surfaces:

1. `end_users`: roundtrip, email uniqueness, server defaults for
   `email_verified` + `auth_provider` + `created_at` + `updated_at`.
2. `end_user_sessions`: roundtrip, token_hash uniqueness, FK cascade
   on end_user delete.
3. `end_user_vendor_links`: composite-PK roundtrip, FK cascade on
   end_user + workspace delete, server default for `linked_via`.
4. `end_user_signin_tokens`: roundtrip, vendor_invite_code carry-
   through, optional consumed_at.

Plus:

- `widget_conversations.end_user_id` column: nullable FK with SET
   NULL on end_user delete (the conversation row survives so the
   vendor's audit trail isn't gapped if the end-user account is
   removed).
- Index landings for ix_end_user_sessions_end_user,
   ix_end_user_vendor_links_workspace,
   ix_end_user_signin_tokens_email_created,
   ix_widget_conversations_workspace_end_user.
- Validation helpers `is_valid_end_user_auth_provider` +
   `is_valid_end_user_vendor_link_via`.

Same shape as `test_workspace_members_schema.py`. Endpoint tests
(auth flow, session resolver, widget extensions) live in their own
files starting from 25.2.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from db import session_scope
from models import (
    DEFAULT_END_USER_AUTH_PROVIDER,
    DEFAULT_END_USER_VENDOR_LINK_VIA,
    EndUser,
    EndUserSession,
    EndUserSigninToken,
    EndUserVendorLink,
    WidgetConversation,
    Workspace,
    _VALID_END_USER_AUTH_PROVIDERS,
    _VALID_END_USER_VENDOR_LINK_VIA,
    is_valid_end_user_auth_provider,
    is_valid_end_user_vendor_link_via,
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


def _make_end_user_session(s, end_user_id: str, *, token: str | None = None) -> str:
    sid = str(uuid.uuid4())
    s.add(EndUserSession(
        id=sid,
        end_user_id=end_user_id,
        token_hash=token or f"h-{sid[:8]}",
        created_at=_utcnow(),
        expires_at=_utcnow() + timedelta(days=30),
    ))
    s.flush()
    return sid


# ---------- end_users roundtrip ---------- #


def test_end_user_roundtrip_with_server_defaults():
    """An end_user inserted with only the required columns picks up
    server defaults for the rest. Lets the signup path stay terse."""
    with session_scope() as s:
        euid = str(uuid.uuid4())
        s.execute(text(
            "INSERT INTO end_users (id, email) VALUES (:i, :e)"
        ), {"i": euid, "e": f"defaults-{euid[:8]}@example.com"})

    with session_scope() as s:
        eu = s.get(EndUser, euid)
        assert eu is not None
        assert eu.email_verified is False  # server_default false
        assert eu.auth_provider == "magic_link"  # server_default
        assert eu.display_name is None
        assert eu.created_at is not None  # server_default now()
        assert eu.updated_at is not None


def test_end_user_email_is_unique():
    """A second end_user can't be inserted with the same email,
    even after the first one's row exists. Protects the find-or-
    create path on the magic-link consume side."""
    email = "dup@example.com"
    with session_scope() as s:
        _make_end_user(s, email=email)

    from db import SessionLocal
    s2 = SessionLocal()
    try:
        s2.add(EndUser(
            id=str(uuid.uuid4()),
            email=email,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        ))
        with pytest.raises(IntegrityError):
            s2.commit()
    finally:
        s2.rollback()
        s2.close()


# ---------- end_user_sessions roundtrip + cascade ---------- #


def test_end_user_session_roundtrip():
    with session_scope() as s:
        euid = _make_end_user(s)
        sid = _make_end_user_session(s, euid)

    with session_scope() as s:
        row = s.get(EndUserSession, sid)
        assert row is not None
        assert row.end_user_id == euid
        assert row.revoked_at is None
        assert row.expires_at > row.created_at


def test_end_user_session_token_hash_is_unique():
    """Bearer-token collisions would silently log one end user in
    as another. Enforce uniqueness at the DB layer so a bug in
    the SDK can't drop us into that state."""
    with session_scope() as s:
        eu_a = _make_end_user(s, email="a@example.com")
        eu_b = _make_end_user(s, email="b@example.com")
        _make_end_user_session(s, eu_a, token="shared-hash")

    from db import SessionLocal
    s2 = SessionLocal()
    try:
        s2.add(EndUserSession(
            id=str(uuid.uuid4()),
            end_user_id=eu_b,
            token_hash="shared-hash",
            created_at=_utcnow(),
            expires_at=_utcnow() + timedelta(days=30),
        ))
        with pytest.raises(IntegrityError):
            s2.commit()
    finally:
        s2.rollback()
        s2.close()


def test_end_user_session_cascade_on_end_user_delete():
    with session_scope() as s:
        euid = _make_end_user(s)
        sid = _make_end_user_session(s, euid)

    with session_scope() as s:
        s.delete(s.get(EndUser, euid))

    with session_scope() as s:
        assert s.get(EndUserSession, sid) is None


# ---------- end_user_vendor_links roundtrip + cascade ---------- #


def test_vendor_link_roundtrip_with_server_default():
    """A link inserted without linked_via lands as 'invite_code' via
    the server default; linked_at lands as now()."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        euid = _make_end_user(s)
        s.execute(text(
            "INSERT INTO end_user_vendor_links (end_user_id, workspace_id) "
            "VALUES (:e, :w)"
        ), {"e": euid, "w": ws_id})

    with session_scope() as s:
        row = s.get(EndUserVendorLink, (euid, ws_id))
        assert row is not None
        assert row.linked_via == "invite_code"
        assert row.linked_at is not None
        assert row.removed_at is None


def test_vendor_link_composite_pk_rejects_duplicate():
    """An end user can't be linked to the same vendor twice — protects
    against invite-code double-redemption (Phase 27.2)."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        euid = _make_end_user(s)
        s.add(EndUserVendorLink(end_user_id=euid, workspace_id=ws_id))

    from db import SessionLocal
    s2 = SessionLocal()
    try:
        s2.add(EndUserVendorLink(end_user_id=euid, workspace_id=ws_id))
        with pytest.raises(IntegrityError):
            s2.commit()
    finally:
        s2.rollback()
        s2.close()


def test_same_end_user_linked_to_multiple_vendors():
    """The whole point of Phase 27: one end user, many vendors."""
    with session_scope() as s:
        ws_a = _make_workspace(s)
        ws_b = _make_workspace(s)
        euid = _make_end_user(s)
        s.add(EndUserVendorLink(end_user_id=euid, workspace_id=ws_a))
        s.add(EndUserVendorLink(end_user_id=euid, workspace_id=ws_b))

    with session_scope() as s:
        rows = s.execute(
            select(EndUserVendorLink).where(
                EndUserVendorLink.end_user_id == euid
            )
        ).scalars().all()
        assert {r.workspace_id for r in rows} == {ws_a, ws_b}


def test_vendor_link_cascade_on_end_user_delete():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        euid = _make_end_user(s)
        s.add(EndUserVendorLink(end_user_id=euid, workspace_id=ws_id))

    with session_scope() as s:
        s.delete(s.get(EndUser, euid))

    with session_scope() as s:
        assert s.get(EndUserVendorLink, (euid, ws_id)) is None


def test_vendor_link_cascade_on_workspace_delete():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        euid = _make_end_user(s)
        s.add(EndUserVendorLink(end_user_id=euid, workspace_id=ws_id))

    with session_scope() as s:
        s.delete(s.get(Workspace, ws_id))

    with session_scope() as s:
        assert s.get(EndUserVendorLink, (euid, ws_id)) is None


# ---------- end_user_signin_tokens roundtrip ---------- #


def test_signin_token_roundtrip():
    """Token row stores email + hash; consumed_at + vendor_invite_code
    default to NULL on insert."""
    token_hash = "a" * 64
    with session_scope() as s:
        s.add(EndUserSigninToken(
            token_hash=token_hash,
            email="signin@example.com",
            created_at=_utcnow(),
            expires_at=_utcnow() + timedelta(minutes=15),
        ))

    with session_scope() as s:
        row = s.get(EndUserSigninToken, token_hash)
        assert row is not None
        assert row.email == "signin@example.com"
        assert row.consumed_at is None
        assert row.vendor_invite_code is None


def test_signin_token_carries_vendor_invite_code():
    """Phase 25.2's vendor_invite_code field rides through the email
    round-trip so the consume side can link end_user→vendor in one
    transaction."""
    token_hash = "b" * 64
    invite = "inv-" + str(uuid.uuid4())[:8]
    with session_scope() as s:
        s.add(EndUserSigninToken(
            token_hash=token_hash,
            email="invite@example.com",
            created_at=_utcnow(),
            expires_at=_utcnow() + timedelta(minutes=15),
            vendor_invite_code=invite,
        ))

    with session_scope() as s:
        row = s.get(EndUserSigninToken, token_hash)
        assert row.vendor_invite_code == invite


# ---------- widget_conversations.end_user_id ---------- #


def _start_widget_conversation(
    s,
    workspace_id: str,
    *,
    end_user_id: str | None = None,
) -> str:
    conv_id = str(uuid.uuid4())
    now = _utcnow()
    s.add(WidgetConversation(
        id=conv_id,
        workspace_id=workspace_id,
        customer_facing_agent_name="support-bot",
        anon_user_id="anon-1" if end_user_id is None else None,
        end_user_id=end_user_id,
        started_at=now,
        last_message_at=now,
    ))
    s.flush()
    return conv_id


def test_widget_conversation_end_user_id_nullable():
    """Anonymous conversations (existing v1 behavior) leave the
    column NULL. Confirms the migration didn't accidentally enforce
    NOT NULL."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        conv_id = _start_widget_conversation(s, ws_id, end_user_id=None)

    with session_scope() as s:
        c = s.get(WidgetConversation, conv_id)
        assert c.end_user_id is None
        assert c.anon_user_id == "anon-1"


def test_widget_conversation_end_user_id_roundtrip():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        euid = _make_end_user(s)
        conv_id = _start_widget_conversation(s, ws_id, end_user_id=euid)

    with session_scope() as s:
        c = s.get(WidgetConversation, conv_id)
        assert c.end_user_id == euid


def test_widget_conversation_end_user_set_null_on_end_user_delete():
    """SET NULL (not CASCADE): the conversation row survives so the
    vendor's audit trail isn't gapped if the end-user account is
    later removed."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        euid = _make_end_user(s)
        conv_id = _start_widget_conversation(s, ws_id, end_user_id=euid)

    with session_scope() as s:
        s.delete(s.get(EndUser, euid))

    with session_scope() as s:
        c = s.get(WidgetConversation, conv_id)
        assert c is not None  # conversation survives
        assert c.end_user_id is None  # FK SET NULL fired


# ---------- Index landings ---------- #


@pytest.mark.parametrize("table,index", [
    ("end_user_sessions", "ix_end_user_sessions_end_user"),
    ("end_user_vendor_links", "ix_end_user_vendor_links_workspace"),
    ("end_user_signin_tokens", "ix_end_user_signin_tokens_email_created"),
    ("widget_conversations", "ix_widget_conversations_workspace_end_user"),
])
def test_expected_index_landed(table: str, index: str):
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = :t AND indexname = :i"
        ), {"t": table, "i": index}).first()
        assert r is not None, f"index {index} missing on {table}"


# ---------- Validation helpers ---------- #


def test_auth_provider_validator():
    assert is_valid_end_user_auth_provider("magic_link")
    assert is_valid_end_user_auth_provider("siwa")
    for bad in ("MAGIC_LINK", "google_oauth", "apikey", "", None, 1):
        assert not is_valid_end_user_auth_provider(bad)


def test_link_via_validator():
    for ok in ("invite_code", "direct_invite", "public_discovery"):
        assert is_valid_end_user_vendor_link_via(ok)
    for bad in ("INVITE_CODE", "magic", "", None, 1):
        assert not is_valid_end_user_vendor_link_via(bad)


def test_defaults_match_frozensets():
    assert DEFAULT_END_USER_AUTH_PROVIDER in _VALID_END_USER_AUTH_PROVIDERS
    assert DEFAULT_END_USER_VENDOR_LINK_VIA in _VALID_END_USER_VENDOR_LINK_VIA
    assert _VALID_END_USER_AUTH_PROVIDERS == {"magic_link", "siwa"}
    assert _VALID_END_USER_VENDOR_LINK_VIA == {
        "invite_code", "direct_invite", "public_discovery",
    }
