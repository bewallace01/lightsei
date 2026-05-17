"""Phase 17.1: tests for the auth + billing schema backbone.

Three things under test:

1. The `_VALID_PLAN_TIERS` / `_VALID_AUTH_PROVIDERS` validators
   + their helper functions.
2. Default behavior at the DB layer — fresh workspaces land on
   plan_tier='free' + free_credits_remaining_usd=5.00; fresh
   users land on email_verified=false + auth_provider='apikey'.
3. The new `email_signin_tokens` table: round-trips, the
   (email, created_at DESC) index exists for the rate-limit query.

Actual auth / billing flows that USE these columns live in
17.2-17.5 and get their own test files.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from db import session_scope
from models import (
    DEFAULT_AUTH_PROVIDER,
    DEFAULT_PLAN_TIER,
    _VALID_AUTH_PROVIDERS,
    _VALID_PLAN_TIERS,
    EmailSigninToken,
    User,
    Workspace,
    is_valid_auth_provider,
    is_valid_plan_tier,
)
from tests.conftest import auth_headers, signup


# ---------- Validators ---------- #


def test_plan_tier_validator_accepts_canonical_values():
    for tier in ("free", "paid"):
        assert is_valid_plan_tier(tier), tier


def test_plan_tier_validator_rejects_off_list_and_non_string():
    for bad in ("FREE", "pro", "", None, 0, [], {}):
        assert not is_valid_plan_tier(bad), repr(bad)


def test_plan_tier_default_is_in_valid_set():
    assert DEFAULT_PLAN_TIER in _VALID_PLAN_TIERS


def test_auth_provider_validator_accepts_three_paths():
    for p in ("apikey", "magic_link", "google_oauth"):
        assert is_valid_auth_provider(p), p


def test_auth_provider_validator_rejects_off_list():
    for bad in ("APIKEY", "magicLink", "github_oauth", "", None, 42):
        assert not is_valid_auth_provider(bad), repr(bad)


def test_auth_provider_default_is_in_valid_set():
    assert DEFAULT_AUTH_PROVIDER in _VALID_AUTH_PROVIDERS


def test_valid_sets_are_frozen():
    """Documenting the invariant — anyone reaching for .add() at
    runtime gets caught."""
    assert isinstance(_VALID_PLAN_TIERS, frozenset)
    assert isinstance(_VALID_AUTH_PROVIDERS, frozenset)


# ---------- Workspace defaults ---------- #


def test_fresh_workspace_lands_on_free_tier(client, alice):
    """`alice` fixture creates a workspace through the signup
    endpoint; confirm the new columns default sanely."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
    assert ws.plan_tier == "free"
    assert float(ws.free_credits_remaining_usd) == pytest.approx(5.00, abs=1e-6)
    assert ws.stripe_customer_id is None
    assert ws.stripe_subscription_id is None


def test_workspace_can_be_updated_to_paid(client, alice):
    """The bulk transitions (free → paid via Stripe webhook in 17.4
    or admin override) need to round-trip cleanly through the model."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        ws.plan_tier = "paid"
        ws.stripe_customer_id = "cus_TEST123"
        ws.stripe_subscription_id = "sub_TEST456"

    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
    assert ws.plan_tier == "paid"
    assert ws.stripe_customer_id == "cus_TEST123"
    assert ws.stripe_subscription_id == "sub_TEST456"


def test_stripe_customer_id_unique_when_set(client, alice, bob):
    """Two workspaces can't share a Stripe customer (1:1 mapping).
    NULL is allowed multiple times — that's the partial-unique-index
    behavior from the migration."""
    # First write commits normally.
    with session_scope() as s:
        a = s.get(Workspace, alice["workspace"]["id"])
        a.stripe_customer_id = "cus_DUPE"

    # Second write needs to fail. Use a raw connection so we can
    # observe the constraint violation without session_scope's
    # commit-on-exit fighting the failed transaction state.
    from db import SessionLocal
    s2 = SessionLocal()
    try:
        b = s2.get(Workspace, bob["workspace"]["id"])
        b.stripe_customer_id = "cus_DUPE"
        with pytest.raises(IntegrityError):
            s2.flush()
    finally:
        s2.rollback()
        s2.close()


def test_free_credits_decrement_persists(client, alice):
    """The paywall middleware (17.5) will decrement on every Run row
    creation. Confirm the precision survives — 5.00 - 0.001234 should
    round-trip without losing the trailing precision."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        ws.free_credits_remaining_usd = (
            Decimal("5.000000") - Decimal("0.001234")
        )

    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
    assert float(ws.free_credits_remaining_usd) == pytest.approx(
        4.998766, abs=1e-6,
    )


# ---------- User defaults ---------- #


def test_fresh_user_via_apikey_signup_lands_as_unverified(client, alice):
    """The existing /auth/signup endpoint (which `alice` uses) creates
    a user without a magic-link round-trip. server_default puts them on
    auth_provider='apikey' + email_verified=false. New flows in 17.2 +
    17.3 set the field explicitly to other values."""
    user_id = alice["user"]["id"]
    with session_scope() as s:
        u = s.get(User, user_id)
    assert u.auth_provider == "apikey"
    assert u.email_verified is False
    assert u.google_user_id is None


def test_user_can_be_updated_to_verified(client, alice):
    user_id = alice["user"]["id"]
    with session_scope() as s:
        u = s.get(User, user_id)
        u.email_verified = True
        u.auth_provider = "magic_link"

    with session_scope() as s:
        u = s.get(User, user_id)
    assert u.email_verified is True
    assert u.auth_provider == "magic_link"


def test_google_user_id_unique_when_set(client, alice, bob):
    """One Google identity maps to one User row. Partial-unique on
    NOT NULL so apikey users (NULL google_user_id) don't collide."""
    with session_scope() as s:
        a = s.get(User, alice["user"]["id"])
        a.google_user_id = "google-sub-123"

    from db import SessionLocal
    s2 = SessionLocal()
    try:
        b = s2.get(User, bob["user"]["id"])
        b.google_user_id = "google-sub-123"
        with pytest.raises(IntegrityError):
            s2.flush()
    finally:
        s2.rollback()
        s2.close()


# ---------- EmailSigninToken ---------- #


def _hash_for_test(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def test_email_signin_token_round_trip(client, alice):
    """Insert + read + mark-consumed flow that 17.2's consume
    endpoint will run."""
    now = datetime.now(timezone.utc)
    plaintext = "magic-token-abc"
    with session_scope() as s:
        s.add(EmailSigninToken(
            token_hash=_hash_for_test(plaintext),
            email="alice@example.com",
            created_at=now,
            expires_at=now + timedelta(minutes=15),
        ))

    with session_scope() as s:
        row = s.get(EmailSigninToken, _hash_for_test(plaintext))
    assert row is not None
    assert row.email == "alice@example.com"
    assert row.consumed_at is None

    with session_scope() as s:
        row = s.get(EmailSigninToken, _hash_for_test(plaintext))
        row.consumed_at = datetime.now(timezone.utc)

    with session_scope() as s:
        row = s.get(EmailSigninToken, _hash_for_test(plaintext))
    assert row.consumed_at is not None


def test_email_signin_token_pk_prevents_duplicate_inserts(client, alice):
    """token_hash is the PK — same hash can't be inserted twice.
    Belt-and-suspenders against a hash-collision bug in 17.2's
    generator (vanishingly unlikely with sha256, but free to test)."""
    now = datetime.now(timezone.utc)
    h = _hash_for_test("collision-test-token")
    with session_scope() as s:
        s.add(EmailSigninToken(
            token_hash=h, email="x@y.com",
            created_at=now, expires_at=now + timedelta(minutes=15),
        ))

    from db import SessionLocal
    s2 = SessionLocal()
    try:
        s2.add(EmailSigninToken(
            token_hash=h, email="x@y.com",
            created_at=now, expires_at=now + timedelta(minutes=15),
        ))
        with pytest.raises(IntegrityError):
            s2.flush()
    finally:
        s2.rollback()
        s2.close()


def test_email_index_supports_rate_limit_query(client, alice):
    """The (email, created_at DESC) index lets 17.2 ask 'how many
    tokens did this email get in the last hour' cheaply. Smoke test
    via a query that should use the index (we don't EXPLAIN here,
    just confirm the query runs)."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    with session_scope() as s:
        for i in range(3):
            s.add(EmailSigninToken(
                token_hash=_hash_for_test(f"t-{i}"),
                email="alice@example.com",
                created_at=now - timedelta(minutes=i * 10),
                expires_at=now + timedelta(minutes=15),
            ))

    with session_scope() as s:
        count = s.execute(
            text(
                "SELECT COUNT(*) FROM email_signin_tokens "
                "WHERE email = :em AND created_at >= :cutoff"
            ),
            {"em": "alice@example.com", "cutoff": cutoff},
        ).scalar()
    assert count == 3


# ---------- Alembic backfill smoke test ---------- #


def test_existing_workspaces_landed_on_free_after_migration(client, alice):
    """0030's backfill targets every workspace with the same starting
    state a fresh signup would land with. Confirm the column populated
    + the value is what we expect."""
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        row = s.execute(
            text(
                "SELECT plan_tier, free_credits_remaining_usd "
                "FROM workspaces WHERE id = :w"
            ),
            {"w": workspace_id},
        ).first()
    assert row is not None
    assert row[0] == "free"
    assert float(row[1]) == pytest.approx(5.00, abs=1e-6)
