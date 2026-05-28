from __future__ import annotations

from sqlalchemy import select

import apple_signin
from db import session_scope
from models import EndUser


def test_siwa_repeat_sign_in_uses_apple_sub_without_email(
    client, monkeypatch,
):
    claims = [
        apple_signin.AppleIdentityClaim(
            sub="apple-user-123",
            email="relay@example.com",
            email_verified=True,
        ),
        apple_signin.AppleIdentityClaim(
            sub="apple-user-123",
            email=None,
            email_verified=True,
        ),
    ]

    def fake_verify(_token: str) -> apple_signin.AppleIdentityClaim:
        return claims.pop(0)

    monkeypatch.setattr(apple_signin, "verify_identity_token", fake_verify)

    first = client.post(
        "/auth/end-user/sign-in-with-apple",
        json={"identity_token": "token-1"},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["is_new_end_user"] is True

    second = client.post(
        "/auth/end-user/sign-in-with-apple",
        json={"identity_token": "token-2"},
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["is_new_end_user"] is False
    assert second_body["end_user"]["id"] == first_body["end_user"]["id"]

    with session_scope() as s:
        rows = s.execute(
            select(EndUser).where(EndUser.apple_sub == "apple-user-123")
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].email == "relay@example.com"
