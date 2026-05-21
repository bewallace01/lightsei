"""Phase 20.2: Google OAuth (connector install) tests.

Three surfaces:

1. `backend/connectors/google_oauth.py` pure helpers — PKCE pair,
   build URL, exchange happy/error, refresh happy/invalid_grant.
2. `GET /connectors/google/start` — 503 unconfigured, 404 unknown
   type, persists state with the right connector_type.
3. `GET /connectors/google/callback` — exchange → encrypted blob →
   ConnectorInstallation row, reinstall updates in place, error paths
   render HTML 400.

Stubs `httpx.post` so tests don't hit Google.
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

import secrets_crypto
from connectors import google_oauth as gco
from db import session_scope
from models import (
    ConnectorInstallation,
    ConnectorOAuthPendingState,
)
from tests.conftest import auth_headers


@pytest.fixture(autouse=True)
def _google_env(monkeypatch):
    monkeypatch.setenv("LIGHTSEI_GOOGLE_CLIENT_ID", "1234.apps.googleusercontent.com")
    monkeypatch.setenv("LIGHTSEI_GOOGLE_CLIENT_SECRET", "fake_secret")
    monkeypatch.setenv(
        "LIGHTSEI_CONNECTORS_GOOGLE_REDIRECT_URI",
        "https://api.lightsei.test/connectors/google/callback",
    )


def _fake_id_token(email: str = "ops@example.com") -> str:
    """Fabricate a JWT-ish string with the email claim in the payload.
    Signature isn't verified by the helper, so any third segment works."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode("ascii").rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email, "sub": "G123"}).encode("ascii"),
    ).decode("ascii").rstrip("=")
    return f"{header}.{payload}.sig"


def _fake_exchange_response(
    *,
    access_token: str = "ya29.fake-access",
    refresh_token: str = "1//fake-refresh",
    expires_in: int = 3600,
    scope: str = (
        "openid email https://www.googleapis.com/auth/gmail.modify"
    ),
    email: str = "ops@example.com",
    status: int = 200,
) -> SimpleNamespace:
    body: dict[str, Any] = {}
    if status < 400:
        body = {
            "access_token": access_token,
            "expires_in": expires_in,
            "scope": scope,
            "token_type": "Bearer",
            "id_token": _fake_id_token(email),
        }
        if refresh_token is not None:
            body["refresh_token"] = refresh_token
    else:
        body = {"error": "invalid_grant"}
    return SimpleNamespace(
        status_code=status,
        json=lambda: body,
        text=str(body),
    )


def _fake_refresh_response(
    *,
    access_token: str = "ya29.refreshed",
    expires_in: int = 3600,
    scope: str = "openid email",
    status: int = 200,
) -> SimpleNamespace:
    body: dict[str, Any] = {}
    if status < 400:
        body = {
            "access_token": access_token,
            "expires_in": expires_in,
            "scope": scope,
            "token_type": "Bearer",
        }
    else:
        body = {"error": "invalid_grant"}
    return SimpleNamespace(
        status_code=status,
        json=lambda: body,
        text=str(body),
    )


# ---------- Pure-helper tests ---------- #


def test_is_configured_true_when_both_env_vars_set():
    assert gco.is_configured() is True


def test_is_configured_false_when_secret_missing(monkeypatch):
    monkeypatch.delenv("LIGHTSEI_GOOGLE_CLIENT_SECRET")
    assert gco.is_configured() is False


def test_new_pkce_pair_returns_verifier_and_challenge():
    v, c = gco.new_pkce_pair()
    assert v != c
    assert len(v) > 40
    assert len(c) > 40
    # All URL-safe-base64 chars (no padding).
    assert all(ch.isalnum() or ch in "_-" for ch in v)
    assert all(ch.isalnum() or ch in "_-" for ch in c)


def test_new_state_is_url_safe_and_random():
    a = gco.new_state()
    b = gco.new_state()
    assert a != b


def test_build_authorization_url_requests_offline_access():
    """Connector flow needs refresh tokens. The sign-in flow doesn't —
    its URL omits access_type=offline. Defense against accidentally
    sharing the sign-in URL builder."""
    url = gco.build_authorization_url(
        state="s",
        challenge="c",
        scopes=["openid", "email", "https://www.googleapis.com/auth/gmail.modify"],
    )
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert qs["access_type"][0] == "offline"
    assert qs["prompt"][0] == "consent"
    assert qs["code_challenge_method"][0] == "S256"
    assert qs["code_challenge"][0] == "c"
    assert qs["state"][0] == "s"
    assert "gmail.modify" in qs["scope"][0]


def test_exchange_happy_path_returns_all_fields(monkeypatch):
    monkeypatch.setattr(
        "connectors.google_oauth.httpx.post",
        lambda *a, **kw: _fake_exchange_response(),
    )
    result = gco.exchange_code_for_tokens(code="code", code_verifier="v")
    assert result["access_token"] == "ya29.fake-access"
    assert result["refresh_token"] == "1//fake-refresh"
    assert result["expires_in"] == 3600
    assert "gmail.modify" in result["scope"]
    assert result["email"] == "ops@example.com"


def test_exchange_missing_refresh_token_raises(monkeypatch):
    """Google sometimes omits refresh_token if it already issued one
    for the same (user, client, scope set). We need it; surface as
    error so the operator knows to revoke + reinstall."""
    monkeypatch.setattr(
        "connectors.google_oauth.httpx.post",
        lambda *a, **kw: _fake_exchange_response(refresh_token=None),
    )
    with pytest.raises(gco.GoogleConnectorOAuthError) as exc:
        gco.exchange_code_for_tokens(code="code", code_verifier="v")
    assert "refresh_token" in str(exc.value)


def test_exchange_http_error_surfaces(monkeypatch):
    monkeypatch.setattr(
        "connectors.google_oauth.httpx.post",
        lambda *a, **kw: _fake_exchange_response(status=400),
    )
    with pytest.raises(gco.GoogleConnectorOAuthError):
        gco.exchange_code_for_tokens(code="code", code_verifier="v")


def test_exchange_transport_error_surfaces(monkeypatch):
    import httpx
    def _boom(*a, **kw):
        raise httpx.ConnectError("dns failed")
    monkeypatch.setattr("connectors.google_oauth.httpx.post", _boom)
    with pytest.raises(gco.GoogleConnectorOAuthError) as exc:
        gco.exchange_code_for_tokens(code="code", code_verifier="v")
    assert "token exchange failed" in str(exc.value)


def test_refresh_happy_path(monkeypatch):
    monkeypatch.setattr(
        "connectors.google_oauth.httpx.post",
        lambda *a, **kw: _fake_refresh_response(),
    )
    r = gco.refresh_access_token(refresh_token="old-refresh")
    assert r["access_token"] == "ya29.refreshed"
    assert r["expires_in"] == 3600


def test_refresh_invalid_grant_raises(monkeypatch):
    """Google's `invalid_grant` on refresh = the user revoked the
    install on Google's side OR 6 months of inactivity. Caller should
    catch this + mark the install revoked."""
    monkeypatch.setattr(
        "connectors.google_oauth.httpx.post",
        lambda *a, **kw: _fake_refresh_response(status=400),
    )
    with pytest.raises(gco.GoogleConnectorOAuthError) as exc:
        gco.refresh_access_token(refresh_token="dead")
    assert "invalid_grant" in str(exc.value)


# ---------- Endpoint tests ---------- #


def _complete_connector_oauth(client, state: str, session_token: str, code: str = "abc"):
    return client.post(
        "/connectors/google/complete",
        headers=auth_headers(session_token),
        json={"code": code, "state": state},
    )


def test_start_503_when_not_configured(client, alice, monkeypatch):
    monkeypatch.delenv("LIGHTSEI_GOOGLE_CLIENT_ID")
    r = client.get(
        "/connectors/google/start?type=gmail",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 503


def test_start_404_when_unknown_connector(client, alice):
    r = client.get(
        "/connectors/google/start?type=notion",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 404


def test_start_persists_state_with_connector_type(client, alice):
    r = client.get(
        "/connectors/google/start?type=gmail",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["authorization_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    state = body["state"]

    with session_scope() as s:
        row = s.get(ConnectorOAuthPendingState, state)
        assert row is not None
        assert row.workspace_id == alice["workspace"]["id"]
        assert row.connector_type == "gmail"
        assert row.installed_by_user_id == alice["user"]["id"]
        assert row.code_verifier  # PKCE verifier persisted


def test_complete_creates_install_row(client, alice, monkeypatch):
    """Happy path: start, dashboard completion, encrypted token blob persisted."""
    start = client.get(
        "/connectors/google/start?type=google_calendar",
        headers=auth_headers(alice["session_token"]),
    ).json()
    state = start["state"]

    monkeypatch.setattr(
        "connectors.google_oauth.httpx.post",
        lambda *a, **kw: _fake_exchange_response(
            scope=(
                "openid email "
                "https://www.googleapis.com/auth/calendar.events "
                "https://www.googleapis.com/auth/calendar.readonly"
            ),
            email="ops@example.com",
        ),
    )

    r = _complete_connector_oauth(client, state, alice["session_token"])
    assert r.status_code == 200, r.text
    assert r.json()["redirect_after"] == "/integrations?installed=google_calendar"

    from sqlalchemy import select as _select
    with session_scope() as s:
        rows = s.execute(
            _select(ConnectorInstallation).where(
                ConnectorInstallation.workspace_id == alice["workspace"]["id"],
                ConnectorInstallation.connector_type == "google_calendar",
            )
        ).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.external_account_email == "ops@example.com"
        assert "calendar.events" in " ".join(row.scopes)
        decoded = row.encrypted_tokens.decode("ascii")
        payload = json.loads(secrets_crypto.decrypt(decoded))
        assert payload["access_token"] == "ya29.fake-access"
        assert payload["refresh_token"] == "1//fake-refresh"
        assert payload["expires_at"] is not None

    # State row was single-use'd.
    with session_scope() as s:
        assert s.get(ConnectorOAuthPendingState, state) is None


def test_complete_reinstall_updates_existing_row(client, alice, monkeypatch):
    """Re-running the install for a connector that's already active
    should update the existing row, not insert a duplicate that
    would violate the partial-unique index."""
    # First install.
    start1 = client.get(
        "/connectors/google/start?type=gmail",
        headers=auth_headers(alice["session_token"]),
    ).json()
    monkeypatch.setattr(
        "connectors.google_oauth.httpx.post",
        lambda *a, **kw: _fake_exchange_response(access_token="first-access"),
    )
    r1 = _complete_connector_oauth(
        client, start1["state"], alice["session_token"], code="first",
    )
    assert r1.status_code == 200, r1.text

    # Second install — same connector, different token.
    start2 = client.get(
        "/connectors/google/start?type=gmail",
        headers=auth_headers(alice["session_token"]),
    ).json()
    monkeypatch.setattr(
        "connectors.google_oauth.httpx.post",
        lambda *a, **kw: _fake_exchange_response(access_token="second-access"),
    )
    r2 = _complete_connector_oauth(
        client, start2["state"], alice["session_token"], code="second",
    )
    assert r2.status_code == 200, r2.text

    # Exactly one active install for gmail. Token = the second one.
    from sqlalchemy import select as _select
    with session_scope() as s:
        rows = s.execute(
            _select(ConnectorInstallation).where(
                ConnectorInstallation.workspace_id == alice["workspace"]["id"],
                ConnectorInstallation.connector_type == "gmail",
                ConnectorInstallation.revoked_at.is_(None),
            )
        ).scalars().all()
        assert len(rows) == 1
        decoded = rows[0].encrypted_tokens.decode("ascii")
        payload = json.loads(secrets_crypto.decrypt(decoded))
        assert payload["access_token"] == "second-access"


def test_complete_400_on_unknown_state(client, alice):
    r = _complete_connector_oauth(client, "does-not-exist", alice["session_token"])
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_state"


def test_callback_forwards_user_cancel_to_dashboard(client):
    r = client.get(
        "/connectors/google/callback?error=access_denied",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/integrations/google/callback?error=access_denied" in r.headers["location"]


def test_complete_400_on_exchange_failure(client, alice, monkeypatch):
    """Token exchange failures are rejected without storing an install."""
    start = client.get(
        "/connectors/google/start?type=google_drive",
        headers=auth_headers(alice["session_token"]),
    ).json()
    monkeypatch.setattr(
        "connectors.google_oauth.httpx.post",
        lambda *a, **kw: _fake_exchange_response(status=400),
    )
    r = _complete_connector_oauth(
        client, start["state"], alice["session_token"], code="bad",
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "exchange_failed"


def test_complete_400_on_unknown_connector_in_state(client, alice, monkeypatch):
    """Defensive: someone hand-crafts a state row with a connector
    that's been removed from the registry. Should render HTML 400,
    not crash."""
    # Insert a state row directly with a bogus connector_type.
    with session_scope() as s:
        s.add(ConnectorOAuthPendingState(
            state="bogus-state",
            workspace_id=alice["workspace"]["id"],
            installed_by_user_id=alice["user"]["id"],
            connector_type="defunct_connector",
            code_verifier="v",
            redirect_after=None,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ))

    monkeypatch.setattr(
        "connectors.google_oauth.httpx.post",
        lambda *a, **kw: _fake_exchange_response(),
    )
    r = _complete_connector_oauth(client, "bogus-state", alice["session_token"])
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "unknown_connector"


def test_complete_rejects_different_user(client, alice, bob, monkeypatch):
    start = client.get(
        "/connectors/google/start?type=gmail",
        headers=auth_headers(alice["session_token"]),
    ).json()
    monkeypatch.setattr(
        "connectors.google_oauth.httpx.post",
        lambda *a, **kw: _fake_exchange_response(access_token="stolen"),
    )

    r = _complete_connector_oauth(client, start["state"], bob["session_token"])
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "oauth_state_owner_mismatch"

    from sqlalchemy import select as _select
    with session_scope() as s:
        rows = s.execute(
            _select(ConnectorInstallation).where(
                ConnectorInstallation.workspace_id == alice["workspace"]["id"],
                ConnectorInstallation.connector_type == "gmail",
            )
        ).scalars().all()
        assert rows == []
