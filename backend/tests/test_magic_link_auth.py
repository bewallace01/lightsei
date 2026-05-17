"""Phase 17.2: magic-link auth backend tests.

Three surfaces:

1. `backend/email_provider.py` capture-mode (the network-free path
   tests use): send_magic_link enqueues to `_captured`.
2. `POST /auth/magic-link/request` — rate-limit per-email, always-200
   contract, token row gets inserted, email gets sent (captured).
3. `POST /auth/magic-link/consume` — signs in existing user, creates
   new user+workspace pair, single-use enforcement, expired-token 422,
   unknown-token 422.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

import email_provider as ep
from db import session_scope
from models import EmailSigninToken, User, Workspace
from tests.conftest import auth_headers


@pytest.fixture(autouse=True)
def _reset_email_capture():
    """Capture list leaks across tests; clear between to keep
    assertions clean."""
    ep._reset_for_tests()
    yield
    ep._reset_for_tests()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------- email_provider capture mode ---------- #


def test_send_magic_link_captures_when_no_api_key():
    """No LIGHTSEI_RESEND_API_KEY → automatic capture mode. No
    network call; entry appended to _captured with the magic URL
    intact."""
    ep.send_magic_link(
        email="alice@example.com",
        token="tok-abc",
        dashboard_url="http://dash.test",
    )
    captured = ep.captured_emails()
    assert len(captured) == 1
    assert captured[0]["to"] == ["alice@example.com"]
    assert captured[0]["subject"] == "Sign in to Lightsei"
    assert (
        captured[0]["_magic_url"]
        == "http://dash.test/auth/magic-link?token=tok-abc"
    )
    # Both plain text and HTML bodies present.
    assert "tok-abc" in captured[0]["text"]
    assert "tok-abc" in captured[0]["html"]


def test_send_magic_link_capture_mode_when_env_var_forces_fake(monkeypatch):
    """Even with an API key present, LIGHTSEI_EMAIL_FAKE_CAPTURE=1
    keeps us in capture mode — lets prod-config tests stay
    network-free."""
    monkeypatch.setenv("LIGHTSEI_RESEND_API_KEY", "re_pretend")
    monkeypatch.setenv("LIGHTSEI_EMAIL_FAKE_CAPTURE", "1")
    ep.send_magic_link(
        email="bob@example.com", token="t-2",
        dashboard_url="http://x.test",
    )
    assert len(ep.captured_emails()) == 1


def test_dashboard_url_trailing_slash_normalized():
    ep.send_magic_link(
        email="alice@example.com",
        token="tok-1",
        dashboard_url="http://dash.test/",
    )
    url = ep.captured_emails()[0]["_magic_url"]
    # No double-slash before /auth.
    assert "//auth" not in url.replace("http://", "")
    assert url == "http://dash.test/auth/magic-link?token=tok-1"


# ---------- POST /auth/magic-link/request ---------- #


def test_request_magic_link_inserts_token_and_sends_email(client):
    """Happy path: a fresh email request inserts an
    email_signin_tokens row + captures an email."""
    r = client.post(
        "/auth/magic-link/request",
        json={"email": "new@example.com"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}

    with session_scope() as s:
        rows = s.execute(
            select(EmailSigninToken).where(
                EmailSigninToken.email == "new@example.com"
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.consumed_at is None
    # Default TTL is ~15 min from creation.
    assert (row.expires_at - row.created_at).total_seconds() == pytest.approx(
        900, abs=2,
    )

    captured = ep.captured_emails()
    assert len(captured) == 1
    assert captured[0]["to"] == ["new@example.com"]


def test_request_magic_link_lowercases_email(client):
    """Stored hash must match on consume regardless of input casing —
    normalize at the door."""
    r = client.post(
        "/auth/magic-link/request",
        json={"email": "Mixed.Case@Example.COM"},
    )
    assert r.status_code == 200
    with session_scope() as s:
        emails = [
            row.email for row in s.execute(select(EmailSigninToken)).scalars()
        ]
    assert emails == ["mixed.case@example.com"]


def test_request_magic_link_always_200_even_for_unknown_email(client):
    """No leak: an unknown email looks identical to a known one. The
    captured email goes out (which is fine — it's just a magic link the
    recipient can ignore), but the HTTP response is the same."""
    r = client.post(
        "/auth/magic-link/request",
        json={"email": "nobody@example.com"},
    )
    assert r.status_code == 200


def test_request_magic_link_per_email_rate_limit(client):
    """After MAGIC_LINK_MAX_PER_HOUR (5) requests for the same email,
    further requests still return 200 but don't insert new tokens or
    capture new emails — silent throttle.

    Reset the per-IP signup limiter between calls so we're testing the
    per-email cap (the thing this test cares about), not the existing
    SIGNUP_LIMIT_PER_MIN which would otherwise fire first."""
    from limits import reset_counter_for_tests
    email = "throttle@example.com"
    for _ in range(5):
        reset_counter_for_tests()
        r = client.post(
            "/auth/magic-link/request", json={"email": email},
        )
        assert r.status_code == 200

    with session_scope() as s:
        count = s.execute(
            select(EmailSigninToken).where(EmailSigninToken.email == email)
        ).scalars().all()
    assert len(count) == 5
    assert len(ep.captured_emails()) == 5

    # 6th request: per-email throttle kicks in. 200 (no leak) but no
    # new token + no new email.
    reset_counter_for_tests()
    r = client.post(
        "/auth/magic-link/request", json={"email": email},
    )
    assert r.status_code == 200
    with session_scope() as s:
        count_after = s.execute(
            select(EmailSigninToken).where(EmailSigninToken.email == email)
        ).scalars().all()
    assert len(count_after) == 5
    assert len(ep.captured_emails()) == 5


def test_request_magic_link_validates_email_format(client):
    """Pydantic's EmailStr rejects malformed input with a 422 — no
    point hitting the rate-limiter on garbage."""
    r = client.post(
        "/auth/magic-link/request", json={"email": "not-an-email"},
    )
    assert r.status_code == 422


# ---------- POST /auth/magic-link/consume ---------- #


def _request_and_grab_token(client, email: str) -> str:
    """Helper: do a request + return the unhashed token from the
    captured email. Mirrors what the dashboard does on click."""
    client.post("/auth/magic-link/request", json={"email": email})
    captured = ep.captured_emails()
    magic_url = captured[-1]["_magic_url"]
    # token param is the last URL segment after `?token=`
    return magic_url.split("token=", 1)[1]


def test_consume_creates_new_user_and_workspace(client):
    """First-time email → creates a User + Workspace pair, returns a
    session, marks is_new_user=True. Workspace gets the 17.1 defaults."""
    token = _request_and_grab_token(client, "fresh@example.com")
    r = client.post(
        "/auth/magic-link/consume", json={"token": token},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_new_user"] is True
    assert body["user"]["email"] == "fresh@example.com"
    assert body["session_token"]
    assert body["workspace"]["name"] == "fresh's workspace"

    # User landed verified + on the magic_link provider.
    with session_scope() as s:
        u = s.execute(
            select(User).where(User.email == "fresh@example.com")
        ).scalar_one()
    assert u.email_verified is True
    assert u.auth_provider == "magic_link"

    # Workspace defaults from 17.1 carry through.
    with session_scope() as s:
        ws = s.get(Workspace, body["workspace"]["id"])
    assert ws.plan_tier == "free"
    assert float(ws.free_credits_remaining_usd) == pytest.approx(5.00, abs=1e-6)


def test_consume_signs_in_existing_user(client, alice):
    """Existing user (created via the apikey signup) requesting a
    magic link and consuming it should sign them in — same workspace,
    new session — not create a duplicate user."""
    token = _request_and_grab_token(client, "alice@example.com")
    r = client.post(
        "/auth/magic-link/consume", json={"token": token},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["is_new_user"] is False
    assert body["user"]["id"] == alice["user"]["id"]
    assert body["workspace"]["id"] == alice["workspace"]["id"]

    # Magic-link consume promotes existing user to verified.
    with session_scope() as s:
        u = s.get(User, alice["user"]["id"])
    assert u.email_verified is True
    # Original auth_provider preserved — we don't rewrite history.
    assert u.auth_provider == "apikey"


def test_consume_is_single_use(client):
    """Same token can only be consumed once. Second consume → 422."""
    token = _request_and_grab_token(client, "single-use@example.com")
    r1 = client.post(
        "/auth/magic-link/consume", json={"token": token},
    )
    assert r1.status_code == 200

    r2 = client.post(
        "/auth/magic-link/consume", json={"token": token},
    )
    assert r2.status_code == 422
    assert "invalid or expired" in r2.json()["detail"].lower()


def test_consume_rejects_unknown_token(client):
    """Unknown / never-issued token → 422. Same response shape as
    expired-or-consumed so the client can't probe for token existence."""
    r = client.post(
        "/auth/magic-link/consume", json={"token": "totally-fake-token"},
    )
    assert r.status_code == 422


def test_consume_rejects_expired_token(client):
    """Manually age a token past its expires_at then try to consume it.
    Should look identical to unknown-token rejection."""
    token = _request_and_grab_token(client, "expired@example.com")
    th = _hash(token)
    with session_scope() as s:
        row = s.get(EmailSigninToken, th)
        # Set expires_at to 1 minute ago.
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    r = client.post(
        "/auth/magic-link/consume", json={"token": token},
    )
    assert r.status_code == 422


def test_consume_marks_token_consumed(client):
    """consumed_at gets set so the row is auditable + the single-use
    check has a record to gate on."""
    token = _request_and_grab_token(client, "audit@example.com")
    th = _hash(token)
    r = client.post(
        "/auth/magic-link/consume", json={"token": token},
    )
    assert r.status_code == 200
    with session_scope() as s:
        row = s.get(EmailSigninToken, th)
    assert row.consumed_at is not None
    assert (datetime.now(timezone.utc) - row.consumed_at).total_seconds() < 60


def test_consume_session_token_actually_authenticates(client):
    """The session token returned by consume should be usable as an
    Authorization: Bearer on the next request — proves the session row
    landed in the DB correctly + matches the existing auth machinery."""
    token = _request_and_grab_token(client, "sess@example.com")
    r = client.post(
        "/auth/magic-link/consume", json={"token": token},
    )
    sess_token = r.json()["session_token"]

    # Use the session to hit an authenticated endpoint.
    r2 = client.get(
        "/auth/me",
        headers=auth_headers(sess_token),
    )
    assert r2.status_code == 200
    assert r2.json()["user"]["email"] == "sess@example.com"


def test_consume_validates_token_minlength(client):
    """Tokens that are obviously too short are rejected at the schema
    layer before they hit the DB lookup."""
    r = client.post(
        "/auth/magic-link/consume", json={"token": "x"},
    )
    assert r.status_code == 422
