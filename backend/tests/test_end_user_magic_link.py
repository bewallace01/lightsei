"""Phase 25.2: end-user magic-link auth tests.

Three surfaces:

1. `email_provider.send_end_user_magic_link` capture-mode: distinct
   subject + URL path from the operator helper. Shares the
   FAKE_CAPTURE + REQUIRE_LIVE infrastructure.
2. `POST /auth/end-user/magic-link/request`: always-200 no-leak,
   per-email rate limit, token row insert, email captured,
   vendor_invite_code persisted on the token row.
3. `POST /auth/end-user/magic-link/consume`: signup-via-magic-link
   creates an EndUser, second consume signs the same user in,
   single-use enforcement, unknown / expired token 422,
   vendor_invite_code echoed on response (no linking yet, deferred
   to Phase 27.2 when vendor_invite_codes lands).
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select

import email_provider as ep
from db import session_scope
from models import (
    EndUser,
    EndUserSession,
    EndUserSigninToken,
    EndUserVendorLink,
    VendorInviteCode,
)
from tests.conftest import auth_headers, signup


@pytest.fixture(autouse=True)
def _reset_email_capture():
    """Capture list leaks across tests; clear between to keep
    assertions clean."""
    ep._reset_for_tests()
    yield
    ep._reset_for_tests()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------- email_provider.send_end_user_magic_link ---------- #


def test_end_user_send_captures_with_distinct_subject_and_url():
    """End-user helper lands a captured row with the consumer-
    friendly subject and the /c/auth/magic-link path. Keeps
    the two senders visibly separate so a routing bug surfaces
    immediately in tests."""
    ep.send_end_user_magic_link(
        email="alice@example.com",
        token="tok-eu-abc",
        dashboard_url="http://dash.test",
    )
    captured = ep.captured_emails()
    assert len(captured) == 1
    assert captured[0]["to"] == ["alice@example.com"]
    assert captured[0]["subject"] == "Sign in to your account"
    assert (
        captured[0]["_magic_url"]
        == "http://dash.test/c/auth/magic-link?token=tok-eu-abc"
    )
    # Both bodies present + carry the token through.
    assert "tok-eu-abc" in captured[0]["text"]
    assert "tok-eu-abc" in captured[0]["html"]


def test_end_user_send_distinct_from_operator_send():
    """Subjects differ so an operator sender misrouted into the
    end-user path (or vice versa) shows up in tests rather than in
    a confused user's inbox."""
    ep.send_magic_link(
        email="op@example.com", token="op-1",
        dashboard_url="http://x.test",
    )
    ep.send_end_user_magic_link(
        email="eu@example.com", token="eu-1",
        dashboard_url="http://x.test",
    )
    out = ep.captured_emails()
    subjects = [c["subject"] for c in out]
    assert subjects == ["Sign in to Lightsei", "Sign in to your account"]


# ---------- POST /auth/end-user/magic-link/request ---------- #


def test_end_user_request_inserts_token_and_sends_email(client):
    """Happy path: a fresh email request inserts an
    end_user_signin_tokens row and captures an email."""
    r = client.post(
        "/auth/end-user/magic-link/request",
        json={"email": "newcustomer@example.com"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}

    with session_scope() as s:
        rows = s.execute(
            select(EndUserSigninToken).where(
                EndUserSigninToken.email == "newcustomer@example.com"
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.consumed_at is None
    assert row.vendor_invite_code is None
    assert (row.expires_at - row.created_at).total_seconds() == pytest.approx(
        900, abs=2,
    )

    captured = ep.captured_emails()
    assert len(captured) == 1
    assert captured[0]["to"] == ["newcustomer@example.com"]
    assert "/c/auth/magic-link?token=" in captured[0]["_magic_url"]


def test_end_user_request_lowercases_email(client):
    r = client.post(
        "/auth/end-user/magic-link/request",
        json={"email": "Mixed.Case@Example.COM"},
    )
    assert r.status_code == 200
    with session_scope() as s:
        emails = [
            row.email
            for row in s.execute(select(EndUserSigninToken)).scalars()
        ]
    assert emails == ["mixed.case@example.com"]


def test_end_user_request_persists_vendor_invite_code(client):
    """Phase 25.2 carry-through: the optional invite code lands on
    the token row so Phase 27.2 can read it back at consume time
    after the vendor_invite_codes table lands."""
    r = client.post(
        "/auth/end-user/magic-link/request",
        json={
            "email": "invited@example.com",
            "vendor_invite_code": "inv-jyni-001",
        },
    )
    assert r.status_code == 200
    with session_scope() as s:
        row = s.execute(
            select(EndUserSigninToken).where(
                EndUserSigninToken.email == "invited@example.com"
            )
        ).scalar_one()
    assert row.vendor_invite_code == "inv-jyni-001"


def test_end_user_request_always_200_even_for_unknown_email(client):
    """No leak: the response shape is identical whether or not the
    email already has an end_user row."""
    r = client.post(
        "/auth/end-user/magic-link/request",
        json={"email": "ghost@example.com"},
    )
    assert r.status_code == 200


def test_end_user_request_per_email_rate_limit(client):
    """5 requests/hour for the same email; 6th still 200 (no leak)
    but no new token + no new email."""
    from limits import reset_counter_for_tests

    email = "throttle-eu@example.com"
    for _ in range(5):
        reset_counter_for_tests()
        r = client.post(
            "/auth/end-user/magic-link/request",
            json={"email": email},
        )
        assert r.status_code == 200

    with session_scope() as s:
        count = s.execute(
            select(EndUserSigninToken).where(
                EndUserSigninToken.email == email
            )
        ).scalars().all()
    assert len(count) == 5
    assert len(ep.captured_emails()) == 5

    reset_counter_for_tests()
    r = client.post(
        "/auth/end-user/magic-link/request",
        json={"email": email},
    )
    assert r.status_code == 200
    with session_scope() as s:
        count_after = s.execute(
            select(EndUserSigninToken).where(
                EndUserSigninToken.email == email
            )
        ).scalars().all()
    assert len(count_after) == 5
    assert len(ep.captured_emails()) == 5


def test_end_user_request_validates_email_format(client):
    r = client.post(
        "/auth/end-user/magic-link/request",
        json={"email": "not-an-email"},
    )
    assert r.status_code == 422


# ---------- POST /auth/end-user/magic-link/consume ---------- #


def _request_and_grab_token(
    client, email: str, *, invite_code: str | None = None,
) -> str:
    """Mirror the dashboard click: send a request, pull the
    plaintext token off the captured magic URL."""
    body = {"email": email}
    if invite_code is not None:
        body["vendor_invite_code"] = invite_code
    client.post("/auth/end-user/magic-link/request", json=body)
    captured = ep.captured_emails()
    magic_url = captured[-1]["_magic_url"]
    return magic_url.split("token=", 1)[1]


def test_consume_creates_new_end_user_and_session(client):
    """First-time email creates an EndUser row + EndUserSession,
    returns the plaintext session token, marks is_new_end_user=True.
    The new EndUser lands verified on the magic_link provider."""
    token = _request_and_grab_token(client, "fresh-eu@example.com")
    r = client.post(
        "/auth/end-user/magic-link/consume", json={"token": token},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_new_end_user"] is True
    assert body["end_user"]["email"] == "fresh-eu@example.com"
    assert body["end_user"]["email_verified"] is True
    assert body["end_user"]["auth_provider"] == "magic_link"
    assert body["session_token"]
    assert body["linked_vendors"] == []
    assert body["vendor_invite_code"] is None

    # Backing rows exist + the session is owned by this end_user.
    with session_scope() as s:
        eu = s.execute(
            select(EndUser).where(EndUser.email == "fresh-eu@example.com")
        ).scalar_one()
        sessions = s.execute(
            select(EndUserSession).where(
                EndUserSession.end_user_id == eu.id
            )
        ).scalars().all()
    assert len(sessions) == 1
    assert sessions[0].revoked_at is None


def test_consume_signs_in_existing_end_user(client):
    """Existing EndUser → second magic-link round trip signs them
    in, doesn't create a duplicate row, is_new_end_user=False."""
    token1 = _request_and_grab_token(client, "returning@example.com")
    r1 = client.post(
        "/auth/end-user/magic-link/consume", json={"token": token1},
    )
    assert r1.status_code == 200
    first_id = r1.json()["end_user"]["id"]

    # Second round trip — different token, same end_user.
    token2 = _request_and_grab_token(client, "returning@example.com")
    r2 = client.post(
        "/auth/end-user/magic-link/consume", json={"token": token2},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["is_new_end_user"] is False
    assert body["end_user"]["id"] == first_id

    with session_scope() as s:
        rows = s.execute(
            select(EndUser).where(EndUser.email == "returning@example.com")
        ).scalars().all()
    assert len(rows) == 1  # no duplicate


def test_consume_carries_request_side_vendor_invite_code_to_response(client):
    """An invite code attached at request time rides through the
    email round trip and lands on the consume response. Phase 27.2
    reads this to insert the end_user_vendor_links row."""
    token = _request_and_grab_token(
        client, "invited2@example.com", invite_code="inv-halo-42",
    )
    r = client.post(
        "/auth/end-user/magic-link/consume", json={"token": token},
    )
    assert r.status_code == 200
    assert r.json()["vendor_invite_code"] == "inv-halo-42"


def test_consume_body_invite_code_overrides_request_side(client):
    """If the user types an invite code on the consume page that
    differs from the one they typed at request time, the consume-
    side value wins. Lets a user fix a typo without re-issuing
    the magic link."""
    token = _request_and_grab_token(
        client, "fix-typo@example.com", invite_code="inv-typo",
    )
    r = client.post(
        "/auth/end-user/magic-link/consume",
        json={"token": token, "vendor_invite_code": "inv-correct"},
    )
    assert r.status_code == 200
    assert r.json()["vendor_invite_code"] == "inv-correct"


def test_consume_falls_back_to_request_side_when_body_omits_code(client):
    """When the consume body omits vendor_invite_code, we fall back
    to whatever was attached to the token at request time. Doesn't
    require the dashboard to re-thread the code through."""
    token = _request_and_grab_token(
        client, "fallback@example.com", invite_code="inv-fb-1",
    )
    r = client.post(
        "/auth/end-user/magic-link/consume", json={"token": token},
    )
    assert r.json()["vendor_invite_code"] == "inv-fb-1"


def test_consume_redeems_valid_request_side_vendor_invite_code(client):
    operator = signup(
        client,
        email="invite-owner@example.com",
        workspace_name="Invite Co",
    )
    mint = client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(operator["session_token"]),
        json={"count": 1},
    )
    code = mint.json()["codes"][0]["code"]

    token = _request_and_grab_token(
        client, "autolink@example.com", invite_code=code,
    )
    r = client.post(
        "/auth/end-user/magic-link/consume", json={"token": token},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["linked_vendors"][0]["id"] == operator["workspace"]["id"]

    end_user_id = body["end_user"]["id"]
    with session_scope() as s:
        link = s.get(
            EndUserVendorLink, (end_user_id, operator["workspace"]["id"]),
        )
        assert link is not None
        assert link.removed_at is None

        code_row = s.get(VendorInviteCode, code)
        assert code_row.consumed_at is not None
        assert code_row.consumed_by_end_user_id == end_user_id


def test_consume_is_single_use(client):
    """Same token consumed twice: first 200, second 422."""
    token = _request_and_grab_token(client, "single-use-eu@example.com")
    r1 = client.post(
        "/auth/end-user/magic-link/consume", json={"token": token},
    )
    assert r1.status_code == 200

    r2 = client.post(
        "/auth/end-user/magic-link/consume", json={"token": token},
    )
    assert r2.status_code == 422
    assert "invalid or expired" in r2.json()["detail"].lower()


def test_consume_rejects_unknown_token(client):
    r = client.post(
        "/auth/end-user/magic-link/consume",
        json={"token": "totally-fake-token"},
    )
    assert r.status_code == 422


def test_consume_rejects_expired_token(client):
    """Age a token past its expires_at; consume should look
    identical to the unknown-token rejection (no probing)."""
    token = _request_and_grab_token(client, "expired-eu@example.com")
    th = _hash(token)
    with session_scope() as s:
        row = s.get(EndUserSigninToken, th)
        row.expires_at = row.created_at - timedelta(seconds=1)

    r = client.post(
        "/auth/end-user/magic-link/consume", json={"token": token},
    )
    assert r.status_code == 422


def test_consume_promotes_unverified_existing_end_user(client):
    """If an EndUser row somehow exists with email_verified=False
    (defensive — the consume path is the only way to make one and
    it sets True; this guards against future code paths that
    insert pre-verification), a successful consume flips the flag."""
    with session_scope() as s:
        import uuid as _uuid
        eu = EndUser(
            id=str(_uuid.uuid4()),
            email="unverified@example.com",
            email_verified=False,
            auth_provider="magic_link",
        )
        s.add(eu)

    token = _request_and_grab_token(client, "unverified@example.com")
    r = client.post(
        "/auth/end-user/magic-link/consume", json={"token": token},
    )
    assert r.status_code == 200
    assert r.json()["end_user"]["email_verified"] is True
    assert r.json()["is_new_end_user"] is False
