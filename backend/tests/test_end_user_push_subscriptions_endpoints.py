"""Phase 28.5: tests for the end-user push-subscription endpoints
plus the GET /me/end-user fields they coordinate with.

Endpoints under test:

  POST   /me/end-user/push-subscriptions
  DELETE /me/end-user/push-subscriptions
  GET    /me/end-user                  (push_vapid_public_key +
                                        has_active_push_subscription)

Cross-token-type + expired/revoked-session auth paths live in
test_end_user_auth.py, so these tests focus on row state, upsert
shape, soft-revoke idempotence, and cross-user isolation.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from db import session_scope
from keys import generate_session_token, hash_token
from models import (
    EndUser,
    EndUserPushSubscription,
    EndUserSession,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_end_user(*, email: str | None = None) -> tuple[str, str]:
    """Returns (end_user_id, plaintext_session_token)."""
    euid = str(uuid.uuid4())
    token = generate_session_token()
    with session_scope() as s:
        s.add(EndUser(
            id=euid,
            email=email or f"eu-{euid[:8]}@example.com",
            display_name="Alice",
        ))
        s.flush()
        s.add(EndUserSession(
            id=str(uuid.uuid4()),
            end_user_id=euid,
            token_hash=hash_token(token),
            created_at=_now(),
            expires_at=_now() + timedelta(days=30),
        ))
    return euid, token


def _eu_auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _sub_body(*, endpoint: str = "https://push.example/abc") -> dict[str, str]:
    return {
        "endpoint": endpoint,
        "p256dh": "BJxxx-p256dh-key",
        "auth": "auth-secret-xyz",
    }


# ---------- POST /me/end-user/push-subscriptions ---------- #


def test_post_subscription_requires_auth(client):
    r = client.post(
        "/me/end-user/push-subscriptions", json=_sub_body(),
    )
    assert r.status_code == 401


def test_post_subscription_creates_row(client):
    euid, token = _make_end_user()
    r = client.post(
        "/me/end-user/push-subscriptions",
        json=_sub_body(),
        headers=_eu_auth(token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"] == "https://push.example/abc"
    assert body["active"] is True
    assert isinstance(body["id"], str) and len(body["id"]) > 0

    with session_scope() as s:
        row = s.scalar(
            select(EndUserPushSubscription).where(
                EndUserPushSubscription.end_user_id == euid,
            )
        )
        assert row is not None
        assert row.endpoint == "https://push.example/abc"
        assert row.revoked_at is None
        assert row.p256dh == "BJxxx-p256dh-key"
        assert row.auth == "auth-secret-xyz"


def test_post_subscription_is_idempotent_same_endpoint(client):
    """POSTing the same endpoint twice updates the existing row
    rather than creating a duplicate. The composite unique on
    (end_user_id, endpoint) enforces this at the DB layer too."""
    euid, token = _make_end_user()
    r1 = client.post(
        "/me/end-user/push-subscriptions",
        json=_sub_body(),
        headers=_eu_auth(token),
    )
    r2 = client.post(
        "/me/end-user/push-subscriptions",
        json={
            "endpoint": "https://push.example/abc",
            "p256dh": "rotated-p256dh",
            "auth": "rotated-auth",
        },
        headers=_eu_auth(token),
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]

    with session_scope() as s:
        rows = s.scalars(
            select(EndUserPushSubscription).where(
                EndUserPushSubscription.end_user_id == euid,
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].p256dh == "rotated-p256dh"
        assert rows[0].auth == "rotated-auth"


def test_post_subscription_reactivates_revoked_row(client):
    """If the row was revoked (user disabled, or 410 cleanup ran),
    a fresh subscribe clears revoked_at so the send fan-out picks
    it back up."""
    euid, token = _make_end_user()
    # Initial subscribe.
    client.post(
        "/me/end-user/push-subscriptions",
        json=_sub_body(),
        headers=_eu_auth(token),
    )
    # Revoke.
    client.request(
        "DELETE",
        "/me/end-user/push-subscriptions",
        json={"endpoint": "https://push.example/abc"},
        headers=_eu_auth(token),
    )
    with session_scope() as s:
        row = s.scalar(select(EndUserPushSubscription))
        assert row.revoked_at is not None

    # Re-subscribe should clear revoked_at.
    r = client.post(
        "/me/end-user/push-subscriptions",
        json=_sub_body(),
        headers=_eu_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["active"] is True
    with session_scope() as s:
        row = s.scalar(select(EndUserPushSubscription))
        assert row.revoked_at is None


def test_post_subscription_rejects_empty_endpoint(client):
    _, token = _make_end_user()
    r = client.post(
        "/me/end-user/push-subscriptions",
        json={"endpoint": "", "p256dh": "k", "auth": "a"},
        headers=_eu_auth(token),
    )
    assert r.status_code == 422


# ---------- DELETE /me/end-user/push-subscriptions ---------- #


def test_delete_subscription_revokes_row(client):
    euid, token = _make_end_user()
    client.post(
        "/me/end-user/push-subscriptions",
        json=_sub_body(),
        headers=_eu_auth(token),
    )
    r = client.request(
        "DELETE",
        "/me/end-user/push-subscriptions",
        json={"endpoint": "https://push.example/abc"},
        headers=_eu_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "revoked": True,
        "endpoint": "https://push.example/abc",
    }

    with session_scope() as s:
        row = s.scalar(select(EndUserPushSubscription))
        assert row.revoked_at is not None


def test_delete_subscription_is_idempotent(client):
    """Second DELETE for the same endpoint still returns 200; the
    row is already revoked so revoked_at is left alone."""
    _, token = _make_end_user()
    client.post(
        "/me/end-user/push-subscriptions",
        json=_sub_body(),
        headers=_eu_auth(token),
    )
    r1 = client.request(
        "DELETE",
        "/me/end-user/push-subscriptions",
        json={"endpoint": "https://push.example/abc"},
        headers=_eu_auth(token),
    )
    with session_scope() as s:
        first_revoked_at = s.scalar(
            select(EndUserPushSubscription.revoked_at)
        )
    r2 = client.request(
        "DELETE",
        "/me/end-user/push-subscriptions",
        json={"endpoint": "https://push.example/abc"},
        headers=_eu_auth(token),
    )
    with session_scope() as s:
        second_revoked_at = s.scalar(
            select(EndUserPushSubscription.revoked_at)
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert first_revoked_at == second_revoked_at


def test_delete_subscription_returns_404_for_unknown_endpoint(client):
    _, token = _make_end_user()
    r = client.request(
        "DELETE",
        "/me/end-user/push-subscriptions",
        json={"endpoint": "https://push.example/never-subscribed"},
        headers=_eu_auth(token),
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "push_subscription_not_found"


def test_delete_subscription_cannot_revoke_another_users_endpoint(client):
    """Cross-user isolation: end user B sees a 404 trying to delete
    end user A's endpoint, never modifies A's row."""
    _, token_a = _make_end_user(email="a@example.com")
    _, token_b = _make_end_user(email="b@example.com")
    client.post(
        "/me/end-user/push-subscriptions",
        json=_sub_body(endpoint="https://push.example/a"),
        headers=_eu_auth(token_a),
    )

    r = client.request(
        "DELETE",
        "/me/end-user/push-subscriptions",
        json={"endpoint": "https://push.example/a"},
        headers=_eu_auth(token_b),
    )
    assert r.status_code == 404

    with session_scope() as s:
        row = s.scalar(select(EndUserPushSubscription))
        assert row.revoked_at is None  # A's row untouched


# ---------- GET /me/end-user push fields ---------- #


def test_me_end_user_surfaces_vapid_public_key(client, monkeypatch):
    monkeypatch.setenv(
        "LIGHTSEI_VAPID_PUBLIC_KEY", "BTest-public-key-base64url"
    )
    _, token = _make_end_user()
    r = client.get("/me/end-user", headers=_eu_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["push_vapid_public_key"] == "BTest-public-key-base64url"


def test_me_end_user_vapid_key_is_null_when_unset(client, monkeypatch):
    monkeypatch.delenv("LIGHTSEI_VAPID_PUBLIC_KEY", raising=False)
    _, token = _make_end_user()
    r = client.get("/me/end-user", headers=_eu_auth(token))
    body = r.json()
    assert body["push_vapid_public_key"] is None


def test_me_end_user_has_active_push_subscription_flips_with_state(
    client,
):
    """Flag mirrors row state: false initially → true after POST →
    false after DELETE."""
    _, token = _make_end_user()

    r = client.get("/me/end-user", headers=_eu_auth(token))
    assert r.json()["has_active_push_subscription"] is False

    client.post(
        "/me/end-user/push-subscriptions",
        json=_sub_body(),
        headers=_eu_auth(token),
    )
    r = client.get("/me/end-user", headers=_eu_auth(token))
    assert r.json()["has_active_push_subscription"] is True

    client.request(
        "DELETE",
        "/me/end-user/push-subscriptions",
        json={"endpoint": "https://push.example/abc"},
        headers=_eu_auth(token),
    )
    r = client.get("/me/end-user", headers=_eu_auth(token))
    assert r.json()["has_active_push_subscription"] is False
