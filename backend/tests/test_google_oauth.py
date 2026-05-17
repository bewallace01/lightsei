"""Phase 17.3: Google OAuth backend tests.

Three surfaces:

1. `backend/google_oauth.py` pure helpers — PKCE pair generation,
   state generation, authorization URL assembly, `is_configured`.
2. `GET /auth/google/start` — 503 when unconfigured, persists state +
   verifier, returns the authorization URL.
3. `GET /auth/google/callback` — stub Google's token + userinfo
   endpoints; assert new-user / returning-user / email-link paths.

Tests monkeypatch httpx.post + httpx.get inside google_oauth so they
don't hit Google's API. Same shape as the SDK tests stubbing the
Anthropic SDK.
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

import google_oauth as goog
from db import session_scope
from models import OAuthPendingState, User, Workspace
from tests.conftest import auth_headers


@pytest.fixture
def _configured(monkeypatch):
    """Set fake Google OAuth client credentials so endpoints don't 503.
    Yields the configured-state; tests that want the unconfigured path
    skip this fixture."""
    monkeypatch.setenv("LIGHTSEI_GOOGLE_CLIENT_ID", "client-id-test")
    monkeypatch.setenv("LIGHTSEI_GOOGLE_CLIENT_SECRET", "client-secret-test")
    monkeypatch.setenv(
        "LIGHTSEI_GOOGLE_REDIRECT_URI",
        "http://localhost:3000/auth/google/callback",
    )
    yield


# ---------- Pure helpers ---------- #


def test_pkce_pair_round_trip():
    """Verifier hashed with sha256 + urlsafe-base64 + stripped
    padding should equal the challenge — that's what Google checks."""
    verifier, challenge = goog.new_pkce_pair()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert challenge == expected


def test_pkce_pair_is_high_entropy():
    """Two consecutive calls should never return the same verifier."""
    a, _ = goog.new_pkce_pair()
    b, _ = goog.new_pkce_pair()
    assert a != b


def test_state_is_high_entropy():
    a = goog.new_state()
    b = goog.new_state()
    assert a != b
    assert len(a) >= 32


def test_is_configured_reads_env(monkeypatch):
    monkeypatch.delenv("LIGHTSEI_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("LIGHTSEI_GOOGLE_CLIENT_SECRET", raising=False)
    assert not goog.is_configured()

    monkeypatch.setenv("LIGHTSEI_GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("LIGHTSEI_GOOGLE_CLIENT_SECRET", "y")
    assert goog.is_configured()


def test_build_authorization_url_carries_required_params(_configured):
    url = goog.build_authorization_url(
        state="state-abc", challenge="challenge-xyz",
    )
    parsed = urlparse(url)
    assert parsed.netloc == "accounts.google.com"
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["client-id-test"]
    assert qs["response_type"] == ["code"]
    assert qs["state"] == ["state-abc"]
    assert qs["code_challenge"] == ["challenge-xyz"]
    assert qs["code_challenge_method"] == ["S256"]
    # Default scopes present.
    scope = qs["scope"][0].split()
    assert "openid" in scope
    assert "email" in scope


# ---------- GET /auth/google/start ---------- #


def test_start_503_when_not_configured(client, monkeypatch):
    monkeypatch.delenv("LIGHTSEI_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("LIGHTSEI_GOOGLE_CLIENT_SECRET", raising=False)
    r = client.get("/auth/google/start")
    assert r.status_code == 503
    assert "LIGHTSEI_GOOGLE_CLIENT_ID" in r.json()["detail"]


def test_start_returns_authorization_url_and_persists_state(
    client, _configured,
):
    r = client.get("/auth/google/start")
    assert r.status_code == 200
    body = r.json()
    assert body["authorization_url"].startswith(
        "https://accounts.google.com/o/oauth2/v2/auth?"
    )
    state = body["state"]

    # State + verifier got persisted with a 10-minute TTL.
    with session_scope() as s:
        row = s.get(OAuthPendingState, state)
    assert row is not None
    assert row.code_verifier
    assert (row.expires_at - row.created_at).total_seconds() == pytest.approx(
        600, abs=2,
    )


def test_start_threads_redirect_after_through(client, _configured):
    """`redirect_after` query param survives the state row so callback
    can hand the dashboard the original target post-signin."""
    r = client.get("/auth/google/start?redirect_after=/agents/argus")
    state = r.json()["state"]
    with session_scope() as s:
        row = s.get(OAuthPendingState, state)
    assert row.redirect_after == "/agents/argus"


# ---------- GET /auth/google/callback ---------- #


def _stub_google(monkeypatch, *, sub: str, email: str, email_verified: bool = True):
    """Stub httpx.post (token exchange) + httpx.get (userinfo)
    inside the google_oauth module so the callback test path runs
    end-to-end without hitting Google."""
    def fake_post(url, **kwargs):
        assert url == goog.GOOGLE_TOKEN_URL
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "access_token": "fake-access-token",
                "token_type": "Bearer",
                "expires_in": 3599,
                "id_token": "fake.id.token",
            },
        )

    def fake_get(url, **kwargs):
        assert url == goog.GOOGLE_USERINFO_URL
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "sub": sub,
                "email": email,
                "email_verified": email_verified,
                "name": "Test User",
            },
        )

    monkeypatch.setattr(goog.httpx, "post", fake_post)
    monkeypatch.setattr(goog.httpx, "get", fake_get)


def _prime_state(client) -> str:
    """Hit /auth/google/start to populate a valid state row, return
    the state value the callback test uses."""
    r = client.get("/auth/google/start")
    return r.json()["state"]


def test_callback_creates_new_user_and_workspace(
    client, _configured, monkeypatch,
):
    _stub_google(
        monkeypatch,
        sub="google-sub-fresh",
        email="fresh@example.com",
    )
    state = _prime_state(client)
    r = client.get(
        f"/auth/google/callback?code=fake-code&state={state}",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_new_user"] is True
    assert body["user"]["email"] == "fresh@example.com"
    assert body["session_token"]
    assert body["redirect_after"] == "/"

    # User landed verified + auth_provider='google_oauth' +
    # google_user_id matching the stubbed sub.
    with session_scope() as s:
        u = s.execute(
            select(User).where(User.email == "fresh@example.com")
        ).scalar_one()
    assert u.email_verified is True
    assert u.auth_provider == "google_oauth"
    assert u.google_user_id == "google-sub-fresh"

    # Workspace defaults from 17.1 apply.
    with session_scope() as s:
        ws = s.get(Workspace, body["workspace"]["id"])
    assert ws.plan_tier == "free"


def test_callback_signs_in_returning_user_by_google_sub(
    client, _configured, monkeypatch,
):
    """Same sub → matched directly, no email check needed."""
    # First sign-in creates the user.
    _stub_google(monkeypatch, sub="google-sub-r1", email="ret@example.com")
    state1 = _prime_state(client)
    r1 = client.get(f"/auth/google/callback?code=c1&state={state1}")
    user_id = r1.json()["user"]["id"]

    # Second sign-in: same sub but different email (e.g. user renamed
    # their Google account). Should still match the same row via
    # google_user_id and NOT create a duplicate.
    _stub_google(monkeypatch, sub="google-sub-r1", email="renamed@example.com")
    state2 = _prime_state(client)
    r2 = client.get(f"/auth/google/callback?code=c2&state={state2}")
    assert r2.status_code == 200
    assert r2.json()["is_new_user"] is False
    assert r2.json()["user"]["id"] == user_id


def test_callback_links_google_to_existing_email_user(
    client, _configured, monkeypatch, alice,
):
    """An existing apikey-signup user (alice) signs in via Google for
    the first time. Should match by verified-email and link the
    google_user_id rather than creating a duplicate."""
    _stub_google(
        monkeypatch,
        sub="google-sub-alice",
        email="alice@example.com",
        email_verified=True,
    )
    state = _prime_state(client)
    r = client.get(f"/auth/google/callback?code=c&state={state}")
    assert r.status_code == 200
    assert r.json()["is_new_user"] is False
    assert r.json()["user"]["id"] == alice["user"]["id"]

    with session_scope() as s:
        u = s.get(User, alice["user"]["id"])
    assert u.google_user_id == "google-sub-alice"
    assert u.email_verified is True
    # auth_provider stays 'apikey' — we don't rewrite history.
    assert u.auth_provider == "apikey"


def test_callback_does_not_link_to_unverified_email(
    client, _configured, monkeypatch, alice,
):
    """Email matching only happens when Google says the email is
    verified — otherwise an attacker who controls a Google account
    could claim someone else's existing Lightsei user via an
    unverified email claim.

    When Google says unverified AND the email is already taken by an
    existing user, the callback returns 400 email_already_in_use
    rather than crashing on the unique-email constraint. The existing
    user's row stays intact + un-linked."""
    _stub_google(
        monkeypatch,
        sub="google-sub-attacker",
        email="alice@example.com",
        email_verified=False,
    )
    state = _prime_state(client)
    r = client.get(f"/auth/google/callback?code=c&state={state}")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "email_already_in_use"

    # Alice's row is unchanged — not linked, not promoted.
    with session_scope() as s:
        u = s.get(User, alice["user"]["id"])
    assert u.google_user_id is None
    assert u.auth_provider == "apikey"


def test_callback_rejects_unknown_state(client, _configured, monkeypatch):
    _stub_google(monkeypatch, sub="x", email="y@z.com")
    r = client.get(
        "/auth/google/callback?code=c&state=never-issued",
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_state"


def test_callback_rejects_expired_state(client, _configured, monkeypatch):
    state = _prime_state(client)
    with session_scope() as s:
        row = s.get(OAuthPendingState, state)
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    _stub_google(monkeypatch, sub="x", email="y@z.com")
    r = client.get(
        f"/auth/google/callback?code=c&state={state}",
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_state"


def test_callback_is_single_use_per_state(client, _configured, monkeypatch):
    """Second callback with the same state → 400 invalid_state because
    the row got deleted on the first consume."""
    _stub_google(monkeypatch, sub="s1", email="single@example.com")
    state = _prime_state(client)
    r1 = client.get(f"/auth/google/callback?code=c1&state={state}")
    assert r1.status_code == 200

    r2 = client.get(f"/auth/google/callback?code=c2&state={state}")
    assert r2.status_code == 400
    assert r2.json()["detail"]["error"] == "invalid_state"


def test_callback_surfaces_user_cancellation_as_400(client, _configured):
    """Google redirects with ?error=access_denied when the user clicks
    Cancel on the consent screen. We render that as a clean 400 so the
    dashboard's callback page can show a friendly message."""
    r = client.get(
        "/auth/google/callback?error=access_denied",
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "access_denied"
    assert "cancelled" in detail["message"].lower()


def test_callback_400_on_missing_params(client, _configured):
    r = client.get("/auth/google/callback")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "missing_params"


def test_callback_400_on_google_token_exchange_failure(
    client, _configured, monkeypatch,
):
    """Google's token endpoint returns 400 — we should surface a
    friendly 400 to the user, not a 500."""
    def fake_post(url, **kwargs):
        return SimpleNamespace(
            status_code=400,
            json=lambda: {"error": "invalid_grant"},
            text="invalid_grant",
        )

    monkeypatch.setattr(goog.httpx, "post", fake_post)

    state = _prime_state(client)
    r = client.get(f"/auth/google/callback?code=bad&state={state}")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "exchange_failed"


def test_callback_session_token_actually_authenticates(
    client, _configured, monkeypatch,
):
    _stub_google(monkeypatch, sub="s-auth", email="auth-test@example.com")
    state = _prime_state(client)
    r = client.get(f"/auth/google/callback?code=c&state={state}")
    sess = r.json()["session_token"]

    r2 = client.get("/auth/me", headers=auth_headers(sess))
    assert r2.status_code == 200
    assert r2.json()["user"]["email"] == "auth-test@example.com"
