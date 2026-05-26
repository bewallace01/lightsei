"""Phase 28.1: tests for end_user_push_subscriptions.

Surfaces:

1. Roundtrip + server defaults (created_at).
2. Composite uniqueness on (end_user_id, endpoint).
3. Same endpoint allowed across DIFFERENT end_users (multi-user
   shared device case).
4. FK CASCADE on end_user delete.
5. Index landings (the unique constraint + the partial active index).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from db import session_scope
from models import EndUser, EndUserPushSubscription


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_end_user(s, *, email: str | None = None) -> str:
    euid = str(uuid.uuid4())
    s.add(EndUser(
        id=euid, email=email or f"eu-{euid[:8]}@example.com",
    ))
    s.flush()
    return euid


def _make_sub(
    s, end_user_id: str, *, endpoint: str | None = None,
) -> str:
    sid = str(uuid.uuid4())
    s.add(EndUserPushSubscription(
        id=sid,
        end_user_id=end_user_id,
        endpoint=endpoint or f"https://push.example.com/{sid}",
        p256dh="BAaaaa-fake-p256dh-key",
        auth="BBbbbb-fake-auth-secret",
    ))
    s.flush()
    return sid


# ---------- Roundtrip ---------- #


def test_push_subscription_roundtrip_with_server_defaults():
    with session_scope() as s:
        euid = _make_end_user(s)
        sid = _make_sub(s, euid)

    with session_scope() as s:
        row = s.get(EndUserPushSubscription, sid)
        assert row is not None
        assert row.end_user_id == euid
        assert row.endpoint.startswith("https://push.example.com/")
        assert row.p256dh.startswith("BAaaaa-")
        assert row.auth.startswith("BBbbbb-")
        assert row.created_at is not None  # server_default now()
        assert row.last_used_at is None
        assert row.revoked_at is None


# ---------- Composite uniqueness ---------- #


def test_composite_unique_rejects_duplicate_endpoint_for_same_end_user():
    """Re-subscribing from the same device with the same endpoint
    must hit the unique constraint. The Phase 28.5 subscribe endpoint
    uses upsert to avoid the IntegrityError; this test confirms the
    constraint exists so an upsert is necessary."""
    with session_scope() as s:
        euid = _make_end_user(s)
        _make_sub(s, euid, endpoint="https://push.example.com/dup")

    from db import SessionLocal
    s2 = SessionLocal()
    try:
        s2.add(EndUserPushSubscription(
            id=str(uuid.uuid4()),
            end_user_id=euid,
            endpoint="https://push.example.com/dup",  # collision
            p256dh="other-p256dh",
            auth="other-auth",
        ))
        with pytest.raises(IntegrityError):
            s2.commit()
    finally:
        s2.rollback()
        s2.close()


def test_same_endpoint_allowed_for_different_end_users():
    """Shared-device case (two end-user accounts on the same browser
    after sign-out + sign-in). Each end_user gets their own row even
    though endpoint matches."""
    endpoint = "https://push.example.com/shared-device"
    with session_scope() as s:
        eu_a = _make_end_user(s, email="a@example.com")
        eu_b = _make_end_user(s, email="b@example.com")
        _make_sub(s, eu_a, endpoint=endpoint)
        _make_sub(s, eu_b, endpoint=endpoint)

    with session_scope() as s:
        rows = s.execute(
            select(EndUserPushSubscription)
            .where(EndUserPushSubscription.endpoint == endpoint)
        ).scalars().all()
        assert {r.end_user_id for r in rows} == {eu_a, eu_b}


# ---------- FK cascade ---------- #


def test_push_subscription_cascade_on_end_user_delete():
    with session_scope() as s:
        euid = _make_end_user(s)
        _make_sub(s, euid)
        _make_sub(s, euid, endpoint="https://push.example.com/second")

    with session_scope() as s:
        s.delete(s.get(EndUser, euid))

    with session_scope() as s:
        rows = s.execute(
            select(EndUserPushSubscription)
            .where(EndUserPushSubscription.end_user_id == euid)
        ).scalars().all()
        assert rows == []


# ---------- Optional fields ---------- #


def test_last_used_at_and_revoked_at_roundtrip():
    """Phase 28.2 will bump last_used_at on each send + set
    revoked_at on 410 Gone. Confirm both columns hold timestamps
    on roundtrip."""
    now = _utcnow()
    with session_scope() as s:
        euid = _make_end_user(s)
        sid = str(uuid.uuid4())
        s.add(EndUserPushSubscription(
            id=sid,
            end_user_id=euid,
            endpoint="https://push.example.com/used",
            p256dh="p", auth="a",
            last_used_at=now, revoked_at=now,
        ))

    with session_scope() as s:
        row = s.get(EndUserPushSubscription, sid)
        assert row.last_used_at is not None
        assert row.revoked_at is not None


# ---------- Index landings ---------- #


def test_unique_constraint_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT conname FROM pg_constraint "
            "WHERE conname = "
            "'uq_end_user_push_subscriptions_end_user_endpoint'"
        )).first()
        assert r is not None


def test_active_partial_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'end_user_push_subscriptions' "
            "AND indexname = 'ix_end_user_push_subscriptions_active'"
        )).first()
        assert r is not None
        # Partial-where clause filters on revoked_at IS NULL so the
        # active fan-out scan stays small.
        assert "revoked_at" in r[0].lower()
