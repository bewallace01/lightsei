"""Regression tests for end-user Sign in with Apple account binding."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import apple_signin
from apple_signin import AppleIdentityClaim
from db import session_scope
from models import EndUser, EndUserSession


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add_end_user(
    *,
    email: str,
    apple_sub: str | None = None,
    email_verified: bool = True,
    auth_provider: str = "magic_link",
) -> str:
    end_user_id = str(uuid.uuid4())
    now = _now()
    with session_scope() as s:
        s.add(EndUser(
            id=end_user_id,
            email=email,
            apple_sub=apple_sub,
            email_verified=email_verified,
            auth_provider=auth_provider,
            created_at=now,
            updated_at=now,
        ))
    return end_user_id


def _stub_apple_claim(
    monkeypatch,
    *,
    sub: str,
    email: str | None,
    email_verified: bool = True,
) -> None:
    claim = AppleIdentityClaim(
        sub=sub,
        email=email,
        email_verified=email_verified,
    )
    monkeypatch.setattr(
        apple_signin,
        "verify_identity_token",
        lambda _token: claim,
    )


def test_siwa_does_not_trust_body_email_for_unbound_account(
    client,
    monkeypatch,
):
    victim_id = _add_end_user(email="victim@example.com")
    _stub_apple_claim(
        monkeypatch,
        sub="attacker-apple-sub",
        email=None,
    )

    r = client.post(
        "/auth/end-user/sign-in-with-apple",
        json={
            "identity_token": "valid-attacker-token",
            "email": "victim@example.com",
        },
    )

    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "siwa_missing_email"
    with session_scope() as s:
        victim = s.get(EndUser, victim_id)
        assert victim.apple_sub is None
        assert (
            s.query(EndUserSession)
            .filter(EndUserSession.end_user_id == victim_id)
            .count()
            == 0
        )


def test_siwa_binds_first_verified_claim_then_uses_sub_on_later_signin(
    client,
    monkeypatch,
):
    _stub_apple_claim(
        monkeypatch,
        sub="legit-apple-sub",
        email="legit@example.com",
    )

    r1 = client.post(
        "/auth/end-user/sign-in-with-apple",
        json={
            "identity_token": "first-token",
            "display_name": "Legit User",
        },
    )

    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["is_new"] is True
    end_user_id = body1["end_user"]["id"]
    assert body1["end_user"]["email"] == "legit@example.com"

    _stub_apple_claim(
        monkeypatch,
        sub="legit-apple-sub",
        email=None,
    )
    r2 = client.post(
        "/auth/end-user/sign-in-with-apple",
        json={
            "identity_token": "later-token",
            "email": "attacker@example.com",
        },
    )

    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["is_new"] is False
    assert body2["end_user"]["id"] == end_user_id
    assert body2["end_user"]["email"] == "legit@example.com"
    with session_scope() as s:
        end_user = s.get(EndUser, end_user_id)
        assert end_user.apple_sub == "legit-apple-sub"


def test_siwa_links_existing_magic_link_user_only_with_claim_email(
    client,
    monkeypatch,
):
    end_user_id = _add_end_user(
        email="existing@example.com",
        email_verified=False,
    )
    _stub_apple_claim(
        monkeypatch,
        sub="existing-apple-sub",
        email="existing@example.com",
        email_verified=True,
    )

    r = client.post(
        "/auth/end-user/sign-in-with-apple",
        json={"identity_token": "first-token"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["end_user"]["id"] == end_user_id
    with session_scope() as s:
        end_user = s.get(EndUser, end_user_id)
        assert end_user.apple_sub == "existing-apple-sub"
        assert end_user.email_verified is True


def test_siwa_rejects_claim_email_bound_to_different_apple_sub(
    client,
    monkeypatch,
):
    end_user_id = _add_end_user(
        email="bound@example.com",
        apple_sub="original-apple-sub",
        auth_provider="siwa",
    )
    _stub_apple_claim(
        monkeypatch,
        sub="other-apple-sub",
        email="bound@example.com",
    )

    r = client.post(
        "/auth/end-user/sign-in-with-apple",
        json={"identity_token": "other-token"},
    )

    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "siwa_account_conflict"
    with session_scope() as s:
        end_user = s.get(EndUser, end_user_id)
        assert end_user.apple_sub == "original-apple-sub"
